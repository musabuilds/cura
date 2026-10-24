"""The migrations and the models must describe the same schema."""

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from cura.api import migrate
from cura.api.db import Base


def _engine(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")


def test_single_head():
    assert len(ScriptDirectory.from_config(migrate.config()).get_heads()) == 1


def test_migrations_match_models(tmp_path):
    engine = _engine(tmp_path)
    migrate.upgrade(engine)
    assert migrate.current(engine) == migrate.head()
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models changed without a migration: {diff}"


def test_legacy_create_all_database_is_adopted(tmp_path):
    """v0.1.0 databases were built with create_all and carry no alembic_version."""
    engine = _engine(tmp_path)
    migrate.upgrade(engine, migrate.BASELINE)  # the v0.1.0 schema …
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")  # … as create_all left it
    migrate.upgrade(engine)
    assert migrate.current(engine) == migrate.head()
    assert "facilities" in inspect(engine).get_table_names()
