"""Minimal client for any OpenAI-compatible chat endpoint serving an open-weight model.

Works unchanged with Ollama (`http://localhost:11434/v1`), vLLM, llama.cpp server, LM Studio, or a
hosted open-model provider (Together, Groq, OpenRouter, HF Inference Endpoints).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = os.environ.get("CURA_LLM_BASE_URL", "http://localhost:11434/v1")
    model: str = os.environ.get("CURA_LLM_MODEL", "qwen2.5:7b-instruct")
    api_key: str = os.environ.get("CURA_LLM_API_KEY", "not-needed-for-local")
    timeout_s: float = float(os.environ.get("CURA_LLM_TIMEOUT", "120"))


class LLM:
    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout_s,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )

    def json(self, system: str, user: str) -> dict:
        """One deterministic call that must return a JSON object."""
        resp = self._client.post(
            "/chat/completions",
            json={
                "model": self.config.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        if start < 0:
            raise ValueError(f"model returned no JSON object: {content[:200]}")
        return json.loads(content[start : end + 1])
