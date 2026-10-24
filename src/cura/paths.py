"""Repository paths. Everything is resolved from the repo root so commands work from any cwd."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("CURA_ROOT", Path(__file__).resolve().parents[2]))

DATA_DIR = ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
GEO_DIR = REFERENCE_DIR / "geo"
SOURCES_DIR = ROOT / "sources"
STAGING_DIR = SOURCES_DIR / "staging"
RAW_DIR = SOURCES_DIR / "raw"
DIST_DIR = ROOT / "dist"
SCHEMA_DIR = ROOT / "schema"
