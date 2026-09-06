"""Fixtures for the paper-side tests: a real database, real repositories, no mocks."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from src.common.clock import FrozenClock
from src.common.config import (
    PaperConfig,
    PolicyConfig,
    QuoteConfig,
    RiskConfig,
)
from src.paper.broker import PaperBroker
from src.paper.policy import SignalPolicy
from src.paper.positions import PositionRepository
from src.paper.quotes import SimulatedQuoteProvider
from src.paper.risk import KillSwitch, RiskEngine
from tests.factories import T0


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(T0)


@pytest.fixture
def positions() -> PositionRepository:
    return PositionRepository()


@pytest.fixture
def kill_switch(tmp_path: Path) -> KillSwitch:
    return KillSwitch(tmp_path / "KILL_SWITCH")


@pytest.fixture
def risk_config(tmp_path: Path) -> RiskConfig:
    return RiskConfig(
        max_trade_size_sol=Decimal("0.1"),
        max_position_per_token_sol=Decimal("0.3"),
        max_total_exposure_sol=Decimal("1.0"),
        max_daily_exposure_sol=Decimal("2.0"),
        max_daily_loss_sol=Decimal("0.5"),
        max_open_positions=5,
        fee_reserve_sol=Decimal("0.05"),
        starting_balance_sol=Decimal("2.0"),
        max_consecutive_failures=3,
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )


@pytest.fixture
def risk(
    risk_config: RiskConfig, positions: PositionRepository, kill_switch: KillSwitch
) -> RiskEngine:
    return RiskEngine(risk_config, positions=positions, kill_switch=kill_switch)


@pytest.fixture
def policy_config() -> PolicyConfig:
    return PolicyConfig(
        base_buy_size_sol=Decimal("0.05"),
        min_buy_size_sol=Decimal("0.01"),
        max_signal_age_seconds=90,
        intent_ttl_seconds=20,
    )


@pytest.fixture
def policy(
    policy_config: PolicyConfig,
    clock: FrozenClock,
    positions: PositionRepository,
    risk: RiskEngine,
) -> SignalPolicy:
    return SignalPolicy(policy_config, clock=clock, positions=positions, risk=risk)


@pytest.fixture
def quotes(clock: FrozenClock) -> SimulatedQuoteProvider:
    return SimulatedQuoteProvider(
        QuoteConfig(
            provider="simulated",
            quote_ttl_seconds=10,
            simulated_pool_quote_liquidity_sol=Decimal("50"),
            simulated_fee_bps=25,
        ),
        clock=clock,
    )


@pytest.fixture
def paper_config() -> PaperConfig:
    # Landing is deterministic in tests: a probability of 1 keeps the assertions about
    # fills about fills, and the landing model gets its own dedicated test.
    return PaperConfig(
        land_probability=Decimal("1"),
        extra_slippage_bps=50,
        priority_fee_lamports=100_000,
        base_fee_lamports=5_000,
        rng_seed="test-seed",
    )


@pytest.fixture
def broker(
    paper_config: PaperConfig,
    clock: FrozenClock,
    quotes: SimulatedQuoteProvider,
    positions: PositionRepository,
) -> PaperBroker:
    return PaperBroker(paper_config, clock=clock, quotes=quotes, positions=positions)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as active:
        yield active
        active.commit()
