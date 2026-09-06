"""Database access.

SQLite in PAPER/SHADOW, with the pragmas that make it behave under a crash:

* ``journal_mode=WAL`` so a reader never sees a half-written transaction,
* ``synchronous=FULL`` so a committed intent survives a power cut - the write rate here
  is a handful per minute, so durability is cheap,
* ``foreign_keys=ON`` because the invariants in 02_PLAN_B B5 are relational,
* ``busy_timeout`` so a concurrent writer waits instead of raising.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, TypeDecorator, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that is always UTC-aware in Python.

    SQLite has no timezone-aware type: a value written as aware comes back naive, and
    comparing it against an aware ``now`` raises. Rather than sprinkling ``.replace(tzinfo=)``
    across every query, the conversion happens once, here, at the boundary.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("refusing to store a naive datetime; every timestamp is UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, _dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Metadata root for every GundiX table."""


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith(prefix + ":memory:"):
        return Path(url[len(prefix) :])
    return None


def create_db_engine(url: str, *, busy_timeout_ms: int = 5000, echo: bool = False) -> Engine:
    path = _sqlite_path(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, echo=echo, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def migrations_current(engine: Engine, migrations_dir: Path) -> tuple[bool, str]:
    """Is the database on the newest migration? Returns ``(ok, detail)``.

    02_PLAN_B B9 lists this as a preflight condition. Checking it at startup turns a
    confusing "no such table" crash somewhere in the hot path into one clear refusal to
    start.
    """
    versions = migrations_dir / "versions"
    heads = {
        line.split("revision: str = ")[1].strip().strip("\"'")
        for path in versions.glob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("revision: str = ")
    }
    if not heads:
        return False, f"no migrations found in {versions}"

    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return False, "the database has never been migrated (no alembic_version table)"
    with engine.connect() as connection:
        applied = {
            row[0] for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
        }
    missing = heads - applied
    if missing:
        return False, f"migration(s) not applied: {', '.join(sorted(missing))}"
    return True, f"at {', '.join(sorted(applied))}"


@contextmanager
def unit_of_work(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction. Commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
