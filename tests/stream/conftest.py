"""Fixtures for the stream tests that need a running pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gundix_contracts.enums import OperatingMode
from sqlalchemy.orm import Session, sessionmaker

from src.common.app import Application, build_application
from src.common.clock import FrozenClock
from tests.factories import WALLET_A, make_selection, write_selection_artifact


@pytest.fixture
def scenario_pipeline(tmp_path: Path) -> Iterator[tuple[Application, FrozenClock]]:
    """A real application, wired exactly as the runner wires it."""
    from tests.integration.test_end_to_end_paper import build_config

    clock = FrozenClock(datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC))
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    config = build_config(tmp_path, replay_dir=replay_dir)
    write_selection_artifact(
        config.selection.path, make_selection(wallets=(WALLET_A,), now=clock.now())
    )
    app = build_application(
        config,
        OperatingMode.PAPER,
        clock=clock,
        create_tables=True,
        database_url=f"sqlite:///{tmp_path / 'gundix.sqlite'}",
    )
    app.selections.load()
    yield app, clock
    app.close()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as active:
        yield active
        active.commit()
