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


def run(*, host: str, port: int, public_url: str | None, seed_registry: str) -> int:
    ensure_secret()
    if os.environ.get("CURA_ENVIRONMENT", "development") == "production":
        print("serve-demo refuses to run with CURA_ENVIRONMENT=production", file=sys.stderr)
        return 2
    os.environ.setdefault("CURA_DEMO_PHONES", DEMO_NUMBERS)
    if public_url:
        os.environ["CURA_PUBLIC_BASE_URL"] = public_url.rstrip("/")

    from sqlalchemy import func, select

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
