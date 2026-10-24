# Search

`GET /v1/search?q=` — one box over facilities and doctors, Arabic or English, by name or by need.

```bash
curl "$CURA/v1/search?q=طبيب قلب في إربد يقبل ميدنت"
curl "$CURA/v1/search?q=paediatric ER&kind=facility&near=31.95,35.91&radius_km=5"
curl "$CURA/v1/search?q=orthopaedic surgeon&governorate=IR"    # doctors filtered by where they work
```

Each result: `{kind, id, score, ranks: {lexical, semantic}, record, distance_m?}`. The response says
which `mode` ran and whether it was `semantic` (a real embedding model) or not.

## How it works

* **Lexical** — Arabic-folded name tokens (أ/إ/آ→ا, ة→ه, article stripped). Exact on names.
* **Semantic** — each record is embedded as bilingual text describing what it *is*: names, type,
  specialties (expanded to their English and Arabic labels), services, equipment, insurers, place.
  Availability, ratings and hours are deliberately left out, so they never force a re-embed.
* **Fusion** — Reciprocal Rank Fusion of the two rankings.

Vectors live in Postgres via pgvector (HNSW, cosine) — see [ADR 0003](adr/0003-vectors-in-postgres.md).

## Running it

```bash
# Any OpenAI-compatible /embeddings endpoint. Locally, with Ollama:
ollama pull bge-m3
export CURA_EMBED_PROVIDER=openai CURA_EMBED_BASE_URL=http://localhost:11434/v1 CURA_EMBED_MODEL=bge-m3
uv run cura embed sync            # embeds what changed since last time; --rebuild rechecks everything
uv run cura embed sync --watch 30 # keep the index current (docker compose --profile search up)
```

| setting | default | |
|---|---|---|
| `CURA_EMBED_PROVIDER` | `none` | `openai`, `hashing` (offline, spelling-only), `none` (lexical only) |
| `CURA_EMBED_BASE_URL` | `http://localhost:11434/v1` | Ollama, vLLM, TEI or a hosted provider |
| `CURA_EMBED_MODEL` | `bge-m3` | must return 1024 dimensions (or add a migration) |
| `CURA_EMBED_API_KEY` | — | sent as a bearer token |
| `CURA_EMBED_QUERY_PREFIX` / `_DOCUMENT_PREFIX` | empty | for e5-style models (`query: ` / `passage: `) |

Freshness: a record whose `version` moved past its embedding's `source_version` is re-embedded on the
next sync; if its embedded text did not change (an hours edit), only the version is bumped. A
doctor's text includes where they work and refreshes when the doctor record changes or on `--rebuild`.
