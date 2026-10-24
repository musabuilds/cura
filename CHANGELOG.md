# Changelog

All notable changes to the code and the dataset. Dataset counts live in `data/STATS.md`.

## [Unreleased]

### Added
- **Live state** ([ADR 0001](docs/adr/0001-live-state-beside-the-record.md)): availability events for
  doctors (everywhere or per workplace) and facilities (open / limited / closed, ER state, affected
  departments), resolved on every read with the schedule; `open_now` honours closures; load samples;
  idempotent writes; an availability change feed.
- **History** ([ADR 0002](docs/adr/0002-nothing-is-overwritten.md)): every version of every record with
  validity intervals, `/history` and `?as_of=`; `/patterns` features (absence by month and weekday,
  seasonal hours, busy profile).
- **Rosters and onboarding** ([ADR 0004](docs/adr/0004-identity-onboarding-and-duplicates.md)): role
  `period` and two-sided `confirmation`; facility admins add, confirm and end roles; one-time
  invitations that let a facility onboard its doctors; candidate matching for doctors and facilities;
  duplicate checks on create; moderator merges replayed by the pipeline
  (`sources/curation/merges.jsonl`); review reports.
- **Semantic search** ([ADR 0003](docs/adr/0003-vectors-in-postgres.md)): pgvector index in the same
  Postgres, BGE-M3 via any OpenAI-compatible endpoint, hybrid lexical + vector ranking (`/v1/search`),
  `cura embed sync`.
- Alembic migrations; production refuses to boot on an unmigrated schema. Sparse fieldsets (`fields=`).
- CI runs the suite on Postgres + pgvector as well as SQLite.

### Changed
- Owner edits that set `hours.opening_hours` re-derive the parsed `weekly` spans.
- `busy_hours` is owner-editable; `confirmation` is system-managed.
- Every role in the dataset now carries `period` and `confirmation`.

### Removed
- The LLM refresh agent and its weekly schedule. Facilities and doctors now keep their own records
  current through the API; scripted, non-AI collectors (OSM, Wikidata) still run daily.

## [0.1.0] — 2026-10-24

### Added
- Canonical FHIR-aligned model: facilities, practitioners, roles; JSON Schemas in `schema/`.
- Stable identifiers `JO-<gov>-<type>-<seq>` with crosswalk-based stability, tombstones and an
  API-reserved range.
- Pipeline: staging format, normalisation (Arabic folding, E.164, OSM opening hours, ADM1/ADM2
  reverse geocoding), precision-first entity resolution, trust-weighted fusion, validation, exports
  (CSV, GeoJSON, SQLite), generated statistics.
- Collectors: OpenStreetMap (Overpass), Wikidata.
- First dataset build from 30+ sources: the MoH licensed-institutions register, MoH Statistical Report
  2024, MoH hospital and health-centre directories, Royal Medical Services, HCAC accreditations, the
  Private Hospitals Association, hospital doctor directories, insurer and TPA provider networks,
  Civil Health Insurance contracted hospitals, lab chains, OSM and Wikidata. Reference data: insurers
  and TPAs (CBJ), JMA fee schedules (2008, 2024), MoH co-payments, public holidays 2026–2027, Jordan
  Medical Council specialties.
- API: open reads with search, filters, geo and open-now; change feed; OAuth 2.0 authorization
  server (code + PKCE, rotating refresh tokens); app registration with API keys; claims; field-level
  write policy; proposals and moderation; reviews by dimension; RFC 9457 errors; ETags.
- Refresh agent: open-weight LLM over OpenAI-compatible endpoints with verbatim-evidence grounding.
- CI (lint, tests, validation, reproducibility) and a scheduled refresh workflow that opens PRs.
