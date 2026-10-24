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
    # Phone sign-in. Codes go through a pluggable sender: "console" logs them (development only),
    # "twilio" sends SMS / places a voice call.
    sms_provider: Literal["console", "twilio"] = "console"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    email_provider: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Cura <no-reply@cura.jo>"
    # Demo accounts for pitches and app development: comma-separated numbers that accept `demo_otp`.
    # Ignored whenever environment == "production", whatever they are set to.
    demo_phones: str = ""
    demo_otp: str = "246810"
    # Fernet key (urlsafe base64, 32 bytes) for secrets Cura must read back (webhook signing secrets).
    # Derived from jwt_secret when unset.
    encryption_key: str | None = None
    # Webhook delivery and change fan-out run on background threads; tests run them inline.
    background_workers: bool = True
    max_body_bytes: int = 1_000_000
    sse_max_connections: int = 500
    sse_max_per_ip: int = 20

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def demo_numbers(self) -> set[str]:
        """Normalised demo numbers, or nothing in production (hard off, not a default)."""
        if self.is_production or not self.demo_phones.strip():
            return set()
        from cura import normalize as nz

        return {e for e in (nz.phone_e164(p.strip()) for p in self.demo_phones.split(",")) if e}

    @property
    def effective_cors_origins(self) -> list[str]:
        """Production never answers CORS for every origin; list the origins that may call from a browser."""
        if self.is_production:
            return [o for o in self.cors_origins if o != "*"]
        return self.cors_origins


@cache
def settings() -> Settings:
    s = Settings()
    if s.is_production and s.jwt_secret.startswith("dev-only"):
        raise RuntimeError("CURA_JWT_SECRET must be set in production")
    return s
