"""Embedding models.

Production uses an open-weight multilingual model behind any OpenAI-compatible `/embeddings` endpoint —
the default is **BGE-M3** (1024 dimensions, strong on Arabic and English, MIT licence), served locally
by Ollama or vLLM/TEI, or by a hosted provider. Nothing about the registry depends on one vendor.

`HashingEmbedder` is a deterministic, dependency-free stand-in (character n-grams hashed into the same
1024 dimensions). It is what tests run on and what an offline laptop can use; it matches spellings,
not meanings, and says so in the API's `mode`.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import httpx
import numpy as np

from cura import normalize as nz
from cura.api.db import EMBEDDING_DIM
from cura.api.settings import settings


class Embedder(Protocol):
    model: str
    semantic: bool

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray: ...


def _unit(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.where(norms == 0, 1, norms)).astype(np.float32)


class HashingEmbedder:
    model = "hashing-ngram-v1"
    semantic = False

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in nz.match_key(text, drop=set()).split():
                padded = f" {token} "
                for n in (3, 4):
                    for j in range(max(1, len(padded) - n + 1)):
                        h = int.from_bytes(
                            hashlib.blake2b(padded[j : j + n].encode(), digest_size=8).digest(), "little"
                        )
                        out[i, h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        return _unit(out)


class OpenAICompatibleEmbedder:
    semantic = True

    def __init__(self, base_url: str, model: str, api_key: str | None, *, timeout: float = 60.0):
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
        )
        s = settings()
        self._prefix = {True: s.embed_query_prefix, False: s.embed_document_prefix}

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        prefix = self._prefix[query]
        resp = self._client.post("/embeddings", json={"model": self.model, "input": [prefix + t for t in texts]})
        resp.raise_for_status()
        rows = sorted(resp.json()["data"], key=lambda r: r["index"])
        m = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        if m.shape[1] != EMBEDDING_DIM:
            raise RuntimeError(
                f"{self.model} returns {m.shape[1]} dimensions; the index is {EMBEDDING_DIM}. "
                "Use a 1024-dimension model or add a migration that retypes embeddings.vector."
            )
        return _unit(m)


def from_settings() -> Embedder | None:
    s = settings()
    if s.embed_provider == "openai":
        return OpenAICompatibleEmbedder(s.embed_base_url, s.embed_model, s.embed_api_key, timeout=s.embed_timeout_s)
    if s.embed_provider == "hashing":
        return HashingEmbedder()
    return None
