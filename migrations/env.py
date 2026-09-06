"""Alembic environment.

The schema is owned by ``src/paper/store.py``; this file only points Alembic at it. A
schema change is therefore a reviewed migration, never a silent side effect of starting the
process.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "contracts" / "python")]

from src.common.db import Base  # noqa: E402
from src.paper import store  # noqa: E402,F401  (imported for its side effect: table registration)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Tests and deployments point this somewhere other than the default in alembic.ini.
_url_override = os.environ.get("ALEMBIC_URL")
if _url_override:
    config.set_main_option("sqlalchemy.url", _url_override)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _ensure_sqlite_directory(url: str) -> None:
    """SQLite cannot create a database inside a directory that does not exist yet."""
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + ":memory:"):
        Path(url[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)


def run_migrations_online() -> None:
    _ensure_sqlite_directory(config.get_main_option("sqlalchemy.url") or "")
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rebuilds the table.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
