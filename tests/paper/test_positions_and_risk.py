"""Copy positions, exposure and risk limits (B5, B9).

The invariants under test are the ones that decide whether the system can lose money in a
way nobody predicted: sold never exceeds held, exposure equals the sum of its parts, and a
daily limit survives a restart.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from src.common.clock import FrozenClock
from src.common.config import RiskConfig
from src.paper.positions import PositionError, PositionRepository, sol_to_lamports
from src.paper.risk import KillSwitch, RiskEngine
from tests.factories import MINT_X, MINT_Y, T0, WALLET_A, WALLET_B

SOL = 1_000_000_000


def test_a_buy_opens_a_position(session: Session, positions: PositionRepository) -> None:
    position = positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=SOL // 20,
        now=T0,
    )
    assert position.base_amount_raw == 1_000_000
    assert position.quote_cost_raw == SOL // 20
    assert positions.get_exposure(session, MINT_X).open_positions == 1


def test_positions_are_attributed_per_source_wallet(
    session: Session, positions: PositionRepository
) -> None:
    """Two wallets buying the same token means two positions, not one merged blob.

    That is what makes a proportional sell meaningful later.
    """
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=SOL // 20,
        now=T0,
    )
    positions.apply_buy(
        session,
        source_wallet=WALLET_B,
        base_mint=MINT_X,
        base_filled_raw=3_000_000,
        quote_spent_raw=SOL // 10,
        now=T0,
    )

    assert positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == 1_000_000
    assert positions.get_position(session, WALLET_B, MINT_X).base_amount_raw == 3_000_000
    exposure = positions.get_exposure(session, MINT_X)
    assert exposure.base_amount_raw == 4_000_000
    assert exposure.open_positions == 2


def test_a_partial_sell_releases_cost_proportionally(
    session: Session, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=100_000_000,
        now=T0,
    )
    remaining = positions.apply_sell(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_sold_raw=300_000,
        quote_received_raw=40_000_000,
        now=T0,
    )

    assert remaining.base_amount_raw == 700_000
    # 30 % of the position went out, so 30 % of the cost basis went with it.
    assert remaining.quote_cost_raw == 70_000_000
    assert remaining.quote_proceeds_raw == 40_000_000
    assert positions.get_exposure(session, MINT_X).open_positions == 1


def test_a_full_exit_closes_the_position(session: Session, positions: PositionRepository) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=100_000_000,
        now=T0,
    )
    remaining = positions.apply_sell(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_sold_raw=1_000_000,
        quote_received_raw=150_000_000,
        now=T0,
    )

    assert remaining.base_amount_raw == 0
    assert remaining.quote_cost_raw == 0
    assert remaining.realized_pnl_quote_raw == 150_000_000
    exposure = positions.get_exposure(session, MINT_X)
    assert exposure.open_positions == 0
    assert exposure.base_amount_raw == 0


def test_selling_more_than_held_is_refused(session: Session, positions: PositionRepository) -> None:
    """The invariant that must hold at all times, enforced rather than assumed."""
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=SOL,
        now=T0,
    )
    with pytest.raises(PositionError, match="exceeds the held"):
        positions.apply_sell(
            session,
            source_wallet=WALLET_A,
            base_mint=MINT_X,
            base_sold_raw=1_000_001,
            quote_received_raw=SOL,
            now=T0,
        )


def test_selling_without_a_position_is_refused(
    session: Session, positions: PositionRepository
) -> None:
    with pytest.raises(PositionError, match="no copy position"):
        positions.apply_sell(
            session,
            source_wallet=WALLET_A,
            base_mint=MINT_X,
            base_sold_raw=1,
            quote_received_raw=1,
            now=T0,
        )


def test_selling_another_wallets_position_is_refused(
    session: Session, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=SOL,
        now=T0,
    )
    with pytest.raises(PositionError, match="no copy position"):
        positions.apply_sell(
            session,
            source_wallet=WALLET_B,
            base_mint=MINT_X,
            base_sold_raw=1_000,
            quote_received_raw=1,
            now=T0,
        )


# -- risk ---------------------------------------------------------------------------------
def test_a_trade_above_the_per_trade_limit_is_denied(session: Session, risk: RiskEngine) -> None:
    decision = risk.check_buy(
        session, base_mint=MINT_X, size_lamports=sol_to_lamports(Decimal("0.2")), now=T0
    )
    assert not decision.allowed
    assert decision.reason is not None and decision.reason.value == "position_limit"


def test_the_per_token_limit_accumulates_across_wallets(
    session: Session, risk: RiskEngine, positions: PositionRepository
) -> None:
    """Limits are about risk in a token, so it does not matter who caused it.

    The per-token cap is 0.3 SOL. Two wallets at 0.1 each leave room for exactly 0.1 more;
    anything beyond that must be refused even though no single wallet is near the cap.
    """
    for wallet in (WALLET_A, WALLET_B):
        positions.apply_buy(
            session,
            source_wallet=wallet,
            base_mint=MINT_X,
            base_filled_raw=1_000_000,
            quote_spent_raw=sol_to_lamports(Decimal("0.1")),
            now=T0,
        )

    # Exactly reaching the cap is allowed; the limit is inclusive.
    assert risk.check_buy(
        session, base_mint=MINT_X, size_lamports=sol_to_lamports(Decimal("0.1")), now=T0
    ).allowed

    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=sol_to_lamports(Decimal("0.1")),
        now=T0,
    )
    decision = risk.check_buy(
        session, base_mint=MINT_X, size_lamports=sol_to_lamports(Decimal("0.01")), now=T0
    )
    assert not decision.allowed
    assert "max_position_per_token" in (decision.detail or "")


def test_the_open_position_count_is_capped(
    session: Session, risk: RiskEngine, positions: PositionRepository, risk_config: RiskConfig
) -> None:
    mints = [f"{MINT_X[:-2]}{i:02d}" for i in range(risk_config.max_open_positions)]
    for index, mint in enumerate(mints):
        positions.apply_buy(
            session,
            source_wallet=WALLET_A,
            base_mint=mint,
            base_filled_raw=1_000,
            quote_spent_raw=sol_to_lamports(Decimal("0.01")),
            now=T0,
        )
        assert index >= 0

    decision = risk.check_buy(
        session, base_mint=MINT_Y, size_lamports=sol_to_lamports(Decimal("0.01")), now=T0
    )
    assert not decision.allowed
    assert "open positions" in (decision.detail or "")


def test_the_kill_switch_blocks_buys_and_sells(
    session: Session, risk: RiskEngine, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000,
        quote_spent_raw=1_000,
        now=T0,
    )
    risk.kill_switch.engage("test")

    buy = risk.check_buy(session, base_mint=MINT_X, size_lamports=1_000, now=T0)
    sell = risk.check_sell(
        session, source_wallet=WALLET_A, base_mint=MINT_X, base_amount_raw=100, now=T0
    )
    assert not buy.allowed and buy.reason is not None
    assert buy.reason.value == "kill_switch_active"
    assert not sell.allowed and sell.reason is not None
    assert sell.reason.value == "kill_switch_active"


def test_the_daily_loss_limit_blocks_further_buys(
    session: Session, risk: RiskEngine, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=sol_to_lamports(Decimal("0.1")),
        now=T0,
    )
    positions.apply_sell(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_sold_raw=1_000_000,
        quote_received_raw=0,
        now=T0,
    )
    # Realised loss so far is only 0.1 SOL, below the 0.5 limit.
    assert risk.check_buy(session, base_mint=MINT_Y, size_lamports=1_000, now=T0).allowed

    for mint in (MINT_Y, f"{MINT_Y[:-1]}2", f"{MINT_Y[:-1]}3", f"{MINT_Y[:-1]}4"):
        positions.apply_buy(
            session,
            source_wallet=WALLET_A,
            base_mint=mint,
            base_filled_raw=1_000,
            quote_spent_raw=sol_to_lamports(Decimal("0.1")),
            now=T0,
        )
        positions.apply_sell(
            session,
            source_wallet=WALLET_A,
            base_mint=mint,
            base_sold_raw=1_000,
            quote_received_raw=0,
            now=T0,
        )

    decision = risk.check_buy(session, base_mint=MINT_X, size_lamports=1_000, now=T0)
    assert not decision.allowed
    assert decision.reason is not None and decision.reason.value == "daily_limit"


def test_daily_limits_survive_a_restart(session_factory, risk_config: RiskConfig, tmp_path) -> None:
    """A daily limit that resets when the process restarts is not a limit."""
    positions = PositionRepository()
    with session_factory() as first:
        positions.apply_buy(
            first,
            source_wallet=WALLET_A,
            base_mint=MINT_X,
            base_filled_raw=1_000,
            quote_spent_raw=sol_to_lamports(Decimal("0.5")),
            now=T0,
        )
        first.commit()

    # A brand new process: new repositories, new engine, same database.
    fresh_positions = PositionRepository()
    fresh_risk = RiskEngine(
        risk_config,
        positions=fresh_positions,
        kill_switch=KillSwitch(tmp_path / "KILL_SWITCH_2"),
    )
    with session_factory() as second:
        deployed, _pnl, trades, _fails = fresh_positions.daily(second, T0)
        assert deployed == sol_to_lamports(Decimal("0.5"))
        assert trades == 1
        snapshot = fresh_risk.snapshot(second, T0)
        assert snapshot.total_exposure_lamports == sol_to_lamports(Decimal("0.5"))


def test_consecutive_failures_trip_the_circuit_breaker(
    session: Session, risk: RiskEngine, positions: PositionRepository, risk_config: RiskConfig
) -> None:
    for _ in range(risk_config.max_consecutive_failures):
        positions.record_execution_outcome(session, now=T0, succeeded=False)

    decision = risk.check_buy(session, base_mint=MINT_X, size_lamports=1_000, now=T0)
    assert not decision.allowed
    assert decision.reason is not None and decision.reason.value == "state_mismatch"

    positions.record_execution_outcome(session, now=T0, succeeded=True)
    assert risk.check_buy(session, base_mint=MINT_X, size_lamports=1_000, now=T0).allowed


def test_circuit_breakers_report_what_tripped(
    session: Session, risk: RiskEngine, positions: PositionRepository
) -> None:
    positions.record_execution_outcome(session, now=T0, succeeded=False)
    positions.record_execution_outcome(session, now=T0, succeeded=False)
    positions.record_execution_outcome(session, now=T0, succeeded=False)

    tripped = risk.evaluate_circuit_breakers(
        session,
        now=T0,
        feed_age_seconds=999.0,
        decoder_coverage=Decimal("0.10"),
        observed_slippage_bps=9_999,
    )
    assert any("consecutive_failures" in item for item in tripped)
    assert any("stale_feed" in item for item in tripped)
    assert any("decoder_coverage" in item for item in tripped)
    assert any("slippage_bps" in item for item in tripped)


def test_configuration_cannot_widen_an_absolute_ceiling(tmp_path) -> None:
    """S0 and 02_PLAN_B: absolute limits cannot be exceeded by configuration."""
    with pytest.raises(ValueError, match="absolute code ceiling"):
        RiskConfig(
            max_trade_size_sol=Decimal("5.0"),
            max_position_per_token_sol=Decimal("5.0"),
            max_total_exposure_sol=Decimal("5.0"),
            kill_switch_file=tmp_path / "KILL_SWITCH",
        )


def test_clock_is_injected_not_read_from_the_wall(clock: FrozenClock) -> None:
    """Every expiry and staleness decision must be testable without sleeping."""
    assert clock.now() == T0
    clock.advance(30)
    assert (clock.now() - T0).total_seconds() == 30
