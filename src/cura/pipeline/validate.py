"""Dataset checks run in CI on every change to data/. Errors fail the build; warnings are reported."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from pydantic import ValidationError

from cura import model
from cura.pipeline.merge import load_canonical
from cura.reference import read_csv


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def run() -> Report:
    rep = Report()
    facilities = load_canonical("facilities")
    practitioners = load_canonical("practitioners")
    roles = load_canonical("roles")

    for name, rows, cls in (
        ("facility", facilities, model.Facility),
        ("practitioner", practitioners, model.Practitioner),
        ("role", roles, model.Role),
    ):
        ids = Counter(r.get("id") for r in rows)
        rep.errors += [f"duplicate {name} id {i}" for i, n in ids.items() if n > 1]
        for r in rows:
            try:
                cls.model_validate(r)
            except ValidationError as exc:
                rep.errors.append(f"{name} {r.get('id')}: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}")

    fids = {f["id"] for f in facilities}
    pids = {p["id"] for p in practitioners}
    for r in roles:
        if r["facility_id"] not in fids:
            rep.errors.append(f"role {r['id']} → unknown facility {r['facility_id']}")
        if r["practitioner_id"] not in pids:
            rep.errors.append(f"role {r['id']} → unknown practitioner {r['practitioner_id']}")
    for f in facilities:
        if f.get("replaced_by") and f["replaced_by"] not in fids:
            rep.errors.append(f"facility {f['id']} replaced_by unknown {f['replaced_by']}")
        gov = f["address"].get("governorate")
        if gov and f["id"][3:5] != gov[3:] and f["id"][3:5] != "XX":
            rep.warnings.append(f"facility {f['id']} id segment differs from governorate {gov} (ids are immutable)")

    specialties = {r["code"] for r in read_csv("specialties.csv")}
    unknown_spec = Counter(
        s
        for row in (*facilities, *practitioners)
        for s in (row.get("specialties") or [row.get("specialty")])
        if s and s not in specialties
    )
    rep.errors += [f"unknown specialty code {s} ({n}×)" for s, n in unknown_spec.items()]

    live = [f for f in facilities if f.get("status") != "merged"]
    rep.stats = {
        "facilities": len(live),
        "facilities_by_type": dict(Counter(f["type"] for f in live).most_common()),
        "facilities_by_governorate": dict(
            Counter(f["address"].get("governorate") or "unknown" for f in live).most_common()
        ),
        "facilities_by_ownership": dict(Counter(f["ownership"] for f in live).most_common()),
        "with_location": sum(1 for f in live if f.get("location")),
        "with_phone": sum(1 for f in live if f["contact"].get("phones")),
        "with_hours": sum(1 for f in live if f["hours"].get("opening_hours") or f["hours"].get("text")),
        "with_insurance": sum(1 for f in live if f.get("insurance")),
        "practitioners": len(practitioners),
        "practitioners_with_role": len({r["practitioner_id"] for r in roles}),
        "roles": len(roles),
        "mean_completeness": round(sum(f["quality"]["completeness"] for f in live) / max(len(live), 1), 3),
    }
    return rep
