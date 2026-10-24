"""Controlled vocabularies (data/reference/*.csv) and label → code resolution."""

from __future__ import annotations

import csv
import re
from functools import cache

from rapidfuzz import fuzz, process

from cura.normalize import clean, match_key
from cura.paths import REFERENCE_DIR

_NOTHING: set[str] = set()
# Words that describe the doctor's rank or the unit, not the field ("اختصاصي في جراحة العظام").
GRADE_WORDS = {
    "اختصاصي",
    "اخصائي",
    "أخصائي",
    "استشاري",
    "في",
    "consultant",
    "specialist",
    "department",
    "care",
    "clinic",
    "clinics",
    "services",
    "unit",
    "dr",
    "doctor",
    "senior",
    "the",
    "of",
    "in",
}


def slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return text[:48] or "unnamed"


def read_csv(name: str) -> list[dict[str, str]]:
    path = REFERENCE_DIR / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(name: str, rows: list[dict[str, str]], fields: list[str]) -> None:
    with (REFERENCE_DIR / name).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class Vocabulary:
    """Resolves free-text labels (EN or AR, any spelling) to a reference code."""

    def __init__(self, rows: list[dict[str, str]], label_fields: tuple[str, ...] = ("name_en", "name_ar")):
        self.rows = {r["code"]: r for r in rows}
        self._index: dict[str, str] = {}
        for row in rows:
            labels = [row["code"].replace("_", " ")] + [row.get(f, "") for f in label_fields]
            labels += (row.get("aliases") or "").split(";")
            for label in labels:
                if key := match_key(label, drop=_NOTHING):
                    self._index.setdefault(key, row["code"])
        self._keys = list(self._index)

    def resolve(self, label: str | None, threshold: int = 92) -> str | None:
        key = match_key(clean(label), drop=_NOTHING)
        if not key:
            return None
        for candidate in (key, match_key(key, drop=GRADE_WORDS)):
            if candidate in self._index:
                return self._index[candidate]
            hit = process.extractOne(candidate, self._keys, scorer=fuzz.ratio, score_cutoff=threshold)
            if hit:
                return self._index[hit[0]]
        # "Internal Medicine and Oncology", "Neurology and Neurosurgery": the first named field.
        for part in re.split(r"\s+(?:and|و)\s+|\s*[/&,]\s*", clean(label) or ""):
            if part and part != label and (code := self.resolve(part, threshold)):
                return code
        return None

    def resolve_contains(self, label: str | None) -> str | None:
        """For labels like 'MRI 1.5 Tesla' — exact/fuzzy first, then whole-word alias containment."""
        if code := self.resolve(label):
            return code
        key = f" {match_key(clean(label), drop=_NOTHING)} "
        best: tuple[int, str] | None = None
        for alias, code in self._index.items():
            if f" {alias} " in key and (best is None or len(alias) > best[0]):
                best = (len(alias), code)
        return best[1] if best else None


@cache
def specialties() -> Vocabulary:
    return Vocabulary(read_csv("specialties.csv"))


@cache
def equipment() -> Vocabulary:
    return Vocabulary(read_csv("equipment.csv"))


@cache
def insurers() -> Vocabulary:
    return Vocabulary(read_csv("insurers.csv"))


def reset_caches() -> None:
    for fn in (specialties, equipment, insurers):
        fn.cache_clear()
