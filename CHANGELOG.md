# Changelog

All notable changes to the code and the dataset. Dataset counts live in `data/STATS.md`.

## [Unreleased]

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
