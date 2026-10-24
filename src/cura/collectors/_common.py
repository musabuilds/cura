"""Shared helpers for collectors: paths, HTTP client and the staging writer (see sources/STAGING.md)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cura.paths import RAW_DIR, STAGING_DIR

USER_AGENT = "cura-jo/0.1 (+https://github.com/NextMoveIntl/cura; open health dataset)"


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)


def raw_dir(source: str) -> Path:
    path = RAW_DIR / source
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_staging(source: str, records: Iterable[dict[str, Any]], meta: dict[str, Any]) -> int:
    """Write `<source>.jsonl` and `<source>.meta.json`, sorted by source_ref for stable diffs."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(records, key=lambda r: (r["kind"], str(r.get("source_ref", "")), json.dumps(r["data"])))
    with (STAGING_DIR / f"{source}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    meta = {"source": source, **meta, "record_count": len(rows)}
    (STAGING_DIR / f"{source}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return len(rows)
