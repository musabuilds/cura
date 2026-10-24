"""Semantic search over the registry.

The vector index sits *beside* the canonical store, in the same Postgres (pgvector), not in a second
database: the records, their versions and the write policy stay relational and exact, and the index
is derived from them and rebuildable at any time. One transaction boundary, one backup, no sync job
between two systems that can drift.

    text.py      what a record "means", as bilingual text (the thing that is embedded)
    embedder.py  the model behind the vectors: any OpenAI-compatible endpoint, or a hashing fallback
    index.py     keeping vectors current (`cura embed sync`) and querying them (hybrid search)
"""
