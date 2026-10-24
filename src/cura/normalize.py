"""Normalisation primitives: text, Arabic, names, phones, URLs, opening hours, governorates.

Matching keys produced here are lossy on purpose (they exist to compare, never to display).
"""

from __future__ import annotations

import csv
import re
import unicodedata
from functools import cache
from urllib.parse import urlsplit

import phonenumbers

from cura.paths import REFERENCE_DIR

_WS = re.compile(r"\s+")
_ARABIC = re.compile(r"[؀-ۿ]")
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # harakat + tatweel
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# Words that describe the *kind* of place, not *which* place. Removing them before fuzzy matching
# stops "Al Amal Hospital" matching "Al Amal Pharmacy" purely on shared boilerplate.
GENERIC = {
    "hospital",
    "hospitals",
    "clinic",
    "clinics",
    "center",
    "centre",
    "medical",
    "health",
    "healthcare",
    "pharmacy",
    "pharmacies",
    "lab",
    "labs",
    "laboratory",
    "laboratories",
    "dental",
    "dentist",
    "the",
    "and",
    "of",
    "for",
    "al",
    "el",
    "complex",
    "polyclinic",
    "specialized",
    "specialised",
    "specialty",
    "branch",
    "dr",
    "doctor",
    "group",
    "co",
    "company",
    "llc",
    "ltd",
    "jordan",
    "jordanian",
    "مستشفى",
    "مستشفي",
    "مشفى",
    "عياده",
    "عيادات",
    "مركز",
    "مراكز",
    "طبي",
    "الطبي",
    "الطبيه",
    "طبيه",
    "صيدليه",
    "مختبر",
    "مختبرات",
    "مجمع",
    "د",
    "دكتور",
    "الدكتور",
    "فرع",
    "و",
    "ل",
    "الصحي",
    "صحي",
    "التخصصي",
    "تخصصي",
    "الاردن",
    "الاردني",
}
TITLES = {
    "dr",
    "doctor",
    "prof",
    "professor",
    "assoc",
    "assist",
    "assistant",
    "associate",
    "mr",
    "mrs",
    "ms",
    "miss",
    "consultant",
    "specialist",
    "md",
    "phd",
    "frcs",
    "mrcp",
    "د",
    "دكتور",
    "الدكتور",
    "الدكتوره",
    "دكتوره",
    "ا",
    "أ",
    "اد",
    "أد",
    "بروفيسور",
    "استشاري",
    "اخصائي",
    "الاستشاري",
    "الاخصائي",
    "السيد",
    "السيده",
}


def clean(value: object) -> str | None:
    if value is None:
        return None
    text = _WS.sub(" ", unicodedata.normalize("NFC", str(value)).replace("_", " ")).strip(" \t\n\r-–—,;|")
    return text or None


def is_arabic(text: str | None) -> bool:
    return bool(text and _ARABIC.search(text))


def normalize_arabic(text: str) -> str:
    text = _DIACRITICS.sub("", text)
    return (
        text.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
        .replace("ؤ", "و")
        .replace("ئ", "ي")
    )


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c) or _ARABIC.match(c))
    text = normalize_arabic(text.lower())
    text = _NON_WORD.sub(" ", text).replace("_", " ")
    out = []
    for tok in text.split():
        # Arabic definite article: "المستشفى" → "مستشفى", but leave short words alone.
        if tok.startswith("ال") and len(tok) > 4:
            tok = tok[2:]
        out.append(tok)
    return out


def match_key(text: str | None, drop: set[str] = GENERIC) -> str:
    """Order-insensitive comparison key with boilerplate words removed."""
    if not text:
        return ""
    stripped = {normalize_arabic(w) for w in drop} | {normalize_arabic(w[2:]) for w in drop if w.startswith("ال")}
    tokens = _tokens(text)
    kept = [t for t in tokens if t not in stripped]
    # "Jordan Hospital" is all boilerplate — then the boilerplate *is* the name.
    return " ".join(kept or tokens)


PLACEHOLDER = {
    "hospital",
    "pharmacy",
    "clinic",
    "health",
    "center",
    "centre",
    "medical",
    "the",
    "project",
    "site",
    "post",
    "dental",
    "lab",
    "laboratory",
    "doctor",
    "dr",
    "primary",
    "مستشفى",
    "صيدليه",
    "صيدلية",
    "عياده",
    "العياده",
    "مركز",
    "صحي",
    "الصحي",
    "طبي",
    "الطبي",
    "مختبر",
    "د",
}


def is_placeholder_name(text: str | None) -> bool:
    """True for names that only say what kind of place it is ("hospital", "مركز صحي", "Health Post")."""
    toks = [t for t in _tokens(text or "") if not t.isdigit()]
    words = {normalize_arabic(w) for w in PLACEHOLDER} | {
        normalize_arabic(w)[2:] for w in PLACEHOLDER if w.startswith("ال")
    }
    return all(t in words for t in toks)


# Name words that declare a facility type. Used only to correct OSM's known `amenity=hospital`
# over-tagging, never to invent a type for an untyped record.
_NAME_TYPES = (
    ("pharmacy", ("pharmacy", "pharma", "صيدليه", "صيدليات")),
    ("dental_clinic", ("dental", "dentist", "orthodont", "اسنان", "الاسنان")),
    ("optician", ("optic", "optical", "بصريات", "نظارات")),
    ("laboratory", ("laboratory", "laboratories", "lab", "labs", "مختبر", "مختبرات")),
    (
        "primary_care_center",
        ("health center", "health centre", "health post", "مركز صحي", "مركز الصحي", "الصحي الشامل"),
    ),
    ("clinic", ("medical center", "medical centre", "clinic", "polyclinic", "مركز طبي", "عياده", "عيادات", "مجمع")),
)


NON_HEALTH = (
    "مدرسه",
    "مدرسة",
    "بلديه",
    "بلدية",
    "منزل",
    "اراضي",
    "أراضي",
    "مسجد",
    "حذاء",
    "school",
    "mosque",
    "municipality",
    "project site",
)


def names_non_health(text: str | None) -> bool:
    """OSM noise: schools, houses, land plots tagged with a health key."""
    low = normalize_arabic((text or "").lower())
    return any(normalize_arabic(w) in low for w in NON_HEALTH)


def type_declared_by_name(text: str | None) -> str | None:
    if not text:
        return None
    norm = " " + " ".join(_tokens(text)) + " "
    raw = " " + normalize_arabic(text.lower()) + " "
    if any(w in norm for w in (" hospital ", " مستشفي ", " مستشفى ")) or "مستشف" in raw or "hospital" in raw:
        return "hospital"
    for ftype, words in _NAME_TYPES:
        for w in words:
            w_norm = " ".join(_tokens(w))
            if f" {w_norm} " in norm or (w_norm in norm and len(w_norm) > 6):
                return ftype
    return None


_TITLE = re.compile(
    r"^\s*(assoc\.?\s*prof\.?|prof(?:essor)?(?:\.|(?=\s))|dr(?:\.|-|(?=\s))|doctor(?=\s)|أ\.\s*د\.?|ا\.\s*د\.?|د\.|د\s"
    r"|الدكتور[ةه]?|دكتور[ةه]?)\s*",
    re.IGNORECASE,
)


def strip_title(name: str | None) -> tuple[str | None, str | None]:
    """'DR. ALI ABU AWWAD' → ('Ali Abu Awwad', 'Dr.'); 'الدكتور عيسى' → ('عيسى', 'Dr.')."""
    if not name:
        return name, None
    title = None
    if m := _TITLE.match(name):
        word = m[1].lower()
        title = "Prof." if word.startswith(("prof", "أ", "ا.")) or "prof" in word else "Dr."
        name = name[m.end() :].strip(" .-")
    if name and not is_arabic(name) and name.isupper():
        name = name.title()
    return (name or None), title


_ABD = re.compile(r"\b(عبد|ابو|أبو)\s+(?=ال)")


def person_key(text: str | None) -> str:
    # "عبد الله" and "عبدالله" are the same name written two ways.
    return match_key(_ABD.sub(r"\1", text) if text else text, drop=TITLES)


# --- contact -----------------------------------------------------------------------------------


def phone_e164(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    raw = re.split(r"\b(?:ext|x|ext\.|فرعي)\b", raw, flags=re.I)[0]
    try:
        num = phonenumbers.parse(raw, "JO")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        # Jordanian short codes (e.g. 911, call-centre numbers like 06-5xxxxxx typos) are dropped.
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


@cache
def _area_codes() -> dict[str, str]:
    return {row["iso_3166_2"]: row["landline_prefix"] for row in _reference("governorates.csv")}


def phones(values: list[str] | str | None, governorate: str | None = None) -> list[str]:
    """E.164 phones. Landlines printed without their area code ("5608080") are completed from the
    stated governorate: Jordan's area codes follow it (06 Amman, 05 centre, 02 north, 03 south)."""
    if not values:
        return []
    if isinstance(values, str):
        values = re.split(r"[;,/|]|\s{2,}|\bor\b|\bأو\b", values)
    area = _area_codes().get(governorate or "")
    out: list[str] = []
    for v in values:
        for part in re.split(r"[;,/|]", str(v)):
            part = part.strip()
            digits = re.sub(r"\D", "", part.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
            if area and len(digits) == 7 and digits[0] in "2345678":
                part = area + digits
            if (p := phone_e164(part)) and p not in out:
                out.append(p)
    return out


def website(url: str | None) -> str | None:
    url = clean(url)
    if not url or " " in url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if "." not in parts.netloc:
        return None
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def domain(url: str | None) -> str | None:
    if not (url := website(url)):
        return None
    host = urlsplit(url).netloc
    host = host.removeprefix("www.")
    # Social/link-in-bio hosts identify nothing about the facility.
    if host in {"facebook.com", "m.facebook.com", "instagram.com", "linktr.ee", "wa.me", "goo.gl", "maps.app.goo.gl"}:
        return None
    return host


def email(value: str | None) -> str | None:
    value = clean(value)
    if value and re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", value):
        return value.lower()
    return None


# --- opening hours -----------------------------------------------------------------------------

_DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]
_RULE = re.compile(
    r"^(?P<days>[A-Za-z]{2}(?:\s*-\s*[A-Za-z]{2})?(?:\s*,\s*[A-Za-z]{2}(?:\s*-\s*[A-Za-z]{2})?)*)\s+"
    r"(?P<times>\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}(?:\s*,\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2})*)$"
)


def _expand_days(spec: str) -> list[str]:
    days: list[str] = []
    for part in spec.split(","):
        part = part.strip().lower()
        if "-" in part:
            a, b = (p.strip() for p in part.split("-"))
            if a not in _DAYS or b not in _DAYS:
                raise ValueError(part)
            i, j = _DAYS.index(a), _DAYS.index(b)
            days += [_DAYS[(i + k) % 7] for k in range((j - i) % 7 + 1)]
        elif part in _DAYS:
            days.append(part)
        else:
            raise ValueError(part)
    return days


def parse_opening_hours(value: str | None) -> dict:
    """Parse the common subset of OSM `opening_hours`. Returns fields for `model.Hours`.

    Anything outside the subset (PH rules, month ranges, comments) keeps the raw string and no
    `weekly` list — partial parses would silently drop rules, which is worse than no parse.
    """
    value = clean(value)
    if not value:
        return {}
    if value.replace(" ", "") in {"24/7", "Mo-Su00:00-24:00", "00:00-24:00"}:
        weekly = [{"day": d, "opens": "00:00", "closes": "24:00"} for d in _DAYS]
        return {"opening_hours": "24/7", "always_open": True, "weekly": weekly}
    weekly: list[dict] = []
    closed: set[str] = set()
    try:
        for rule in (r.strip() for r in value.split(";") if r.strip()):
            if rule.lower().endswith(" off") or rule.lower().endswith(" closed"):
                closed.update(_expand_days(rule.rsplit(" ", 1)[0]))
                continue
            m = _RULE.match(rule)
            if not m:
                raise ValueError(rule)
            for day in _expand_days(m["days"]):
                for span in m["times"].split(","):
                    opens, closes = (t.strip().zfill(5) for t in span.split("-"))
                    weekly.append({"day": day, "opens": opens, "closes": closes})
    except ValueError:
        return {"opening_hours": value}
    weekly = [w for w in weekly if w["day"] not in closed]
    weekly.sort(key=lambda w: (_DAYS.index(w["day"]), w["opens"]))
    return {"opening_hours": value, "weekly": weekly, "always_open": False}


# --- reference lookups -------------------------------------------------------------------------


@cache
def _reference(name: str) -> list[dict[str, str]]:
    with (REFERENCE_DIR / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@cache
def _governorate_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    extra = {
        "JO-AM": ["capital", "al asimah", "العاصمه", "عمان", "amman governorate"],
        "JO-AZ": ["zarka", "az zarqa", "الزرقاء", "رصيفه", "russeifa"],
        "JO-IR": ["irbed", "arbid", "اربد"],
        "JO-BA": ["salt", "as salt", "al balqa", "السلط", "البلقاء"],
        "JO-MN": ["maan", "ma an", "معان", "petra", "wadi musa", "البتراء"],
        "JO-AT": ["tafila", "tafielah", "at tafilah", "الطفيله"],
        "JO-AJ": ["ajlun", "ajlune", "عجلون"],
        "JO-AQ": ["akaba", "al aqaba", "العقبه"],
        "JO-KA": ["kerak", "al karak", "الكرك"],
        "JO-MA": ["mafraq", "al mafraq", "المفرق"],
        "JO-JA": ["jarash", "جرش"],
        "JO-MD": ["madaba", "مادبا", "مأدبا"],
    }
    for row in _reference("governorates.csv"):
        iso = row["iso_3166_2"]
        for name in [
            row["code"],
            iso,
            row["name_en"],
            row["name_ar"],
            row["capital_en"],
            row["capital_ar"],
            *extra[iso],
        ]:
            aliases[" ".join(_tokens(name))] = iso
    return aliases


def governorate(value: str | None) -> str | None:
    value = clean(value)
    if not value:
        return None
    if re.fullmatch(r"JO-[A-Z]{2}", value.upper()):
        valid = set(_governorate_aliases().values())
        # Common non-ISO codes seen in the wild (JO-ZA for Zarqa, JO-TA for Tafilah).
        value = {"JO-ZA": "JO-AZ", "JO-TA": "JO-AT", "JO-AM": "JO-AM"}.get(value.upper(), value.upper())
        return value if value in valid else None
    key = " ".join(_tokens(value))
    aliases = _governorate_aliases()
    if key in aliases:
        return aliases[key]
    # "Amman - Shmeisani", "إربد / الحصن": try each token run.
    for tok in key.split():
        if tok in aliases:
            return aliases[tok]
    return None
