"""`cura serve-demo`: one command from a fresh checkout to a server the phone app can reach.

1. A JWT secret: `CURA_JWT_SECRET` if set, otherwise generated once into `var/.secret` (gitignored,
   owner-only where the OS allows) and reused, so sessions survive restarts.
2. Development settings with the demo numbers enabled (never in production).
3. Migrate, load the registry from `data/*.jsonl` when the database is behind it, seed the demo.
4. uvicorn on 0.0.0.0:8000, trusting `X-Forwarded-*` from the local tunnel.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from pathlib import Path

from cura.paths import ROOT

SECRET_FILE = ROOT / "var" / ".secret"
DEMO_NUMBERS = "0790000001,0790000002,0790000003"


def ensure_secret(path: Path = SECRET_FILE) -> str:
    if os.environ.get("CURA_JWT_SECRET"):
        return os.environ["CURA_JWT_SECRET"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and len(path.read_text(encoding="ascii").strip()) >= 32:
        secret = path.read_text(encoding="ascii").strip()
    else:
        secret = secrets.token_hex(32)
        path.write_text(secret + "\n", encoding="ascii")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    os.environ["CURA_JWT_SECRET"] = secret
    return secret


def _dataset_size() -> int:
    from cura.paths import DATA_DIR

    path = DATA_DIR / "facilities.jsonl"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _rebuildable() -> bool:
    """Only a database nobody has written to through the API may be thrown away and rebuilt."""
    from sqlalchemy import func, select

    from cura.api import db
    from cura.api.db import Change, Claim, FacilityRow, PractitionerRow, RoleRow, User
    from cura.api.settings import settings

    if not settings().database_url.startswith("sqlite"):
        return False
    with db.new_session() as s:
        if any(s.scalar(select(func.count()).select_from(t)) for t in (User, Claim, Change)):
            return False
        return not any(
            s.scalar(select(func.count()).select_from(t).where(t.dirty))
            for t in (FacilityRow, PractitionerRow, RoleRow)
        )


def _move_aside() -> Path:
    import time

    from cura.api import db
    from cura.api.settings import settings

    db.reset_engine()
    path = Path(settings().database_url.removeprefix("sqlite:///"))
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    path.rename(backup)
    return backup


def run(*, host: str, port: int, public_url: str | None, seed_registry: str) -> int:
    ensure_secret()
    if os.environ.get("CURA_ENVIRONMENT", "development") == "production":
        print("serve-demo refuses to run with CURA_ENVIRONMENT=production", file=sys.stderr)
        return 2
    os.environ.setdefault("CURA_DEMO_PHONES", DEMO_NUMBERS)
    if public_url:
        os.environ["CURA_PUBLIC_BASE_URL"] = public_url.rstrip("/")

    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from cura.api import admin, db, demo_data
    from cura.api.db import FacilityRow
    from cura.api.settings import settings

    settings.cache_clear()
    db.reset_engine()
    db.init()
    with db.new_session() as s:
        have = s.scalar(select(func.count()).select_from(FacilityRow).where(FacilityRow.id < "JO-ZZ")) or 0
    want = _dataset_size()
    if seed_registry == "always" or (seed_registry == "auto" and have < want):
        print(f"Loading the registry from data/ ({have} facilities in the database, {want} in the dataset)…")
        try:
            print(admin.seed())
        except IntegrityError:
            # An older build whose ids no longer line up with the dataset cannot be updated in place.
            if not _rebuildable():
                print(
                    "The database is from an older build and holds API edits; not touching it. "
                    "Run with --seed-registry never, or migrate it by hand.",
                    file=sys.stderr,
                )
                return 1
            backup = _move_aside()
            print(f"The database was an older build with no API edits; moved it to {backup} and rebuilding…")
            db.reset_engine()
            db.init()
            print(admin.seed())
    ids = demo_data.seed()
    base = settings().public_base_url
    print("\nCura demo is ready.")
    print(f"  API          {base}   (docs: {base}/docs)")
    print(f"  Demo site    {base}/demo/site/{ids['facilities']['amman_crest_hospital']}")
    print(f"  Phone login  0790000001 / 0790000002 / 0790000003, code {settings().demo_otp}")
    print(f"  Musab        {ids['practitioners']['musab']}")
    fac = ids["facilities"]
    print(f"  Facilities   {fac['amman_crest_hospital']} (Crest), {fac['al_jabiri_clinic']} (clinic)\n")
    db.reset_engine()

    import uvicorn

    uvicorn.run(
        "cura.api.main:app", host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info"
    )
    return 0
