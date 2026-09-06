"""Shared test fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from src.common.clock import FrozenClock
from src.common.db import Base, create_db_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_TRANSACTIONS = REPO_ROOT / "contracts" / "golden_raw"
GOLDEN_CASES = REPO_ROOT / "contracts" / "golden"

#: A fixed instant used by every deterministic test.
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def load_golden(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (GOLDEN_TRANSACTIONS / f"{name}.json").read_text(encoding="utf-8")
    )
    return payload


def golden_names() -> list[str]:
    return sorted(path.stem for path in GOLDEN_TRANSACTIONS.glob("*.json"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.sqlite'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()
