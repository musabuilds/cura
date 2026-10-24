"""`GET /demo/site/{facility_id}`: what a hospital's own website looks like when it is built on Cura.

The page holds no data. It reads the public API in the browser (`GET /v1/facilities/{id}?expand=
practitioners`) and listens to `GET /v1/stream?facility_id=…`; when a doctor changes their hours or
availability on their phone, the card updates and flashes within a second. Exactly what a real
hospital site would do with its API key, minus the key (public reads need none).

Security: a strict CSP with a per-request nonce for the one inline script and stylesheet; every value
from the API is written with `textContent`, never parsed as HTML; no third-party requests at all.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from cura.api import store
from cura.api.db import get_session
from cura.model import ID_PATTERN

router = APIRouter()
PAGE = Path(__file__).with_name("demo_site.html")


def _csp(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "img-src 'self' https: data:; connect-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


@router.get("/demo/site/{facility_id}", response_class=HTMLResponse, tags=["demo"])
def demo_site(facility_id: str, db: Session = Depends(get_session)) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    headers = {"Content-Security-Policy": _csp(nonce), "Cache-Control": "no-store", "X-Frame-Options": "DENY"}
    if not re.fullmatch(ID_PATTERN, facility_id) or store.get(db, "facility", facility_id) is None:
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><title>Not found</title><style nonce='{nonce}'>body{{font:16px "
            "system-ui;margin:20vh auto;max-width:480px;padding:0 16px;color:#334}}</style><h1>Facility not found"
            "</h1><p>Use an id from <code>/v1/facilities</code>.</p>",
            status_code=404,
            headers=headers,
        )
    config = json.dumps({"facilityId": facility_id}).replace("<", "\\u003c")
    html = PAGE.read_text(encoding="utf-8").replace("__NONCE__", nonce).replace("__CONFIG__", config)
    return HTMLResponse(html, headers=headers)
