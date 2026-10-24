# Cura

**An open registry of every healthcare facility, doctor and service in Jordan: a clean, versioned
dataset plus an API that apps can read from and write to.**

Hospitals, clinics, doctors' offices, pharmacies, labs, imaging centres, MoH health centres and the
people who work in them. Each record has coordinates, contacts, hours, specialties, equipment,
insurance accepted and prices where known, and a provenance trail for every fact. Bilingual
(Arabic/English) throughout.

Current coverage is in [`data/STATS.md`](data/STATS.md), regenerated on every build.

```
┌───────────────── sources ──────────────────┐    ┌──────── pipeline ────────┐    ┌──── outputs ────┐
│ MoH registers · MoH statistical book 2024  │    │ normalise (AR/EN, E.164, │    │ data/*.jsonl    │
│ RMS · HCAC · Private Hospitals Assoc.      │──▶ │ geocode, opening hours)  │──▶ │ (canonical, git)│
│ hospital doctor directories · insurer      │    │ entity resolution        │    │ CSV · GeoJSON · │
│ networks · OSM · Wikidata · JMA fees       │    │ trust-weighted fusion    │    │ SQLite · API    │
└────────────────────────────────────────────┘    │ stable IDs · validation  │    └────────┬────────┘
        ▲                                         └──────────────────────────┘             │
        │  open-weight LLM agent re-reads facility sites weekly (evidence-checked)         │
        └───────────── verified doctors & facility admins edit through the API ◀───────────┘
```

## Use the data

| you want | use |
|---|---|
| the whole dataset | `data/facilities.jsonl`, `data/practitioners.jsonl`, `data/roles.jsonl` (one JSON record per line) |
| a spreadsheet / a map / SQL | `uv run cura export` → `dist/*.csv`, `dist/facilities.geojson`, `dist/cura.sqlite` |
| live queries from an app | the API, see [docs/api.md](docs/api.md) |
| the schema | [`schema/*.schema.json`](schema) (JSON Schema 2020-12) and [docs/data-model.md](docs/data-model.md) |

```bash
curl "$CURA/v1/facilities?near=31.9539,35.9106&radius_km=2&type=pharmacy&open_now=true"
curl "$CURA/v1/facilities?type=hospital&specialty=cardiology&insurer=mednet&governorate=AM"
curl "$CURA/v1/practitioners?q=خالد&specialty=pediatrics"
```

`null` means **unknown**, never "no". See [the data model](docs/data-model.md#unknown-is-not-no).

## Run it

```bash
uv sync --extra dev
uv run pytest                       # unit + end-to-end API tests
uv run cura merge                   # sources/staging → data/
uv run cura validate                # schema + referential integrity
uv run cura db seed && uv run cura api   # http://127.0.0.1:8000/docs
```

Production-like stack (Postgres + API): `docker compose up -d --build`, then
`docker compose exec api cura db seed`.

## How it stays clean

* **Provenance on every record**: which sources said it, when, and from which URL.
* **Precision-first entity resolution**: Arabic letter-form folding, boilerplate-aware fuzzy
  matching, distance gates, conservative person matching that understands Arabic name chains.
  ([details](docs/data-model.md#how-sources-become-records))
* **Stable, meaningful IDs** (`JO-AM-HSP-00042`) that never change or get reused
  ([rules](docs/identifiers.md)).
* **Trust-weighted fusion**: ministry lists outrank a hospital's own site, which outranks insurer
  networks, OSM, Wikidata and the LLM agent.
* **CI**: lint, tests, schema validation, and a check that the committed dataset rebuilds exactly
  from the committed sources.

## How it stays current

* **Scripted collectors** (OSM, Wikidata) run daily and open a PR ([refresh.yml](.github/workflows/refresh.yml)).
* **The refresh agent** ([docs/agent.md](docs/agent.md)) runs an open-weight model (Qwen2.5 / Llama 3.1
  via any OpenAI-compatible endpoint) over facilities' own websites every week. It keeps a value only
  when the model quotes the page verbatim. Its output is a proposal, never a direct write.
* **People**: doctors and facility admins claim their records and update schedules, fees and
  insurance through any app built on the API. Their edits flow back into the open dataset with
  attribution ([governance](docs/governance.md)).

## Repository layout

```
data/                canonical dataset (generated: do not hand-edit) + reference vocabularies
  reference/         governorates, facility types, specialties, insurers, equipment, holidays …
sources/staging/     one JSONL + meta per source: the only input to the merge
schema/              generated JSON Schemas
src/cura/
  collectors/        scripted sources (osm, wikidata)
  pipeline/          merge (entity resolution + fusion), validate, export, stats
  api/               FastAPI app: registry, OAuth 2.0 server, contributions, moderation
  agent/             LLM refresh agent
docs/                data model, identifiers, API, governance, agent
```

## License

* **Data** (`data/`, `sources/`): [ODbL-1.0](DATA_LICENSE). Attribute "Cura contributors,
  OpenStreetMap contributors and the listed sources".
* **Code**: [Apache-2.0](LICENSE).

Built by [NextMove](https://nextmoveinternational.com) in Amman.
