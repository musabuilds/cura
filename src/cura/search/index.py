"""Keeping the semantic index current, and searching it.

Freshness without an outbox: every registry row carries `version`, every embedding the
`source_version` it was computed from. `sync` embeds whatever moved; a write never waits on a model,
and a model outage only delays search freshness, never a write.

Search is hybrid. The lexical matcher (Arabic-folded name tokens) is exact about names; vectors are good
at meaning ("heart doctor in Irbid who takes MedNet"). Both rankings are fused with Reciprocal Rank
Fusion (Cormack et al., 2009), which needs no score calibration between the two.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

import numpy as np
from sqlalchemy import Float, and_, bindparam, func, or_, select
from sqlalchemy.orm import Session

from cura.api import store
from cura.api.db import Embedding, FacilityRow, PractitionerRow, RoleRow
from cura.geo import haversine_m
from cura.search import text as T
from cura.search.embedder import Embedder

ROWS = {"facility": FacilityRow, "practitioner": PractitionerRow}
RRF_K = 60


# --- sync ----------------------------------------------------------------------------------------


def _workplaces(db: Session, practitioner_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    q = (
        select(RoleRow.practitioner_id, FacilityRow.doc)
        .join(FacilityRow, FacilityRow.id == RoleRow.facility_id)
        .where(RoleRow.practitioner_id.in_(practitioner_ids))
    )
    for pid, fdoc in db.execute(q):
        out[pid].append(fdoc["name"].get("en") or fdoc["name"].get("ar"))
    return out


def sync(
    db: Session,
    embedder: Embedder,
    *,
    batch: int = 32,
    limit: int | None = None,
    rebuild: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Embed every record whose version moved past its vector (or every record, with `rebuild`)."""
    counts = {"embedded": 0, "unchanged": 0, "removed": 0}
    for kind, row_cls in ROWS.items():
        cursor = ""
        while limit is None or counts["embedded"] < limit:
            stale = or_(
                Embedding.record_id.is_(None),
                Embedding.source_version < row_cls.version,
                Embedding.model != embedder.model,
            )
            stmt = (
                select(row_cls, Embedding)
                .outerjoin(Embedding, and_(Embedding.kind == kind, Embedding.record_id == row_cls.id))
                .where(row_cls.status != "merged", row_cls.id > cursor)
                .order_by(row_cls.id)
                .limit(batch)
            )
            if not rebuild:
                stmt = stmt.where(stale)
            pairs = db.execute(stmt).all()
            if not pairs:
                break
            cursor = pairs[-1][0].id
            places = _workplaces(db, [r.id for r, _ in pairs]) if kind == "practitioner" else {}
            texts = {
                r.id: T.facility_text(r.doc) if kind == "facility" else T.practitioner_text(r.doc, places.get(r.id))
                for r, _ in pairs
            }
            hashes = {rid: T.content_hash(t, embedder.model) for rid, t in texts.items()}
            todo = [r for r, e in pairs if e is None or e.content_hash != hashes[r.id]]
            for r, e in pairs:
                if e is not None and e.content_hash == hashes[r.id]:
                    e.source_version = r.version  # the record changed, not what it means
                    counts["unchanged"] += 1
            if todo:
                vectors = embedder.embed([texts[r.id] for r in todo])
                for r, v in zip(todo, vectors, strict=True):
                    db.merge(
                        Embedding(
                            kind=kind,
                            record_id=r.id,
                            model=embedder.model,
                            source_version=r.version,
                            content_hash=hashes[r.id],
                            vector=v,
                        )
                    )
                counts["embedded"] += len(todo)
            db.commit()
            if progress:
                progress(counts)
    merged = select(Embedding).where(
        or_(
            *[
                and_(Embedding.kind == kind, Embedding.record_id.in_(select(cls.id).where(cls.status == "merged")))
                for kind, cls in ROWS.items()
            ]
        )
    )
    for e in db.scalars(merged).all():
        db.delete(e)
        counts["removed"] += 1
    db.commit()
    return counts


# --- query ---------------------------------------------------------------------------------------

_MATRIX: dict[tuple, tuple] = {}  # SQLite only: (model, kinds) → (stamp, keys, matrix)


def nearest(db: Session, embedder: Embedder, query: str, kinds: list[str], k: int = 200) -> list[tuple[str, str]]:
    """The k records whose vectors are closest to the query, nearest first."""
    qv = embedder.embed([query], query=True)[0]
    base = and_(Embedding.model == embedder.model, Embedding.kind.in_(kinds))
    if db.get_bind().dialect.name == "postgresql":
        param = bindparam("qv", qv, type_=Embedding.__table__.c.vector.type)
        distance = Embedding.vector.op("<=>", return_type=Float())(param)
        rows = db.execute(select(Embedding.kind, Embedding.record_id).where(base).order_by(distance).limit(k))
        return [(kind, rid) for kind, rid in rows]
    # Exact scan for SQLite (tests, local development), cached until the index changes.
    stamp = db.execute(select(func.count(), func.max(Embedding.updated_at)).where(base)).one()
    key = (embedder.model, tuple(sorted(kinds)))
    cached = _MATRIX.get(key)
    if cached is None or cached[0] != tuple(stamp):
        rows = db.execute(select(Embedding.kind, Embedding.record_id, Embedding.vector).where(base)).all()
        keys = [(kind, rid) for kind, rid, _ in rows]
        matrix = np.stack([v for *_, v in rows]) if rows else np.zeros((0, len(qv)), dtype=np.float32)
        cached = _MATRIX[key] = (tuple(stamp), keys, matrix)
    _, keys, matrix = cached
    if not keys:
        return []
    scores = matrix @ qv
    top = np.argsort(-scores)[:k]
    return [keys[i] for i in top]


def _workplace_docs(db: Session, practitioner_ids: set[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    if not practitioner_ids:
        return out
    q = (
        select(RoleRow.practitioner_id, RoleRow.doc, FacilityRow.doc)
        .join(FacilityRow, FacilityRow.id == RoleRow.facility_id)
        .where(RoleRow.practitioner_id.in_(practitioner_ids))
    )
    for pid, role, facility in db.execute(q):
        if store.is_listed(role):
            out[pid].append(facility)
    return out


LOCATION = ("governorate", "near")


def _keep(kind: str, doc: dict, f: dict, places: dict[str, list[dict]] | None = None) -> bool:
    """Facility filters apply to facilities. `governorate` and `near` also apply to doctors, through the
    places they work: "pediatrician in Irbid" means a pediatrician working in Irbid."""
    if doc.get("status") in ("merged", "permanently_closed"):
        return False
    if kind == "practitioner":
        if f.get("specialty") and doc.get("specialty") not in f["specialty"].split(","):
            return False
        where = {k: f[k] for k in LOCATION if f.get(k)}
        if where:
            where["radius_km"] = f.get("radius_km", 10)
            return any(_keep("facility", place, where) for place in (places or {}).get(doc["id"], []))
        return True
    if f.get("type") and doc["type"] not in f["type"].split(","):
        return False
    if f.get("governorate"):
        govs = {g if g.startswith("JO-") else f"JO-{g.upper()}" for g in f["governorate"].split(",")}
        if doc["address"].get("governorate") not in govs:
            return False
    if f.get("specialty") and not set(f["specialty"].split(",")) & set(doc.get("specialties") or []):
        return False
    if f.get("insurer") and not set(f["insurer"].split(",")) & {i["insurer"] for i in doc.get("insurance") or []}:
        return False
    if f.get("emergency") and not doc.get("emergency"):
        return False
    if f.get("near"):
        loc = doc.get("location")
        if not loc:
            return False
        lat, lon = f["near"]
        if haversine_m(lat, lon, loc["lat"], loc["lon"]) > f.get("radius_km", 10) * 1000:
            return False
    return True


def hybrid(
    db: Session,
    embedder: Embedder | None,
    query: str,
    *,
    kinds: list[str],
    filters: dict,
    mode: str = "hybrid",
    limit: int = 20,
) -> tuple[list[dict], str]:
    """Ranked results and the mode actually used (semantic parts drop out when no model is configured)."""
    lexical: dict[tuple[str, str], int] = {}
    semantic: dict[tuple[str, str], int] = {}
    docs: dict[tuple[str, str], dict] = {}
    used = mode if embedder is not None else "lexical"
    if used in ("hybrid", "lexical"):
        hits: list[tuple[str, dict]] = []
        if "facility" in kinds:
            data, _, _ = store.search_facilities(
                db,
                q=query,
                type=filters.get("type"),
                governorate=filters.get("governorate"),
                specialty=filters.get("specialty"),
                insurer=filters.get("insurer"),
                emergency=filters.get("emergency"),
                near=filters.get("near"),
                radius_km=filters.get("radius_km", 10),
                limit=100,
            )
            hits += [("facility", d) for d in data]
        if "practitioner" in kinds:
            data, _ = store.search_practitioners(db, q=query, specialty=filters.get("specialty"), limit=100)
            hits += [("practitioner", d) for d in data]
        places = _workplace_docs(db, {d["id"] for k, d in hits if k == "practitioner"})
        hits = [(k, d) for k, d in hits if k == "facility" or _keep(k, d, filters, places)]
        for rank, (kind, doc) in enumerate(hits):
            lexical.setdefault((kind, doc["id"]), rank + 1)
            docs[(kind, doc["id"])] = doc
    if used in ("hybrid", "semantic"):
        found = nearest(db, embedder, query, kinds)
        by_kind: dict[str, list[str]] = defaultdict(list)
        for kind, rid in found:
            by_kind[kind].append(rid)
        fetched = {(kind, d["id"]): d for kind, ids in by_kind.items() for d in store.docs(db, kind, ids)}
        places = _workplace_docs(db, set(by_kind.get("practitioner", [])))
        rank = 0
        for key in found:
            doc = fetched.get(key)
            if doc is None or not _keep(key[0], doc, filters, places):
                continue
            rank += 1
            semantic[key] = rank
            docs[key] = doc
    fused = []
    for key in set(lexical) | set(semantic):
        score = sum(1 / (RRF_K + r[key]) for r in (lexical, semantic) if key in r)
        fused.append((score, key))
    fused.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for score, (kind, rid) in fused[:limit]:
        item = {
            "kind": kind,
            "id": rid,
            "score": round(score * (RRF_K + 1) / (2 if used == "hybrid" else 1), 4),
            "ranks": {"lexical": lexical.get((kind, rid)), "semantic": semantic.get((kind, rid))},
            "record": docs[(kind, rid)],
        }
        if filters.get("near") and (loc := docs[(kind, rid)].get("location")):
            item["distance_m"] = round(haversine_m(*filters["near"], loc["lat"], loc["lon"]))
        out.append(item)
    return out, used
