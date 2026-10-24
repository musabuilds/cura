from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from cura.paths import ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CURA_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{(ROOT / 'var' / 'cura.db').as_posix()}"
    # HS256 signing secret. MUST be overridden in any shared deployment (>= 32 random bytes).
    jwt_secret: str = "dev-only-insecure-secret-change-me-0123456789"
    jwt_issuer: str = "https://api.cura.jo"
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 30 * 24 * 3600
    auth_code_ttl_s: int = 120
    public_base_url: str = "http://127.0.0.1:8000"
    cors_origins: list[str] = ["*"]
    # Requests per minute. Reads are open; a registered app key raises the ceiling.
    rate_anonymous_per_min: int = 60
    rate_app_per_min: int = 600
    environment: str = "development"
    # Semantic search. "openai" = any OpenAI-compatible /embeddings endpoint (Ollama, vLLM, TEI, hosted);
    # "hashing" = offline n-gram stand-in; "none" = lexical search only.
    embed_provider: Literal["none", "hashing", "openai"] = "none"
    embed_base_url: str = "http://localhost:11434/v1"
    embed_model: str = "bge-m3"
    embed_api_key: str | None = None
    embed_timeout_s: float = 60.0
    embed_query_prefix: str = ""  # e5-style models want "query: " / "passage: "; BGE-M3 wants nothing
    embed_document_prefix: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@cache
def settings() -> Settings:
    s = Settings()
    if s.is_production and s.jwt_secret.startswith("dev-only"):
        raise RuntimeError("CURA_JWT_SECRET must be set in production")
    return s
