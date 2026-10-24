# Data model

Cura models healthcare the way HL7 **FHIR R4** does, so the registry can be exposed as FHIR later
without a redesign. Three record types, joined by IDs:

```
Facility  ≈ FHIR Organization + Location     data/facilities.jsonl
Practitioner ≈ FHIR Practitioner              data/practitioners.jsonl
Role      ≈ FHIR PractitionerRole             data/roles.jsonl   (practitioner × facility)
```

A doctor who works at two hospitals and a private clinic is **one** practitioner with **three** roles.
Schedules and fees live on the role, because they differ per place.

The authoritative definition is the Pydantic model in [`src/cura/model.py`](../src/cura/model.py); JSON
Schemas generated from it are in [`schema/`](../schema).

## Unknown is not "no"

| value | meaning |
|---|---|
| `null` | **unknown** — no source has stated it |
| `[]` | a source stated there are none |
| `false` | a source stated it is not the case |

`emergency: null` means "we don't know whether there is an emergency department" — an app must not
render that as "no ER". Ratings, busy hours and prices start `null` and fill as data arrives.

## Facility

| field | notes |
|---|---|
| `id` | see [identifiers](identifiers.md) |
| `status` | `active`, `temporarily_closed`, `permanently_closed`, `merged` (tombstone → `replaced_by`), `unknown` |
| `type` | [`facility_types.csv`](../data/reference/facility_types.csv) — hospital, clinic, primary_care_center, doctor_office, dental_clinic, pharmacy, laboratory, imaging_center, blood_bank, rehabilitation, optician, dialysis_center, emergency_medical_service, other |
| `ownership` | [`ownership.csv`](../data/reference/ownership.csv) — moh, rms (Royal Medical Services), university, other_gov, private, ngo, unrwa, unknown. Government vs private is `ownership.csv:sector`. |
| `name` | `{en, ar, aliases[]}` — every spelling seen in any source is kept in `aliases` for search |
| `address` | `governorate` (ISO 3166-2:JO, e.g. `JO-AM`), `district` (liwa, from coordinates), `city`, `neighborhood`, `street`, `building`, `floor`, `postcode`, `text` (verbatim) |
| `location` | WGS84 `lat`/`lon`, `precision` (`exact` = surveyed point, `approximate` = city-level), `source` |
| `contact` | `phones` (E.164, `+9626…`), `emails`, `website` |
| `hours` | `opening_hours` in [OSM syntax](https://wiki.openstreetmap.org/wiki/Key:opening_hours), parsed `weekly` spans, `always_open`, `text` when unparseable |
| `emergency`, `beds` | |
| `specialties` | codes from [`specialties.csv`](../data/reference/specialties.csv), derived from departments and source tags |
| `departments`, `services` | free text as published |
| `equipment` | `{code, name, count, available}` — code from [`equipment.csv`](../data/reference/equipment.csv) (MRI, CT, PET-CT, cath lab, LINAC …) |
| `insurance` | `{insurer, network, source}` — insurer code from `insurers.csv` |
| `accreditations` | `{body, status, year}` — JCI, HCAC, CAP, ISO 15189 … |
| `prices` | `{item, category, price | min/max, effective_from, source}` |
| `amenities` | `wheelchair`, `parking`, `delivery`, `home_visits` … tri-state |
| `rating` | aggregate of Cura's own reviews: `overall`, `count`, per-dimension means ([`review_dimensions.csv`](../data/reference/review_dimensions.csv)) |
| `busy_hours` | `{day, hour, level 0–1}` — reserved; filled from check-ins/reviews, never scraped |
| `identifiers` | crosswalk to every source ID: `{"osm": "way/123", "wikidata": "Q…", "moh_statbook_2024": "…"}` |
| `sources` | provenance: `{source, ref, url, retrieved_at}` per contributing record |
| `quality` | `completeness` (share of 10 core fields known), `confidence` (corroboration), `last_verified_at` |

## Practitioner

`name`, `title`, `grade` (consultant / specialist / resident / general_practitioner / unknown),
`specialty` (code), `subspecialties` (free text), `gender` (**only when a source states it** — never
inferred from a name), `languages`, `qualifications`, `license_verified` (set when a moderator approves
the doctor's own claim), `rating`, `identifiers`, `sources`, `quality`.

Personal data is limited to what the practitioner's employer or the practitioner publishes
professionally. No personal phone numbers, home addresses, national IDs or photos.

## Role

`practitioner_id`, `facility_id`, `specialty` at that place, `hours`, `consultation_fee` (`{amount,
currency}`), `insurance`, `phones` (clinic), `profile_url`, `active`.

## Reference data

`data/reference/*.csv` — governorates, facility types, ownership, specialties (EN/AR + aliases),
equipment, insurers, public holidays, review dimensions. `data/prices.jsonl` holds published price
references (Jordan Medical Association fee schedule, MoH treatment fees) with their effective year.

## How sources become records

```
sources/staging/*.jsonl ─normalise─▶ candidates ─block + link─▶ clusters ─fuse─▶ data/*.jsonl
                                                     ▲
                       previous data/*.jsonl ────────┘ identifier crosswalk keeps IDs stable
```

**Normalisation** — Unicode NFC; Arabic letter-form folding (أ/إ/آ→ا, ى→ي, ة→ه, diacritics and
tatweel removed, definite article stripped); E.164 phones via libphonenumber; URLs canonicalised;
governorate from coordinates (point-in-polygon against geoBoundaries ADM1/ADM2) or, failing that, from
the stated address.

**Entity resolution** (precision-first) — candidates are blocked by shared name tokens, phone, website
domain or prior ID, then linked when types are compatible and:

* both have coordinates: ≤ 1.2 km (hospitals) / 250 m (others) apart **and** name similarity ≥ 85, or
  ≤ 60 m and ≥ 70;
* otherwise: same governorate and, for hospitals, similarity ≥ 90 (or ≥ 70 with a shared phone or
  domain); for others ≥ 95 plus a shared phone/domain/city; MoH health centres (named after their
  village) ≥ 97.

Name similarity is RapidFuzz token-set/sort ratio over EN and AR keys with boilerplate removed
("hospital", "مستشفى", "medical center" …). Two records from the same non-OSM source never merge —
a directory listing a name twice means two branches.

Practitioners merge only on an identical normalised full name plus a compatible specialty; two-token
names ("Ahmad Ali") only merge within the same facility, because they collide across a country.

**Fusion** — each source has a trust prior (`merge.trust`: official ministry lists 0.95, a facility's
own site 0.9, insurer networks 0.75, OSM 0.7, Wikidata 0.6, the LLM agent 0.6, verified API edits 0.97).
Scalar fields take the most trusted non-null value; coordinates prefer surveyed points (OSM, API);
lists are unioned and de-duplicated. Every contributing record stays in `sources`.

**Confidence** = `min(1, 0.35 + 0.15 × independent source families + 0.1 if edited by a verified owner)`.
A record seen only in OSM sits at 0.5; corroborated by the ministry and an insurer, it reaches 0.8.
