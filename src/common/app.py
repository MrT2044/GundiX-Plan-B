"""Composition root.

One place builds the object graph, so tests exercise the same wiring the runner does. If a
test had to assemble its own pipeline, it would be testing an arrangement that never runs
in production - which is exactly the self-deception S0 9.3 warns about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gundix_contracts.enums import OperatingMode
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.common.clock import Clock, SystemClock
from src.common.config import RuntimeConfig
from src.common.db import Base, create_db_engine, create_session_factory
from src.common.logging import get_logger, log_event
from src.execution.live_gate import kill_switch_from
from src.execution.reconcile import Reconciler
from src.execution.shadow import ShadowBroker
from src.paper.broker import Broker, PaperBroker
from src.paper.export import ObservationExporter
from src.paper.policy import SignalPolicy
from src.paper.positions import PositionRepository
from src.paper.quotes import build_quote_provider
from src.paper.risk import KillSwitch, RiskEngine
from src.paper.selection import SelectionRepository
from src.stream.pipeline import Pipeline
from src.stream.sources import ChainEventSource, build_source

logger = get_logger(__name__)


@dataclass(slots=True)
class Application:
    config: RuntimeConfig
    mode: OperatingMode
    clock: Clock
    engine: Engine
    session_factory: sessionmaker[Session]
    source: ChainEventSource
    selections: SelectionRepository
    positions: PositionRepository
    risk: RiskEngine
    kill_switch: KillSwitch
    broker: Broker | None
    pipeline: Pipeline
    exporter: ObservationExporter
    reconciler: Reconciler

    def close(self) -> None:
        self.source.close()
        self.engine.dispose()


def build_application(
    config: RuntimeConfig,
    mode: OperatingMode,
    *,
    clock: Clock | None = None,
    create_tables: bool = False,
    database_url: str | None = None,
) -> Application:
    """Assemble the whole of Plan B for one operating mode.

    ``create_tables`` exists for tests and first-run bootstrapping. Production runs apply
    Alembic migrations instead, so that a schema change is a reviewed migration rather than
    a silent side effect of starting the process.
    """
    if mode.may_broadcast:
        # There is no live path in this build, and the mode must not be able to conjure one.
        from src.execution.live_gate import assert_live_allowed

        assert_live_allowed()

    resolved_clock = clock or SystemClock()
    engine = create_db_engine(
        database_url or config.database.url, busy_timeout_ms=config.database.busy_timeout_ms
    )
    if create_tables:
        Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    positions = PositionRepository()
    kill_switch = kill_switch_from(config)
    risk = RiskEngine(config.risk, positions=positions, kill_switch=kill_switch)
    selections = SelectionRepository(
        Path(config.selection.path),
        clock=resolved_clock,
        require_approval=config.selection.require_approval,
    )
    policy = SignalPolicy(
        config.policy,
        clock=resolved_clock,
        positions=positions,
        risk=risk,
        required_finality=config.stream.required_finality,
    )
    quotes = build_quote_provider(config.quotes, clock=resolved_clock)

    broker: Broker | None = None
    if mode is OperatingMode.PAPER:
        broker = PaperBroker(config.paper, clock=resolved_clock, quotes=quotes, positions=positions)
    elif mode is OperatingMode.SHADOW:
        broker = ShadowBroker(
            config.paper, clock=resolved_clock, quotes=quotes, positions=positions
        )
    # RESEARCH and OBSERVE deliberately get no broker: they decode and record, and there is
    # no object present that could execute anything.

    source = build_source(config.stream, clock=resolved_clock)
    pipeline = Pipeline(
        config=config,
        mode=mode,
        clock=resolved_clock,
        session_factory=session_factory,
        source=source,
        selections=selections,
        policy=policy,
        broker=broker,
        positions=positions,
    )

    log_event(
        logger,
        20,
        "application built",
        mode=mode.value,
        broker=broker.kind if broker else None,
        source=source.name,
        quote_provider=config.quotes.provider,
        unknown_program_policy=config.decoder.unknown_program_policy,
        config_hash=config.hash(),
    )
    return Application(
        config=config,
        mode=mode,
        clock=resolved_clock,
        engine=engine,
        session_factory=session_factory,
        source=source,
        selections=selections,
        positions=positions,
        risk=risk,
        kill_switch=kill_switch,
        broker=broker,
        pipeline=pipeline,
        exporter=ObservationExporter(config),
        reconciler=Reconciler(clock=resolved_clock, positions=positions),
    )


__all__ = ["Application", "build_application"]
