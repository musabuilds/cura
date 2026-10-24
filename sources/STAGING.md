# Staging format

Every collector — scripted or human/agent-assisted — writes **staging records**, never the canonical
dataset directly. The pipeline (`cura pipeline merge`) normalizes, geocodes, deduplicates and assigns
identifiers. Keeping collection and canonicalisation apart means a bad source can be dropped and the
dataset rebuilt without it.

## Files

```
sources/staging/<source_slug>.jsonl      one JSON object per line, UTF-8, no BOM
sources/staging/<source_slug>.meta.json  provenance for the whole file
sources/raw/<source_slug>/...            optional: the raw pages/PDFs/JSON the records came from
```

`<source_slug>` is lowercase `a-z0-9_`, e.g. `osm`, `moh_statbook_2023`, `jordanhospital_doctors`.

### `.meta.json`

```json
{
  "source": "jordanhospital_doctors",
  "title": "Jordan Hospital — Find a Doctor directory",
  "publisher": "Jordan Hospital",
  "urls": ["https://..."],
  "retrieved_at": "2026-10-24T12:00:00Z",
  "method": "scrape | api | pdf_extract | manual",
  "license": "ODbL-1.0 | CC-BY-4.0 | public-web (facts) | unknown",
  "robots_respected": true,
  "record_count": 123,
  "notes": "anything a maintainer must know: gaps, caveats, parsing assumptions"
}
```

## Record envelope

```json
{"kind": "facility", "source": "osm", "source_ref": "node/123456", "source_url": "https://...",
 "retrieved_at": "2026-10-24T12:00:00Z", "data": { ... }}
```

| field | required | meaning |
|---|---|---|
| `kind` | yes | `facility`, `practitioner`, `insurer`, `price`, `holiday`, `specialty` |
| `source` | yes | the source slug |
| `source_ref` | no | the record's id inside the source (stable if the source has one) |
| `source_url` | no | the page the fact was read from |
| `retrieved_at` | yes | ISO-8601 UTC |
| `data` | yes | kind-specific payload below |

## Rules — read these before collecting anything

1. **Never guess.** A field you did not read from the source is omitted (or `null`). Do not infer
   gender from a name, a governorate from a phone prefix, or a specialty from a department. `unknown`
   is a valid and honest answer; a wrong value poisons every app built on top.
2. **Keep the source's spelling** in `name_en`/`name_ar`. Normalisation happens downstream.
3. **Bilingual.** Fill `name_ar` whenever the source shows Arabic.
4. **Professional data only.** Clinic phones, hospital emails, published schedules and fees. Never a
   practitioner's personal mobile, home address, national ID or photo of a patient.
5. **Respect robots.txt and terms.** No scraping of sites whose terms forbid it (Google Maps, Vezeeta,
   Altibbi, Tebcan, Facebook). Rate-limit to ≤1 request/second per host.
6. Money is `{"amount": 25, "currency": "JOD"}`. Never a bare number.
7. Dates are ISO-8601. Times are 24h `HH:MM` local (Asia/Amman).

## `data` by kind

### facility

```jsonc
{
  "name_en": "Jordan Hospital", "name_ar": "المستشفى الأردني",
  "type": "hospital",              // see data/reference/facility_types.csv (hospital, clinic,
                                   // primary_care_center, doctor_office, dental_clinic, pharmacy,
                                   // laboratory, imaging_center, blood_bank, rehabilitation,
                                   // optician, dialysis_center, emergency_medical_service, other)
  "ownership": "private",          // moh | rms | university | private | ngo | unrwa | other_gov | unknown
  "governorate": "Amman",          // name or ISO 3166-2 code (JO-AM); omit if not stated
  "city": "Amman", "district": "Shmeisani", "street": "Queen Noor St", "building": "12", "floor": "3",
  "address": "free-text address exactly as published",
  "postcode": "11118",
  "lat": 31.97, "lon": 35.9,       // only from the source; never geocode by hand
  "phones": ["+962 6 560 8080"], "emails": [], "website": "https://...",
  "opening_hours": "Mo-Su 00:00-24:00", // OSM opening_hours syntax if possible, else free text
  "emergency": true, "beds": 300,
  "departments": ["Cardiology", "..."],
  "services": ["Cardiac catheterization", "..."],
  "equipment": ["MRI 3T", "CT 128-slice"],
  "insurance_accepted": ["MedNet", "NatHealth"],
  "accreditations": [{"body": "JCI", "status": "accredited", "year": 2023}],
  "languages": ["ar", "en"],
  "parent": "name of parent organisation / chain if any",
  "extra": {}                      // anything else useful, keyed in snake_case
}
```

### practitioner

```jsonc
{
  "name_en": "Dr. Ahmad Example", "name_ar": "د. أحمد مثال",
  "title": "Dr.",                  // Dr. | Prof. | Assoc. Prof. | ...
  "grade": "consultant",           // consultant | specialist | resident | general_practitioner | unknown
  "specialty": "Cardiology", "subspecialties": ["Interventional cardiology"],
  "gender": "male",                // ONLY if the source states it (e.g. a gender filter); else omit
  "languages": ["ar", "en"],
  "qualifications": ["MD, University of Jordan", "Jordanian Board"],
  "facility_name": "Jordan Hospital", // as written on the source
  "facility_website": "https://www.jordanhospital.com",
  "schedule": "Sun-Thu 10:00-14:00",  // as published
  "consultation_fee": {"amount": 30, "currency": "JOD"},
  "clinic_phones": [], "profile_url": "https://...",
  // A doctor listed with their *own* clinic (licence registers, insurer networks): give the clinic's
  // address. The merge turns it into a doctor_office facility and links the doctor to it.
  "address": "as published", "governorate": "JO-AM", "city": "...", "lat": null, "lon": null,
  "insurance_accepted": ["MedNet"]
}
```

Landlines printed without an area code are completed from `governorate` by the pipeline; leave
them as printed.

### insurer

`{"name_en", "name_ar", "kind": "public|private|tpa|military|unrwa", "website", "phones", "network_url", "notes"}`

### price

`{"item": "Specialist consultation", "item_ar", "category": "consultation|procedure|test|imaging|bed_day|other",
 "amount": {...} | "min": {...}, "max": {...}, "applies_to": "moh_hospitals|private|jma_schedule|<facility name>",
 "grade": "consultant|specialist|general_practitioner", "effective_from": "2024-01-01", "notes"}`

### holiday

`{"date": "2026-12-25", "name_en", "name_ar", "kind": "public|religious_islamic|religious_christian",
 "observed_by": "all|government|private", "lunar": true}`

### specialty

`{"name_en", "name_ar", "parent": "Internal Medicine", "recognized_by": "Jordan Medical Council"}`
