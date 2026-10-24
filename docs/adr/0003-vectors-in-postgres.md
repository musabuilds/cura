# ADR 0003 — The vector index is pgvector inside the registry's Postgres

**Status:** accepted · 2026-10-24

## Context

We want search that understands meaning ("طبيب قلب في إربد يقبل ميدنت", "a lab near me open now")
and an index that AI assistants can retrieve from. The obvious move is a dedicated vector database
(Pinecone, Weaviate, Qdrant) fed by a copy of the data.

## Decision

Store embeddings in the same Postgres, with **pgvector** (`embeddings` table, `vector(1024)`, HNSW
cosine index). The vector index is *derived* data, never a source of truth:

* the canonical records, their versions and the write policy stay relational and exact — a vector
  store cannot do `If-Match`, merge patches or "the version current on 1 July";
* one transaction boundary, one backup, one access-control model, nothing to keep in sync between two
  systems that can drift;
* freshness without an outbox: rows carry `version`, embeddings carry the `source_version` they were
  computed from; `cura embed sync --watch` embeds what moved and skips the model call when the
  embedded text did not change. A model outage delays search freshness, never a write.

**Model:** BGE-M3 (open weights, MIT, 1024 dims, strong Arabic + English), behind any
OpenAI-compatible `/embeddings` endpoint — Ollama or vLLM/TEI self-hosted, or a hosted provider. No
vendor lock-in. A deterministic hashing embedder covers tests and offline use and reports
`semantic: false`.

**Search** is hybrid: the existing Arabic-folded lexical matcher (exact on names) and vectors (good
on meaning), fused with Reciprocal Rank Fusion. Each result carries both ranks.

## Consequences

* At Cura's size (tens of thousands of records) pgvector HNSW answers in milliseconds; a dedicated
  vector DB would be justified only at tens of millions, and could then be fed from the same
  `embeddings` table.
* Changing to a model with another dimension needs a migration (the column is typed).
* SQLite (tests, laptops) stores vectors as bytes and scans exactly — correct, not fast.
