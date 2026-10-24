"""Finding a record that already exists: "is this new hospital one we have?", "which of these is me?".

Facilities are compared with the same entity-resolution rules the pipeline merges sources with
(`pipeline.merge.is_match`), so the API and the nightly merge agree on what "the same place" means.
People are compared on their name with Arabic folding and titles removed, then ranked by the
evidence that separates namesakes: specialty and a shared workplace.

Nothing here decides. It returns ranked candidates with reasons; a person picks, or a moderator.
"""

from __future__ import annotations

import math
import re

from rapidfuzz import fuzz
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from cura import normalize as nz
from cura.api.db import FacilityRow, PractitionerRow, RoleRow
from cura.geo import haversine_m
from cura.pipeline import merge

NEARBY_KM = 2.0
FACILITY_FLOOR = 80  # name similarity (0–100) worth showing a person
PERSON_FLOOR = 85


def _staging(doc: dict, source: str) -> dict:
    """A registry document in the staging shape the pipeline's matcher reads."""
    name, address, contact = doc.get("name") or {}, doc.get("address") or {}, doc.get("contact") or {}
    loc = doc.get("location") or {}
    return {
        "source": source,
        "source_ref": doc.get("id") or "new",
        "data": {
            "type": doc.get("type"),
            "name_en": name.get("en"),
            "name_ar": name.get("ar"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "governorate": address.get("governorate"),
            "city": address.get("city"),
            "district": address.get("neighborhood"),
            "street": address.get("street"),
            "address": address.get("text"),
            "phones": contact.get("phones"),
            "website": contact.get("website"),
        },
    }


def _facility_rows(db: Session, cand: merge.Candidate) -> list[FacilityRow]:
    near = []
    if cand.lat is not None:
        dlat = NEARBY_KM / 111.0
        dlon = NEARBY_KM / (111.0 * max(0.2, abs(math.cos(math.radians(cand.lat)))))
        near.append(
            and_(
                FacilityRow.lat.between(cand.lat - dlat, cand.lat + dlat),
                FacilityRow.lon.between(cand.lon - dlon, cand.lon + dlon),
            )
        )
    tokens = sorted({t for k in cand.keys for t in k.split() if len(t) >= 3}, key=len, reverse=True)[:6]
    if tokens:
        named = or_(*[FacilityRow.search.like(f"% {t} %") for t in tokens])
        near.append(and_(FacilityRow.governorate == cand.gov, named) if cand.gov else named)
    if not near:
        return []
    return list(db.scalars(select(FacilityRow).where(or_(*near), FacilityRow.status != "merged").limit(400)))


def facility_matches(db: Session, record: dict, *, exclude_id: str | None = None, limit: int = 10) -> list[dict]:
    """Existing facilities that may be `record`. `same_place` is the pipeline's own verdict."""
    cand = merge.facility_candidate(_staging(record, "new"), {})
    if cand is None:
        return []
    out = []
    for row in _facility_rows(db, cand):
        if row.id == exclude_id:
            continue
        other = merge.facility_candidate(_staging(row.doc, "registry"), {})
        if other is None:
            continue
        sim = merge.name_similarity(cand, other)
        same = merge.is_match(cand, other)
        dist = (
            round(haversine_m(cand.lat, cand.lon, other.lat, other.lon))
            if cand.lat is not None and other.lat is not None
            else None
        )
        if not same and sim < FACILITY_FLOOR:
            continue
        reasons = [f"name {round(sim)}% similar"]
        if dist is not None:
            reasons.append(f"{dist} m away")
        if set(cand.phones) & set(other.phones):
            reasons.append("shares a phone number")
        if cand.domain and cand.domain == other.domain:
            reasons.append("shares a website")
        if cand.type != other.type:
            reasons.append(f"recorded as {other.type}")
        out.append(
            {
                "id": row.id,
                "type": row.type,
                "name": row.doc["name"],
                "address": row.doc["address"],
                "parent": row.doc.get("parent"),
                "score": round(sim / 100, 2),
                "distance_m": dist,
                "same_place": same,
                "reasons": reasons,
            }
        )
    out.sort(key=lambda c: (not c["same_place"], -c["score"], c["distance_m"] if c["distance_m"] is not None else 1e9))
    return out[:limit]


_PARTICLES = {"al", "el", "al-", "bin", "ibn", "abu"}


def _skeleton(key: str) -> tuple[str, ...] | None:
    """Latin transliterations of one Arabic name differ in their vowels (Musab, Mosab, Mus'ab; Jabiri, Jabri)
    and in the article (Al-Jabiri, Jabiri). Their consonant skeletons agree: msb jbr."""
    tokens = [t for t in key.split() if t not in _PARTICLES]
    if len(tokens) < 2 or not all(t.isascii() and t.isalpha() for t in tokens):
        return None
    out = []
    for t in tokens:
        t = re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiouyw']", "", t))
        if len(t) < 2:
            return None
        out.append(t)
    return tuple(out)


def _person_similarity(a: str, b: str) -> float:
    """Name chains: "Rami Haddad" is "Rami Khalil Haddad" with the father's name dropped, as sources do.
    Same given and family name with one chain inside the other scores as a near match."""
    sim = fuzz.token_sort_ratio(a, b)
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= 2 and short[0] == long_[0] and short[-1] == long_[-1] and set(short) <= set(long_):
        sim = max(sim, 95 - 3 * (len(long_) - len(short)))
    sa, sb = _skeleton(a), _skeleton(b)
    if sa and sa == sb:
        sim = max(sim, 90)
    return sim


def practitioner_matches(
    db: Session,
    *,
    names: list[str],
    specialty: str | None = None,
    facility_id: str | None = None,
    exclude_id: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Existing practitioners who may be the person named. Highest evidence first."""
    bare = [nz.strip_title(n)[0] for n in names if n]
    keys = [k for k in {nz.person_key(n) for n in bare if n} if k]
    if not keys:
        return []
    # Prefilter: any distinctive token of any spelling (transliterations differ: Khalil/Khaleel), then
    # score every hit on folded person keys. A full scan of the name column is cheap at this size.
    tokens = {t for n in bare if n for t in nz.match_key(n, drop=nz.TITLES).split() if len(t) >= 3}
    if not tokens:
        return []
    rows = db.scalars(
        select(PractitionerRow)
        .where(or_(*[PractitionerRow.search.like(f"% {t} %") for t in tokens]), PractitionerRow.status != "merged")
        .limit(3000)
    ).all()
    # Latin spellings of one Arabic name share their opening letters far more often than their vowels
    # (Jabiri / Jabri, Haddad / Hadad): a second, prefix-widened pass, scored by the same rules (skeletons).
    prefixes = {t[:3] for t in tokens if t.isascii() and t.isalpha() and len(t) >= 4 and t not in _PARTICLES}
    if prefixes:
        have = {r.id for r in rows}
        rows += [
            r
            for r in db.scalars(
                select(PractitionerRow)
                .where(
                    or_(*[PractitionerRow.search.like(f"% {p}%") for p in prefixes]), PractitionerRow.status != "merged"
                )
                .limit(2000)
            )
            if r.id not in have
        ]
    workplaces: dict[str, set[str]] = {}
    if facility_id and rows:
        for pid, fid in db.execute(
            select(RoleRow.practitioner_id, RoleRow.facility_id).where(
                RoleRow.practitioner_id.in_([r.id for r in rows])
            )
        ):
            workplaces.setdefault(pid, set()).add(fid)
    out = []
    for row in rows:
        if row.id == exclude_id:
            continue
        theirs = [nz.person_key(n) for n in (row.doc["name"].get("en"), row.doc["name"].get("ar")) if n]
        sim = max((_person_similarity(a, b) for a in keys for b in theirs if b), default=0)
        if sim < PERSON_FLOOR:
            continue
        reasons = ["same name" if sim >= 97 else f"name {round(sim)}% similar"]
        score = sim / 100
        if specialty and row.doc.get("specialty"):
            if row.doc["specialty"] == specialty:
                reasons.append("same specialty")
                score += 0.1
            else:
                reasons.append(f"specialty recorded as {row.doc['specialty']}")
                score -= 0.15
        if facility_id and facility_id in workplaces.get(row.id, set()):
            reasons.append("works at the same facility")
            score += 0.15
        out.append(
            {
                "id": row.id,
                "name": row.doc["name"],
                "title": row.doc.get("title"),
                "specialty": row.doc.get("specialty"),
                "license_verified": row.doc.get("license_verified", False),
                "score": round(min(score, 1.0), 2),
                # "Ahmad Ali" is a hundred people: two-token names need more than a name to be sure.
                "ambiguous": min(len(k.split()) for k in keys) <= 2,
                "reasons": reasons,
            }
        )
    out.sort(key=lambda c: -c["score"])
    return out[:limit]
