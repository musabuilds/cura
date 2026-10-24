"""Schema migrations (Alembic).

The database schema changes only through the revisions in `migrations/versions/`. `db.init()` runs
them, so tests, local SQLite and production Postgres all reach the same schema by the same path, and
`tests/test_migrations.py` fails if a model changes without a revision.

    cura db upgrade                    # apply every pending revision
    cura db revision -m "add x"        # autogenerate a revision from the models, then review it by hand
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"  # the schema of v0.1.0, which predates migrations


def config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS.as_posix())
    return cfg


def head() -> str:
    return ScriptDirectory.from_config(config()).get_current_head()


def current(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(engine, revision: str = "head") -> None:
    cfg = config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "facilities" in tables and "alembic_version" not in tables:
            # A v0.1.0 database built with create_all: adopt it at the baseline, then migrate forward.
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, revision)


def revision(engine, message: str) -> None:
    cfg = config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.revision(cfg, message=message, autogenerate=True)
