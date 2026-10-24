# Contributing

## Setup

```bash
uv sync --extra dev
uv run pytest && uv run cura validate
```

## Adding a data source

1. Read [`sources/STAGING.md`](sources/STAGING.md) — the format and the collection rules.
2. Recurring, scriptable source → a collector in `src/cura/collectors/<slug>.py` with a `run()` that
   calls `write_staging`, registered in `cli.COLLECTORS`. One-off source → commit
   `sources/staging/<slug>.jsonl` + `.meta.json` and describe the method in the meta `notes`.
3. `uv run cura merge && uv run cura validate`, review the diff of `data/`, open a PR.
4. If the source is better or worse than the defaults, set `"trust"` in its meta file and say why.

Never guess a value, never scrape a site whose terms forbid it, never collect personal data that
was not published professionally.

## Fixing a record

Use the API (see [docs/api.md](docs/api.md)) or open an issue with the record ID and a source link.
Do not hand-edit `data/*.jsonl` — it is regenerated from `sources/staging/` and edits are lost.

## Code

* Python ≥ 3.12, `ruff check` + `ruff format` clean, tests for behaviour changes.
* Conventional commits: `feat(api): …`, `fix(merge): …`, `data(osm): …`, `docs: …`.
* `data/` changes and code changes go in separate commits.
