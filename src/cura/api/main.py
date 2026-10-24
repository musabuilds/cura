"""Cura API — the HTTP face of the registry."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cura import __version__
from cura.api import db, errors, sync, webhooks
from cura.api.routes import (
    account,
    auth,
    contrib,
    demo,
    history,
    identity,
    integrations,
    live,
    onboarding,
    registry,
    roster,
    search,
)
from cura.api.settings import settings

log = logging.getLogger("cura.api")

DESCRIPTION = """
Open registry of Jordan's hospitals, clinics, doctors, pharmacies, labs and their services.

* **Read** — open to everyone. Add an `X-API-Key` (register an app at `POST /v1/apps`) for a higher rate limit.
* **Write** — on behalf of a signed-in user via OAuth 2.0 authorization code + PKCE
  (`/oauth/authorize` → `/oauth/token`). Verified owners (doctors, facility admins) edit their own records
  directly; everything else becomes a proposal reviewed by moderators.
* **Live** — doctors and facilities set availability and closures themselves (`POST /v1/availability`);
  every read resolves schedule + events into what is true now.
* **History** — nothing is overwritten: `GET /v1/{collection}/{id}/history`.
* **Sync** — poll `GET /v1/changes?since=` and `GET /v1/availability/changes?since=`.

`null` means *unknown*. Data © OpenStreetMap contributors and the sources listed per record, ODbL-1.0.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings().is_production:
        # Replicas never migrate on boot (they would race); `cura db upgrade` runs once, before them.
        from cura.api import migrate

        if (rev := migrate.current(db.engine())) != migrate.head():
            raise RuntimeError(f"database is at revision {rev}, expected {migrate.head()}: run `cura db upgrade`")
    else:
        db.init()
    workers = settings().background_workers
    if workers:
        sync.start_worker()
        webhooks.start_worker()
    yield
    if workers:
        sync.stop_worker()
        webhooks.stop_worker()


app = FastAPI(
    title="Cura API",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
    license_info={"name": "Data: ODbL-1.0 · Code: Apache-2.0"},
    openapi_tags=[
        {"name": "registry", "description": "Facilities, practitioners and their roles"},
        {"name": "reference", "description": "Controlled vocabularies and price references"},
        {"name": "sync", "description": "Incremental change feed"},
        {"name": "contribute", "description": "Edits and additions (OAuth user token)"},
        {"name": "claims", "description": "Doctors and facility admins claiming their records"},
        {"name": "availability", "description": "Live state: who is available where, closures, load"},
        {"name": "roster", "description": "Who works where: adding, confirming and ending roles; invitations"},
        {"name": "identity", "description": "Finding existing records before creating; merging duplicates"},
        {"name": "history", "description": "Every version of every record"},
        {"name": "search", "description": "Hybrid lexical + semantic search"},
        {"name": "reviews", "description": "Ratings by dimension"},
        {"name": "moderation", "description": "Moderator queue"},
        {"name": "auth", "description": "Accounts"},
        {"name": "apps", "description": "Register an app to get an API key and OAuth client"},
        {"name": "oauth", "description": "OAuth 2.0 authorization server"},
        {"name": "demo", "description": "A hospital website built on the public API, kept live by SSE"},
    ],
)
errors.install(app)

# Pages that load their own scripts (Swagger UI from a CDN, the demo site with a nonce) set their own CSP.
OWN_CSP = ("/docs", "/redoc", "/demo/", "/oauth/authorize")


class BodyLimit:
    """Refuses request bodies over `max_body_bytes` (413), declared or streamed, before a route reads them."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            return await self._reject(scope, send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        try:
            return await self.app(scope, limited, send)
        except _TooLarge:
            return await self._reject(scope, send)

    async def _reject(self, scope, send):
        body = (
            b'{"type":"about:blank","title":"Request too large","status":413,'
            b'"detail":"The request body is over the limit."}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


@app.middleware("http")
async def headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:  # never a stack trace (or the data in it) to the caller
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(
            {"type": "about:blank", "title": "Internal error", "status": 500, "detail": "Something went wrong."},
            status_code=500,
            media_type="application/problem+json",
        )
    if rate := getattr(request.state, "rate", None):
        response.headers["X-RateLimit-Limit"], response.headers["X-RateLimit-Remaining"] = str(rate[0]), str(rate[1])
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith(OWN_CSP):
        h.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if request.url.scheme == "https" or forwarded == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().effective_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "If-Match", "Idempotency-Key"],
    expose_headers=["ETag", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
)
app.add_middleware(BodyLimit, max_bytes=settings().max_body_bytes)


@app.get("/health", tags=["registry"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


app.include_router(auth.router)
app.include_router(account.router)
app.include_router(onboarding.router)
app.include_router(integrations.router)
app.include_router(demo.router)
# Fixed paths (/practitioners/match, /reference/availability, /search) before the {id} routes they resemble.
app.include_router(identity.router)
app.include_router(live.router)
app.include_router(search.router)
app.include_router(registry.router)
app.include_router(roster.router)
app.include_router(contrib.router)
app.include_router(history.router)
app.include_router(contrib.collection_router)
