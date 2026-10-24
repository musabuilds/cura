"""Alembic environment. Always runs on a connection handed over by `cura.api.migrate`."""

from __future__ import annotations

from alembic import context

from cura.api.db import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=connection.dialect.name == "sqlite",  # SQLite cannot ALTER most things in place
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
