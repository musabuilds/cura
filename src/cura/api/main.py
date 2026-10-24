"""Cura API — the HTTP face of the registry."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from cura import __version__
from cura.api import db, errors
from cura.api.routes import auth, contrib, history, identity, live, registry, roster, search
from cura.api.settings import settings

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
    yield


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
    ],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "If-Match", "Idempotency-Key"],
    expose_headers=["ETag", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)
errors.install(app)


@app.middleware("http")
async def headers(request: Request, call_next):
    response = await call_next(request)
    if rate := getattr(request.state, "rate", None):
        response.headers["X-RateLimit-Limit"], response.headers["X-RateLimit-Remaining"] = str(rate[0]), str(rate[1])
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.get("/health", tags=["registry"])
def health() -> dict:
    return {"status": "ok", "version": __version__}


app.include_router(auth.router)
# Fixed paths (/practitioners/match, /reference/availability, /search) before the {id} routes they resemble.
app.include_router(identity.router)
app.include_router(live.router)
app.include_router(search.router)
app.include_router(registry.router)
app.include_router(roster.router)
app.include_router(contrib.router)
app.include_router(history.router)
app.include_router(contrib.collection_router)
