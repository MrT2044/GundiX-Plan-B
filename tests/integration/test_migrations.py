"""The migrated schema must match the models.

02_PLAN_B B9 makes "DB-Migration aktuell" a preflight condition. That check is only
meaningful if a drift between the migration and ``src/paper/store.py`` is actually
detectable, which is what this test does: it migrates an empty database and compares the
result against the metadata the application uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from src.common.db import Base
from src.paper import store  # noqa: F401  (imported for its side effect: table registration)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_database(tmp_path_factory) -> Path:
    target: Path = tmp_path_factory.mktemp("migrations") / "migrated.sqlite"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "-x",
            "dummy",
            "upgrade",
            "head",
        ],
        cwd=REPO_ROOT,
        env={
            "PATH": "",
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": f"{REPO_ROOT};{REPO_ROOT / 'contracts' / 'python'}",
            "ALEMBIC_URL": f"sqlite:///{target}",
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return target


def test_the_migration_produces_the_schema_the_code_expects(migrated_database: Path) -> None:
    engine = create_engine(f"sqlite:///{migrated_database}")
    try:
        inspector = inspect(engine)
        migrated = set(inspector.get_table_names()) - {"alembic_version"}
        expected = set(Base.metadata.tables)
        assert migrated == expected, (
            f"only migrated {sorted(migrated - expected)}, only in code {sorted(expected - migrated)}"
        )

        for table in sorted(expected):
            columns = {column["name"] for column in inspector.get_columns(table)}
            declared = set(Base.metadata.tables[table].columns.keys())
            assert columns == declared, f"{table}: {columns ^ declared}"
    finally:
        engine.dispose()


def test_the_money_invariants_are_enforced_by_unique_indexes(migrated_database: Path) -> None:
    """These two indexes are what make a double trade structurally impossible."""
    engine = create_engine(f"sqlite:///{migrated_database}")
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND name LIKE 'uq_%'"
            ).fetchall()
        by_name = {name: sql or "" for name, sql in rows}

        assert "uq_copy_intents_event_execute" in by_name
        assert "WHERE" in by_name["uq_copy_intents_event_execute"].upper(), (
            "the index must be partial, otherwise a NO_TRADE re-evaluation would collide "
            "with the EXECUTE intent for the same event"
        )
        assert "uq_execution_results_effective" in by_name
        assert "WHERE" in by_name["uq_execution_results_effective"].upper()
    finally:
        engine.dispose()
