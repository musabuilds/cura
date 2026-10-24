"""Staging → canonical dataset.

    staging/*.jsonl ──normalise──▶ candidates ──block+link──▶ clusters ──fuse──▶ canonical records
                                                  ▲
                         data/*.jsonl (previous) ─┘  identifier crosswalk keeps IDs stable

Entity resolution is deliberately precision-first: a missed duplicate is a cosmetic problem, a false
merge publishes one hospital's phone number under another's name. See docs/data-model.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from cura import geo, model
from cura import normalize as nz
from cura import reference as ref
from cura.paths import DATA_DIR, STAGING_DIR

TYPE_SEGMENT = {row["code"]: row["id_segment"] for row in ref.read_csv("facility_types.csv")}

# Types that may describe the same real place when sources disagree on the label.
COMPATIBLE = {
    frozenset({"clinic", "primary_care_center"}),
    frozenset({"clinic", "doctor_office"}),
    frozenset({"clinic", "dental_clinic"}),
    frozenset({"clinic", "other"}),
    frozenset({"clinic", "imaging_center"}),
    frozenset({"clinic", "rehabilitation"}),
    frozenset({"laboratory", "other"}),
    frozenset({"imaging_center", "other"}),
    frozenset({"doctor_office", "dental_clinic"}),
}

CORE_FACILITY_FIELDS = (
    "name.en",
    "name.ar",
    "ownership",
    "address.governorate",
    "location",
    "contact.phones",
    "hours.opening_hours",
    "contact.website",
    "insurance",
    "specialties",
)
CORE_PRACTITIONER_FIELDS = ("name.en", "name.ar", "specialty", "grade", "languages", "qualifications", "title")


def trust(source: str, meta: dict[str, Any] | None = None) -> float:
    """Prior reliability of a source. Overridable per source with `"trust"` in its meta file."""
    if meta and isinstance(meta.get("trust"), int | float):
        return float(meta["trust"])
    if source == "cura_api":
        return 0.97  # verified owners editing their own record
    if source.startswith(("moh_", "rms_", "unrwa_")):
        return 0.95
    if source.startswith(("hcac", "jci", "pha_")) or source.endswith(
        ("_doctors", "_branches", "_official", "_hospital")
    ):
        return 0.9
    if source.endswith("_network"):
        return 0.75
    if source == "osm":
        return 0.7
    if source.startswith("wikidata"):
        return 0.6
    return 0.65


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _h(*parts: object) -> str:
    return "h:" + hashlib.sha1("|".join(str(p or "") for p in parts).encode()).hexdigest()[:12]


def _uniq(items: Iterable[Any], key=lambda x: x) -> list[Any]:
    seen, out = set(), []
    for item in items:
        k = key(item)
        if item in (None, "") or k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# --- loading -----------------------------------------------------------------------------------


def load_staging() -> tuple[dict[str, list[dict]], dict[str, dict]]:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    metas: dict[str, dict] = {}
    for path in sorted(STAGING_DIR.glob("*.jsonl")):
        source = path.stem
        meta_path = path.with_suffix(".meta.json")
        metas[source] = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if metas[source].get("disabled"):
            continue
        with path.open(encoding="utf-8-sig") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}:{n}: {exc}") from exc
                rec.setdefault("source", source)
                if rec.get("kind") and isinstance(rec.get("data"), dict):
                    by_kind[rec["kind"]].append(rec)
    return by_kind, metas


def load_canonical(name: str) -> list[dict]:
    path = DATA_DIR / f"{name}.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_canonical(name: str, records: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for rec in sorted(records, key=lambda r: r.get("id") or json.dumps(r, sort_keys=True)):
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str) + "\n")


# --- facilities --------------------------------------------------------------------------------


@dataclass
class Candidate:
    rec: dict
    trust: float
    type: str
    name_en: str | None
    name_ar: str | None
    keys: list[str]
    lat: float | None
    lon: float | None
    gov: str | None
    city_key: str
    addr_key: str
    phones: list[str]
    domain: str | None
    ident: tuple[str, str]
    shared_point: bool = False  # same point as ≥5 other records of its source: a building or a geocoder centroid
    links: set[int] = field(default_factory=set)

    @property
    def source(self) -> str:
        return self.rec["source"]

    @property
    def data(self) -> dict:
        return self.rec["data"]


def _split_name(data: dict) -> tuple[str | None, str | None]:
    en, ar = nz.clean(data.get("name_en")), nz.clean(data.get("name_ar"))
    if generic := nz.clean(data.get("name")):
        if nz.is_arabic(generic):
            ar = ar or generic
        else:
            en = en or generic
    if en and nz.is_arabic(en) and not ar:
        en, ar = None, en
    return en, ar


def facility_candidate(rec: dict, metas: dict[str, dict]) -> Candidate | None:
    data = rec["data"]
    ftype = data.get("type") if data.get("type") in TYPE_SEGMENT else "other"
    en, ar = _split_name(data)
    if not (en or ar) or all(nz.is_placeholder_name(n) for n in (en, ar) if n):
        return None  # "hospital", "مركز صحي": says what, not which — unpublishable on its own
    if rec["source"] == "osm" and any(nz.names_non_health(n) for n in (en, ar)):
        return None
    if rec["source"] == "osm" and ftype in ("hospital", "clinic", "other", "doctor_office"):
        # OSM in Jordan over-uses amenity=hospital/clinic; the place's own name is better evidence.
        declared = nz.type_declared_by_name(en) or nz.type_declared_by_name(ar)
        if declared and declared != ftype and not (ftype == "doctor_office" and declared == "clinic"):
            ftype = declared
    lat, lon = data.get("lat"), data.get("lon")
    try:
        lat, lon = (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)
    except (TypeError, ValueError):
        lat = lon = None
    if lat is not None and not (29.0 <= lat <= 33.5 and 34.8 <= lon <= 39.4):
        lat = lon = None  # outside Jordan's bounding box: a swapped pair or a bad geocode
    gov = (geo.governorate_at(lat, lon) if lat is not None else None) or nz.governorate(data.get("governorate"))
    if gov is None:
        gov = nz.governorate(data.get("city"))
    if gov is None:
        # "سارة التخصصي/ المفرق", "Al-Iman Hospital - Ajloun": the published name states the place.
        for n in (ar, en):
            if n and (m := re.search(r"[/\-–]\s*([^/\-–]+)$", n)):
                gov = nz.governorate(m[1])
                if gov:
                    break
    source = rec["source"]
    ident_ref = rec.get("source_ref") or _h(ftype, en or ar, data.get("address"), data.get("city"), gov)
    return Candidate(
        rec=rec,
        trust=trust(source, metas.get(source)),
        type=ftype,
        name_en=en,
        name_ar=ar,
        keys=[k for k in (nz.match_key(en), nz.match_key(ar)) if k],
        lat=lat,
        lon=lon,
        gov=gov,
        city_key=nz.match_key(data.get("city"), drop=set()),
        addr_key=nz.match_key(" ".join(str(data.get(k) or "") for k in ("district", "street", "address")), drop=set()),
        phones=nz.phones(data.get("phones"), gov),
        domain=nz.domain(data.get("website")),
        ident=(source, str(ident_ref)),
    )


def _name_sim(a: Candidate, b: Candidate) -> float:
    best = 0.0
    for ka in a.keys:
        for kb in b.keys:
            short = min(len(ka.split()), len(kb.split()))
            # Ministry lists use short forms ("الإيمان" for "مستشفى الإيمان / عجلون"): for hospitals in
            # the same governorate a one-word name contained in the other counts as a match.
            if short == 1 and a.type == b.type == "hospital" and a.gov and a.gov == b.gov:
                best = max(best, fuzz.token_set_ratio(ka, kb))
            elif short >= 2:
                best = max(best, fuzz.token_set_ratio(ka, kb), fuzz.token_sort_ratio(ka, kb))
            else:
                best = max(best, fuzz.ratio(ka, kb))
    return best


def _compatible(a: str, b: str) -> bool:
    return a == b or frozenset({a, b}) in COMPATIBLE


def _is_match(a: Candidate, b: Candidate) -> bool:
    if not _compatible(a.type, b.type):
        return False
    if a.source == b.source and a.source != "osm":
        return False  # a single non-OSM source lists each place once; same-name rows are branches
    sim = _name_sim(a, b)
    if a.lat is not None and b.lat is not None and not (a.shared_point or b.shared_point):
        dist = geo.haversine_m(a.lat, a.lon, b.lat, b.lon)
        limit = 1200 if a.type == "hospital" else 250
        return (dist <= limit and sim >= 85) or (dist <= 60 and sim >= 70)
    if a.gov and b.gov and a.gov != b.gov:
        return False
    shared_contact = bool(set(a.phones) & set(b.phones)) or (a.domain is not None and a.domain == b.domain)
    if a.type == "hospital" and b.type == "hospital":
        return sim >= 90 or (shared_contact and sim >= 70)
    if not (a.gov and b.gov):
        return False
    # Government health centres are named after their village ("مركز صحي إشتفينا"), unique inside a
    # governorate, and official lists rarely carry coordinates — so a near-exact name is enough.
    if "primary_care_center" in (a.type, b.type) and sim >= 97:
        return True
    if a.addr_key and b.addr_key and fuzz.token_set_ratio(a.addr_key, b.addr_key) < 50:
        return False
    same_city = bool(a.city_key) and a.city_key == b.city_key
    return sim >= 95 and (shared_contact or same_city)


class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def cluster(cands: list[Candidate], crosswalk: dict[tuple[str, str], str]) -> list[list[int]]:
    dsu = _DSU(len(cands))
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(cands):
        for key in c.keys:
            for tok in set(key.split()):
                if len(tok) >= 3:
                    blocks[f"t:{tok}"].append(i)
        for p in c.phones:
            blocks[f"p:{p}"].append(i)
        if c.domain:
            blocks[f"d:{c.domain}"].append(i)
        if (cid := crosswalk.get(c.ident)) is not None:
            blocks[f"id:{cid}"].append(i)
    compared: set[tuple[int, int]] = set()
    for bkey, members in blocks.items():
        if bkey.startswith("id:"):
            for j in members[1:]:
                dsu.union(members[0], j)
            continue
        if len(members) > 400:  # a token like "pharmacy1" shared by a whole chain: no signal
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = members[x], members[y]
                if (i, j) in compared:
                    continue
                compared.add((i, j))
                if _is_match(cands[i], cands[j]):
                    dsu.union(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(cands)):
        groups[dsu.find(i)].append(i)
    return list(groups.values())


def _pick(members: list[Candidate], getter) -> Any:
    for c in members:
        value = getter(c)
        if value not in (None, "", [], {}):
            return value
    return None


def _strings(members: list[Candidate], key: str) -> list[str] | None:
    seen_any = any(key in c.data for c in members)
    values = [nz.clean(v) for c in members for v in _as_list(c.data.get(key))]
    out = _uniq([v for v in values if v], key=lambda s: nz.match_key(s, drop=set()))
    return out if (out or seen_any) else None


def fuse_facility(members: list[Candidate], fid: str, previous: dict | None, now: datetime) -> dict:
    members = sorted(members, key=lambda c: -c.trust)
    located = sorted(
        (c for c in members if c.lat is not None),
        key=lambda c: (c.shared_point, c.source not in ("cura_api", "osm"), -c.trust),
    )
    loc = located[0] if located else None
    ftype = _pick(members, lambda c: c.type if c.type != "other" else None) or "other"
    ownership = _pick(
        members,
        lambda c: (
            c.data.get("ownership")
            if c.data.get("ownership") in model.Ownership.__members__ and c.data.get("ownership") != "unknown"
            else None
        ),
    )

    name_en = _pick(members, lambda c: c.name_en)
    name_ar = _pick(members, lambda c: c.name_ar)
    aliases = _uniq(
        [n for c in members for n in (c.name_en, c.name_ar) if n and n not in (name_en, name_ar)],
        key=nz.match_key,
    )

    hours_raw = _pick(members, lambda c: c.data.get("opening_hours"))
    hours = nz.parse_opening_hours(hours_raw) if hours_raw else {}
    if hours and "weekly" not in hours and not re.match(r"^(Mo|Tu|We|Th|Fr|Sa|Su|PH|24/7)", hours_raw):
        hours = {"text": hours_raw}

    phones = _uniq(p for c in members for p in c.phones)
    emails = _uniq(e for c in members for v in _as_list(c.data.get("emails")) if (e := nz.email(v)))
    website = _pick(members, lambda c: nz.website(c.data.get("website")) if c.domain else None)

    departments = _strings(members, "departments")
    services = _strings(members, "services")
    spec_labels = (departments or []) + [s for c in members for s in _as_list(c.data.get("specialties"))]
    specialties = _uniq(code for s in spec_labels if (code := ref.specialties().resolve(s)))

    equipment = None
    if any("equipment" in c.data for c in members):
        equipment = _uniq(
            [
                {"code": ref.equipment().resolve_contains(label) or "other", "name": label, "available": True}
                for c in members
                for label in (nz.clean(v) for v in _as_list(c.data.get("equipment")))
                if label
            ],
            key=lambda e: nz.match_key(e["name"], drop=set()),
        )

    insurance = None
    if any("insurance_accepted" in c.data for c in members):
        insurance = _uniq(
            [
                {"insurer": ref.insurers().resolve(label) or ref.slug(label), "source": c.source}
                for c in members
                for label in (nz.clean(v) for v in _as_list(c.data.get("insurance_accepted")))
                if label
            ],
            key=lambda i: i["insurer"],
        )

    accreditations = None
    if any(c.data.get("accreditations") for c in members):
        accreditations = _uniq(
            [
                {
                    k: v
                    for k, v in {
                        "body": a.get("body"),
                        "status": a.get("status") or "accredited",
                        "year": a.get("year") if isinstance(a.get("year"), int) else None,
                    }.items()
                    if v is not None
                }
                for c in members
                for a in _as_list(c.data.get("accreditations"))
                if isinstance(a, dict) and a.get("body")
            ],
            key=lambda a: (a["body"].lower(), a.get("year")),
        )

    gov = loc.gov if loc else _pick(members, lambda c: c.gov)
    beds = _pick(
        members, lambda c: c.data.get("beds") if isinstance(c.data.get("beds"), int) and c.data["beds"] > 0 else None
    )
    amenities: dict[str, bool | str] = {}
    for c in reversed(members):
        for k in ("wheelchair", "parking", "pharmacy_onsite", "home_visits", "delivery", "drive_through"):
            if (v := c.data.get(k, c.data.get("extra", {}).get(k))) is not None:
                amenities[k] = {"yes": True, "no": False}.get(v, v) if isinstance(v, str) else v

    identifiers: dict[str, str] = dict(previous.get("identifiers", {})) if previous else {}
    for c in members:
        identifiers.setdefault(c.source, c.ident[1])
        if wd := c.data.get("extra", {}).get("wikidata") or c.data.get("wikidata"):
            identifiers.setdefault("wikidata", wd)

    sources = _uniq(
        [
            {
                k: v
                for k, v in {
                    "source": c.source,
                    "ref": c.rec.get("source_ref"),
                    "url": c.rec.get("source_url"),
                    "retrieved_at": c.rec.get("retrieved_at"),
                }.items()
                if v
            }
            for c in members
        ],
        key=lambda s: (s["source"], s.get("ref")),
    )

    status = _pick(
        members, lambda c: c.data.get("status") if c.data.get("status") in model.Status.__members__ else None
    )
    rec: dict[str, Any] = {
        "id": fid,
        "status": status or "active",
        "type": ftype,
        "name": {"en": name_en, "ar": name_ar, "aliases": aliases},
        "ownership": ownership or "unknown",
        "operator": _pick(members, lambda c: nz.clean(c.data.get("operator"))),
        "parent": _pick(members, lambda c: nz.clean(c.data.get("parent"))),
        "address": {
            "governorate": gov,
            "district": (geo.district_at(loc.lat, loc.lon) if loc else None),
            "city": _pick(members, lambda c: nz.clean(c.data.get("city"))),
            "neighborhood": _pick(members, lambda c: nz.clean(c.data.get("district") or c.data.get("neighborhood"))),
            "street": _pick(members, lambda c: nz.clean(c.data.get("street"))),
            "building": _pick(members, lambda c: nz.clean(c.data.get("building"))),
            "floor": _pick(members, lambda c: nz.clean(c.data.get("floor"))),
            "postcode": _pick(members, lambda c: nz.clean(c.data.get("postcode"))),
            "text": _pick(members, lambda c: nz.clean(c.data.get("address"))),
        },
        "location": (
            {
                "lat": round(loc.lat, 6),
                "lon": round(loc.lon, 6),
                "precision": "exact" if loc.source in ("osm", "cura_api") and not loc.shared_point else "approximate",
                "source": loc.source,
            }
            if loc
            else None
        ),
        "contact": {"phones": phones or None, "emails": emails or None, "website": website},
        "hours": hours,
        "emergency": _pick(
            members, lambda c: c.data.get("emergency") if isinstance(c.data.get("emergency"), bool) else None
        ),
        "beds": beds,
        "specialties": specialties or None,
        "departments": departments,
        "services": services,
        "equipment": equipment,
        "insurance": insurance,
        "accreditations": accreditations,
        "languages": _strings(members, "languages"),
        "amenities": amenities,
        "identifiers": dict(sorted(identifiers.items())),
        "sources": sources,
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
    }
    rec["quality"] = _quality(rec, CORE_FACILITY_FIELDS, {s["source"] for s in sources}, members)
    if previous:
        # Fields only the API owns (ratings, busy hours, prices) survive rebuilds.
        for key in ("rating", "busy_hours", "prices"):
            if previous.get(key):
                rec[key] = previous[key]
    if api_rating := _pick(members, lambda c: c.data.get("rating") if c.source == "cura_api" else None):
        rec["rating"] = api_rating
    return _stable(previous, model.Facility.model_validate(rec).model_dump(mode="json"))


def _get(rec: dict, dotted: str) -> Any:
    cur: Any = rec
    for part in dotted.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _quality(rec: dict, core: tuple[str, ...], sources: set[str], members: list[Candidate] | list) -> dict:
    known = sum(1 for f in core if _get(rec, f) not in (None, "", [], {}, "unknown"))
    families = {s.split("_")[0] for s in sources}
    # One source ≈ 0.5; each independent family corroborating adds 0.15, capped at 1.
    confidence = min(1.0, 0.35 + 0.15 * len(families) + (0.1 if "cura_api" in sources else 0.0))
    retrieved = [m.rec.get("retrieved_at") for m in members if getattr(m, "rec", None) and m.rec.get("retrieved_at")]
    return {
        "completeness": round(known / len(core), 2),
        "confidence": round(confidence, 2),
        "last_verified_at": max(retrieved) if retrieved else None,
    }


_VOLATILE = {"updated_at", "quality", "created_at"}


def _stable(previous: dict | None, new: dict) -> dict:
    """Keep `updated_at` when nothing but bookkeeping changed, so rebuilds don't churn the diff."""
    if previous and previous.get("updated_at"):
        strip = lambda r: json.dumps({k: v for k, v in r.items() if k not in _VOLATILE}, sort_keys=True, default=str)  # noqa: E731
        if strip(previous) == strip(new):
            new["updated_at"] = previous["updated_at"]
    return new


class IdAllocator:
    """Sequential IDs per prefix. The top tenth of each range (≥ 90000 / ≥ 900000) is reserved for
    records created through the API, so the pipeline and the API can never hand out the same ID."""

    def __init__(self, existing: Iterable[str], width: int):
        self.width = width
        self.reserved_from = 9 * 10 ** (width - 1)
        self.next: dict[str, int] = defaultdict(lambda: 1)
        for rid in existing:
            prefix, _, num = rid.rpartition("-")
            if int(num) < self.reserved_from:
                self.next[prefix] = max(self.next[prefix], int(num) + 1)

    def new(self, prefix: str) -> str:
        n = self.next[prefix]
        self.next[prefix] = n + 1
        return f"{prefix}-{n:0{self.width}d}"


def merge_facilities(
    staged: list[dict], metas: dict[str, dict], now: datetime
) -> tuple[list[dict], dict[tuple[str, str], str]]:
    previous = {r["id"]: r for r in load_canonical("facilities")}
    crosswalk = {(k, v): r["id"] for r in previous.values() for k, v in r.get("identifiers", {}).items()}
    for c in staged:
        # API-created places arrive with the ID the API allocated (reserved range ≥ 90000).
        if c["source"] == "cura_api" and re.match(model.ID_PATTERN, str(c.get("source_ref", ""))):
            crosswalk[("cura_api", c["source_ref"])] = c["source_ref"]
    for r in previous.values():
        for s in r.get("sources", []):
            if s.get("ref"):
                crosswalk.setdefault((s["source"], s["ref"]), r["id"])
    cands = [c for rec in staged if (c := facility_candidate(rec, metas))]
    points = Counter((c.source, round(c.lat, 4), round(c.lon, 4)) for c in cands if c.lat is not None)
    for c in cands:
        if c.lat is not None and points[(c.source, round(c.lat, 4), round(c.lon, 4))] >= 5:
            c.shared_point = True
    # Deterministic order: IDs for brand-new places are allocated in this order.
    cands.sort(key=lambda c: (c.gov or "JO-XX", c.type, c.keys[0] if c.keys else "", c.ident))
    clusters = cluster(cands, crosswalk)
    alloc = IdAllocator([*previous, *(v for k, v in crosswalk.items() if k[0] == "cura_api")], width=5)
    out: dict[str, dict] = {}
    ident_to_id: dict[tuple[str, str], str] = {}
    for members_idx in sorted(clusters, key=lambda m: min(m)):
        members = [cands[i] for i in members_idx]
        old_ids = sorted({crosswalk[c.ident] for c in members if c.ident in crosswalk})
        if old_ids:
            fid = old_ids[0]
            for dup in old_ids[1:]:
                if dup in previous:
                    out[dup] = previous[dup] | {"status": "merged", "replaced_by": fid}
        else:
            lead = max(members, key=lambda c: c.trust)
            gov = next((c.gov for c in members if c.lat is not None and c.gov), None) or lead.gov or "JO-XX"
            ftype = (
                _pick(sorted(members, key=lambda c: -c.trust), lambda c: c.type if c.type != "other" else None)
                or "other"
            )
            fid = alloc.new(f"JO-{gov[3:]}-{TYPE_SEGMENT[ftype]}")
        if fid in out and out[fid].get("status") != "merged":
            # Two clusters claim one old ID (a previous false merge was split): newest gets a new ID.
            lead = max(members, key=lambda c: c.trust)
            fid = alloc.new(f"JO-{(lead.gov or 'JO-XX')[3:]}-{TYPE_SEGMENT[lead.type]}")
        out[fid] = fuse_facility(members, fid, previous.get(fid), now)
        for c in members:
            ident_to_id[c.ident] = fid
    # Records no source mentions any more are kept, not deleted: absence of evidence is not closure.
    for pid, prev in previous.items():
        out.setdefault(pid, prev)
    return list(out.values()), ident_to_id


# --- practitioners & roles ---------------------------------------------------------------------


def derived_facility(rec: dict) -> dict | None:
    """A network-listed doctor with their own address/phone implies a private clinic."""
    d = rec["data"]
    if not (d.get("address") or d.get("clinic_phones") or d.get("lat")):
        return None
    name = nz.clean(d.get("name_en") or d.get("name_ar"))
    fname = nz.clean(d.get("facility_name"))
    if not fname and not name:
        return None
    dental = (ref.specialties().resolve(d.get("specialty")) or "").startswith(
        ("dent", "ortho", "endo", "perio", "prosth", "pediatric_dent")
    )
    if not fname:
        fname = f"عيادة {name}" if nz.is_arabic(name) else f"{name} Clinic"
    return {
        "kind": "facility",
        "source": rec["source"],
        "source_ref": "derived:" + str(rec.get("source_ref") or _h(name, fname, d.get("address"))),
        "source_url": rec.get("source_url"),
        "retrieved_at": rec.get("retrieved_at"),
        "data": {
            "name": fname,
            "type": "dental_clinic" if dental else "doctor_office",
            "governorate": d.get("governorate"),
            "city": d.get("city"),
            "address": d.get("address"),
            "lat": d.get("lat"),
            "lon": d.get("lon"),
            "phones": d.get("clinic_phones"),
            "insurance_accepted": d.get("insurance_accepted"),
            "opening_hours": d.get("schedule"),
        },
    }


class FacilityResolver:
    """Maps a facility name/website written on a doctor directory to a canonical facility id."""

    def __init__(self, facilities: list[dict]):
        self.by_domain: dict[str, list[str]] = defaultdict(list)
        self.named: list[tuple[str, str, str]] = []
        for f in facilities:
            if f.get("status") == "merged" or f["type"] not in (
                "hospital",
                "clinic",
                "primary_care_center",
                "imaging_center",
                "laboratory",
                "dental_clinic",
                "rehabilitation",
                "dialysis_center",
            ):
                continue
            if d := nz.domain(f["contact"].get("website")):
                self.by_domain[d].append(f["id"])
            for n in (f["name"].get("en"), f["name"].get("ar"), *f["name"].get("aliases", [])):
                if k := nz.match_key(n):
                    self.named.append((k, f["id"], f["type"]))

    def resolve(self, name: str | None, website: str | None) -> str | None:
        if (d := nz.domain(website)) and len(ids := self.by_domain.get(d, [])) == 1:
            return ids[0]
        key = nz.match_key(name)
        if not key:
            return None
        scored = sorted(
            ((fuzz.token_sort_ratio(key, k), fid) for k, fid, _ in self.named),
            reverse=True,
        )
        if (
            scored
            and scored[0][0] >= 92
            and (len(scored) == 1 or scored[1][1] == scored[0][1] or scored[1][0] < scored[0][0] - 3)
        ):
            return scored[0][1]
        if d and (ids := self.by_domain.get(d)):
            return ids[0] if len(set(ids)) == 1 else None
        return None


def _grade(value: str | None) -> str:
    v = (value or "").lower()
    if any(w in v for w in ("consultant", "استشاري")):
        return "consultant"
    if any(w in v for w in ("specialist", "اخصائي", "أخصائي")):
        return "specialist"
    if any(w in v for w in ("resident", "مقيم")):
        return "resident"
    if any(w in v for w in ("general practitioner", "gp", "طبيب عام", "general_practitioner")):
        return "general_practitioner"
    return "unknown"


def merge_practitioners(
    staged: list[dict],
    metas: dict[str, dict],
    facilities: list[dict],
    ident_to_id: dict[tuple[str, str], str],
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    resolver = FacilityResolver(facilities)
    prev_p = {r["id"]: r for r in load_canonical("practitioners")}
    prev_roles = {(r["practitioner_id"], r["facility_id"]): r for r in load_canonical("roles")}
    crosswalk = {(k, v): r["id"] for r in prev_p.values() for k, v in r.get("identifiers", {}).items()}
    for rec in staged:
        if rec["source"] == "cura_api" and re.match(model.PRACTITIONER_ID_PATTERN, str(rec.get("source_ref", ""))):
            crosswalk[("cura_api", rec["source_ref"])] = rec["source_ref"]
    facility_ids = {f["id"] for f in facilities}

    rows = []
    for rec in staged:
        d = rec["data"]
        en, ar = _split_name(d)
        if not (en or ar):
            continue
        if any(nz.type_declared_by_name(n) for n in (en, ar) if n):
            continue  # "DR. PETRA MEDICAL CENTER" in a doctors column is a place; its facility is derived
        (en, title_en), (ar, title_ar) = nz.strip_title(en), nz.strip_title(ar)
        if (title_en or title_ar) and not d.get("title"):
            d = rec["data"] = d | {"title": title_en or title_ar}
        spec = ref.specialties().resolve(d.get("specialty"))
        derived = derived_facility(rec)
        fid = None
        if derived:
            fid = ident_to_id.get((rec["source"], derived["source_ref"]))
        if d.get("facility_id") in facility_ids:
            fid = d["facility_id"]
        fid = fid or resolver.resolve(d.get("facility_name"), d.get("facility_website"))
        ident = (rec["source"], str(rec.get("source_ref") or _h(en or ar, d.get("facility_name"), d.get("specialty"))))
        rows.append(
            {
                "rec": rec,
                "en": en,
                "ar": ar,
                "spec": spec,
                "fid": fid,
                "ident": ident,
                "keys": [k for k in (nz.person_key(en), nz.person_key(ar)) if k],
                "trust": trust(rec["source"], metas.get(rec["source"])),
            }
        )

    # Cluster: identical person key + compatible specialty. Two-token names ("Ahmad Ali") collide
    # across Jordan, so those only merge inside the same facility.
    dsu = _DSU(len(rows))
    index: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        for k in r["keys"]:
            index[k].append(i)
        if r["ident"] in crosswalk:
            index["id:" + crosswalk[r["ident"]]].append(i)
    for key, members in index.items():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                a, b = rows[members[x]], rows[members[y]]
                if key.startswith("id:"):
                    dsu.union(members[x], members[y])
                    continue
                if a["spec"] and b["spec"] and a["spec"] != b["spec"]:
                    parent = lambda s: (ref.specialties().rows.get(s) or {}).get("parent")  # noqa: E731
                    if parent(a["spec"]) != b["spec"] and parent(b["spec"]) != a["spec"]:
                        continue
                if len(key.split()) < 3 and a["fid"] != b["fid"]:
                    continue
                if a["rec"]["source"] == b["rec"]["source"] and a["fid"] == b["fid"] and a["ident"] != b["ident"]:
                    # Same directory listing the same name twice at the same place: trust it is one person.
                    pass
                dsu.union(members[x], members[y])
    # Arabic names are chains (given · father · grandfather · family); sources drop the grandfather.
    # "عبدالله وليد هيدموس" ⊂ "عبدالله وليد عبدالله هيدموس": same first and family name, one chain a
    # subsequence of the other (given + father equal, at most one link missing), ≥ 3 parts and the
    # same stated specialty → one person.
    chains: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["ar"] and r["spec"] and len(toks := nz.person_key(r["ar"]).split()) >= 3:
            chains[(toks[0], toks[-1], r["spec"])].append(i)
    for members in chains.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                a = nz.person_key(rows[members[x]]["ar"]).split()
                b = nz.person_key(rows[members[y]]["ar"]).split()
                short, long_ = (a, b) if len(a) <= len(b) else (b, a)
                # Given and father name must agree; only middle links may be missing.
                if short[:2] != long_[:2] or len(long_) - len(short) > 1:
                    continue
                it = iter(long_)
                if all(tok in it for tok in short):
                    dsu.union(members[x], members[y])

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[dsu.find(i)].append(i)

    alloc = IdAllocator(prev_p, width=6)
    role_alloc = IdAllocator((r["id"] for r in prev_roles.values()), width=7)
    practitioners: dict[str, dict] = {}
    roles: dict[tuple[str, str], dict] = {}
    for idxs in sorted(groups.values(), key=lambda g: (rows[g[0]]["keys"] or [""])[0]):
        members = sorted((rows[i] for i in idxs), key=lambda r: -r["trust"])
        old = sorted({crosswalk[m["ident"]] for m in members if m["ident"] in crosswalk})
        pid = old[0] if old else alloc.new("JO-PRC")
        if pid in practitioners:
            pid = alloc.new("JO-PRC")
        first = lambda key: next((m["rec"]["data"].get(key) for m in members if m["rec"]["data"].get(key)), None)  # noqa: E731, B023
        spec = next((m["spec"] for m in members if m["spec"]), None)
        subs = _uniq(
            [nz.clean(s) for m in members for s in _as_list(m["rec"]["data"].get("subspecialties"))]
            + [nz.clean(m["rec"]["data"].get("specialty")) for m in members if not m["spec"]],
            key=lambda s: nz.match_key(s, drop=set()),
        )
        gender = first("gender")
        prev = prev_p.get(pid, {})
        rec = {
            "id": pid,
            "status": "active",
            "name": {
                "en": next((m["en"] for m in members if m["en"]), None),
                "ar": next((m["ar"] for m in members if m["ar"]), None),
                "aliases": _uniq([n for m in members[1:] for n in (m["en"], m["ar"]) if n], key=nz.person_key),
            },
            "title": nz.clean(first("title")),
            "gender": gender if gender in ("male", "female") else None,
            "grade": next(
                (
                    g
                    for m in members
                    if (g := _grade(m["rec"]["data"].get("grade") or m["rec"]["data"].get("title"))) != "unknown"
                ),
                "unknown",
            ),
            "specialty": spec,
            "subspecialties": [s for s in subs if s] or None,
            "languages": _uniq([nz.clean(v) for m in members for v in _as_list(m["rec"]["data"].get("languages"))])
            or None,
            "qualifications": _uniq(
                [nz.clean(v) for m in members for v in _as_list(m["rec"]["data"].get("qualifications"))]
            )
            or None,
            "license_verified": bool(prev.get("license_verified")),
            "rating": next((m["rec"]["data"]["rating"] for m in members if m["rec"]["data"].get("rating")), None)
            or prev.get("rating")
            or {"overall": None, "count": 0, "dimensions": {}},
            "identifiers": dict(
                sorted(
                    (
                        {**prev.get("identifiers", {}), **{m["ident"][0]: m["ident"][1] for m in reversed(members)}}
                    ).items()
                )
            ),
            "sources": _uniq(
                [
                    {
                        k: v
                        for k, v in {
                            "source": m["rec"]["source"],
                            "ref": m["rec"].get("source_ref"),
                            "url": m["rec"].get("source_url") or m["rec"]["data"].get("profile_url"),
                            "retrieved_at": m["rec"].get("retrieved_at"),
                        }.items()
                        if v
                    }
                    for m in members
                ],
                key=lambda s: (s["source"], s.get("ref"), s.get("url")),
            ),
            "created_at": prev.get("created_at") or now,
            "updated_at": now,
        }
        rec["quality"] = _quality(rec, CORE_PRACTITIONER_FIELDS, {s["source"] for s in rec["sources"]}, [])
        rec["quality"]["last_verified_at"] = (
            max((m["rec"].get("retrieved_at") or "" for m in members), default=None) or None
        )
        practitioners[pid] = _stable(prev, model.Practitioner.model_validate(rec).model_dump(mode="json"))

        for m in members:
            if not m["fid"]:
                continue
            d = m["rec"]["data"]
            key = (pid, m["fid"])
            fee = d.get("consultation_fee")
            schedule = nz.clean(d.get("schedule"))
            if key in roles:
                role = roles[key]
                role["sources"].append({"source": m["rec"]["source"], "url": d.get("profile_url")})
                role["consultation_fee"] = role["consultation_fee"] or (
                    fee if isinstance(fee, dict) and "amount" in fee else None
                )
                continue
            prev_role = prev_roles.get(key, {})
            hours = nz.parse_opening_hours(schedule) if schedule else {}
            if hours and "weekly" not in hours and not re.match(r"^(Mo|Tu|We|Th|Fr|Sa|Su)", schedule):
                hours = {"text": schedule}
            roles[key] = {
                "id": prev_role.get("id") or role_alloc.new("JO-ROL"),
                "practitioner_id": pid,
                "facility_id": m["fid"],
                "active": True,
                "specialty": m["spec"],
                "hours": hours,
                "consultation_fee": fee if isinstance(fee, dict) and "amount" in fee else None,
                "insurance": [
                    {"insurer": ref.insurers().resolve(x) or ref.slug(x), "source": m["rec"]["source"]}
                    for x in _as_list(d.get("insurance_accepted"))
                    if nz.clean(x)
                ]
                or None,
                "phones": nz.phones(d.get("clinic_phones"), nz.governorate(d.get("governorate"))) or None,
                "profile_url": d.get("profile_url"),
                "sources": [{"source": m["rec"]["source"], "url": d.get("profile_url")}],
                "updated_at": now,
            }
    for pid, prev in prev_p.items():
        practitioners.setdefault(pid, prev)
    role_list = []
    for role in roles.values():
        role["sources"] = _uniq(
            [{k: v for k, v in s.items() if v} for s in role["sources"]], key=lambda s: json.dumps(s, sort_keys=True)
        )
        prev_role = prev_roles.get((role["practitioner_id"], role["facility_id"]))
        role_list.append(_stable(prev_role, model.Role.model_validate(role).model_dump(mode="json")))
    known = {(r["practitioner_id"], r["facility_id"]) for r in role_list}
    role_list += [r for k, r in prev_roles.items() if k not in known and k[0] in practitioners]
    return list(practitioners.values()), role_list


# --- reference kinds ---------------------------------------------------------------------------


def merge_insurers(staged: list[dict]) -> int:
    rows = {r["code"]: r for r in ref.read_csv("insurers.csv")}
    vocab = ref.insurers()
    for rec in staged:
        d = rec["data"]
        name_en, name_ar = nz.clean(d.get("name_en")), nz.clean(d.get("name_ar"))
        host = (nz.domain(d.get("website")) or "").split(".")[0]
        # Arabic-only names slug to nothing: fall back to the website host, then a name hash.
        code = (
            vocab.resolve(name_en)
            or vocab.resolve(name_ar)
            or (ref.slug(name_en) if name_en else None)
            or (ref.slug(host) if host else None)
            or "ins_" + hashlib.sha1((name_ar or "").encode()).hexdigest()[:8]
        )
        row = rows.setdefault(
            code,
            {
                "code": code,
                "name_en": "",
                "name_ar": "",
                "kind": "",
                "website": "",
                "phones": "",
                "network_url": "",
                "aliases": "",
            },
        )
        row["name_en"] = row["name_en"] or (name_en or "")
        row["name_ar"] = row["name_ar"] or (name_ar or "")
        row["kind"] = row["kind"] or (d.get("kind") or "")
        row["website"] = row["website"] or (nz.website(d.get("website")) or "")
        row["phones"] = row["phones"] or ";".join(
            nz.phones(d.get("phones"), nz.governorate(d.get("address")) or "JO-AM")
        )
        row["network_url"] = row["network_url"] or (d.get("network_url") or "")
        aliases = {a for a in row["aliases"].split(";") if a} | {a for a in _as_list(d.get("aliases")) if a}
        row["aliases"] = ";".join(sorted(aliases))
    ref.write_csv(
        "insurers.csv",
        sorted(rows.values(), key=lambda r: r["code"]),
        ["code", "name_en", "name_ar", "kind", "website", "phones", "network_url", "aliases"],
    )
    ref.reset_caches()
    return len(rows)


def merge_holidays(staged: list[dict]) -> int:
    rows = {}
    for rec in staged:
        d = rec["data"]
        if not d.get("date"):
            continue
        rows[(d["date"], nz.match_key(d.get("name_en"), drop=set()))] = {
            "date": d["date"],
            "name_en": nz.clean(d.get("name_en")) or "",
            "name_ar": nz.clean(d.get("name_ar")) or "",
            "kind": d.get("kind") or "",
            "observed_by": d.get("observed_by") or "all",
            "lunar": "true" if d.get("lunar") else "false",
            "source": rec["source"],
        }
    if rows:
        ref.write_csv(
            "holidays.csv",
            [rows[k] for k in sorted(rows)],
            ["date", "name_en", "name_ar", "kind", "observed_by", "lunar", "source"],
        )
    return len(rows)


def merge_specialties(staged: list[dict]) -> int:
    rows = {r["code"]: r for r in ref.read_csv("specialties.csv")}
    vocab = ref.specialties()
    added = 0
    for rec in staged:
        d = rec["data"]
        en, ar = nz.clean(d.get("name_en")), nz.clean(d.get("name_ar"))
        if not en or vocab.resolve(en, threshold=95) or (ar and vocab.resolve(ar, threshold=95)):
            continue
        code = ref.slug(en)
        if code in rows:
            continue
        rows[code] = {
            "code": code,
            "name_en": en,
            "name_ar": ar or "",
            "parent": vocab.resolve(d.get("parent")) or "",
            "aliases": "",
        }
        added += 1
    ref.write_csv("specialties.csv", list(rows.values()), ["code", "name_en", "name_ar", "parent", "aliases"])
    ref.reset_caches()
    return added


def merge_prices(staged: list[dict]) -> list[dict]:
    out = []
    for rec in staged:
        d = dict(rec["data"])
        if not nz.clean(d.get("item")):
            continue
        d["source"] = rec["source"]
        d["source_url"] = rec.get("source_url")
        out.append({k: v for k, v in d.items() if v not in (None, "", [])})
    out.sort(key=lambda p: (p.get("applies_to", ""), p.get("category", ""), p["item"], p.get("grade", "")))
    return out


# --- entry point -------------------------------------------------------------------------------


def run() -> dict[str, Any]:
    now = _now()
    staged, metas = load_staging()
    report: dict[str, Any] = {"built_at": now.isoformat().replace("+00:00", "Z")}
    report["insurers"] = merge_insurers(staged.get("insurer", []))
    report["holidays"] = merge_holidays(staged.get("holiday", []))
    report["specialties_added"] = merge_specialties(staged.get("specialty", []))

    derived = [d for rec in staged.get("practitioner", []) if (d := derived_facility(rec))]
    facilities, ident_to_id = merge_facilities(staged.get("facility", []) + derived, metas, now)
    practitioners, roles = merge_practitioners(staged.get("practitioner", []), metas, facilities, ident_to_id, now)
    prices = merge_prices(staged.get("price", []))

    write_canonical("facilities", facilities)
    write_canonical("practitioners", practitioners)
    write_canonical("roles", roles)
    with (DATA_DIR / "prices.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for p in prices:
            fh.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")

    report.update(facilities=len(facilities), practitioners=len(practitioners), roles=len(roles), prices=len(prices))
    report["sources"] = {
        s: {k: m.get(k) for k in ("title", "license", "retrieved_at", "record_count", "disabled")}
        for s, m in sorted(metas.items())
    }
    return report
