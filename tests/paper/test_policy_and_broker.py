"""Signal policy and paper broker (B4, B6).

Every NO_TRADE reason that the policy can produce gets a test, because Plan A reads the
distribution of these reasons to understand what the strategy actually does. A reason that
is never produced correctly makes that analysis wrong in a way nobody notices.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from gundix_contracts.enums import (
    Decision,
    DecodeConfidence,
    DedupPolicy,
    ExecutionStatus,
    Finality,
    OperatingMode,
    Side,
    Venue,
    WalletStatus,
)
from gundix_contracts.mints import USDC_MINT
from sqlalchemy.orm import Session

from src.common.clock import FrozenClock
from src.common.config import PaperConfig, PolicyConfig
from src.paper.broker import ExecutionContext, PaperBroker
from src.paper.policy import SignalContext, SignalPolicy
from src.paper.positions import PositionRepository, sol_to_lamports
from src.paper.selection import ActiveSelection
from src.paper.store import StoredCopyIntent
from tests.factories import MINT_X, T0, WALLET_A, WALLET_B, make_selection, make_swap_event


def active_selection(selection=None) -> ActiveSelection:
    selection = selection or make_selection(wallets=(WALLET_A,))
    return ActiveSelection(
        selection=selection,
        content_sha256="0" * 64,
        source_path=__import__("pathlib").Path("test"),
        loaded_at_utc=T0,
        by_wallet={entry.wallet: entry for entry in selection.wallets},
    )


def evaluate(policy: SignalPolicy, session: Session, event, *, selection=None, mode=None, ctx=None):
    return policy.evaluate(
        session,
        event,
        selection=selection if selection is not None else active_selection(),
        mode=mode or OperatingMode.PAPER,
        context=ctx,
    )


# -- the happy path -------------------------------------------------------------------------
def test_a_clean_buy_becomes_an_executable_intent(policy: SignalPolicy, session: Session) -> None:
    result = evaluate(policy, session, make_swap_event(side=Side.BUY))

    assert result.is_execute
    assert result.intent.decision is Decision.EXECUTE
    assert result.intent.reason_code is None
    assert result.intent.side is Side.BUY
    assert result.intent.target_size_quote_raw == sol_to_lamports(Decimal("0.05"))
    assert result.intent.expires_at_utc == T0 + timedelta(seconds=20)


def test_the_size_never_copies_the_traders_absolute_size(
    policy: SignalPolicy, session: Session
) -> None:
    """A wallet risking 50 SOL says nothing about what GundiX should risk."""
    huge = make_swap_event(quote_amount_raw=50_000_000_000)
    result = evaluate(policy, session, huge)
    assert result.intent.target_size_quote_raw == sol_to_lamports(Decimal("0.05"))


def test_the_size_scales_with_the_weight_plan_a_assigned(
    policy: SignalPolicy, session: Session
) -> None:
    selection = make_selection(wallets=(WALLET_A, WALLET_B))  # 0.5 weight each
    result = evaluate(policy, session, make_swap_event(), selection=active_selection(selection))
    assert result.intent.target_size_quote_raw == sol_to_lamports(Decimal("0.025"))


# -- NO_TRADE reasons ------------------------------------------------------------------------
def test_an_unselected_wallet_is_refused(policy: SignalPolicy, session: Session) -> None:
    result = evaluate(policy, session, make_swap_event(wallet=WALLET_B))
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "wallet_not_selected"


@pytest.mark.parametrize("status", [WalletStatus.OBSERVE_ONLY, WalletStatus.SUSPENDED])
def test_a_non_active_wallet_is_refused(
    policy: SignalPolicy, session: Session, status: WalletStatus
) -> None:
    selection = active_selection(make_selection(status=status))
    result = evaluate(policy, session, make_swap_event(), selection=selection)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "wallet_not_selected"


def test_observe_mode_never_produces_an_executable_intent(
    policy: SignalPolicy, session: Session
) -> None:
    result = evaluate(policy, session, make_swap_event(), mode=OperatingMode.OBSERVE)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "mode_disallows_execution"


def test_an_expired_selection_is_refused(policy: SignalPolicy, session: Session) -> None:
    expired = make_selection(expires_in_days=1, now=T0 - timedelta(days=5))
    result = evaluate(policy, session, make_swap_event(), selection=active_selection(expired))
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "selection_expired"


def test_a_failed_transaction_is_never_a_trade(policy: SignalPolicy, session: Session) -> None:
    """S0 4.1.7: every economic consumer filters on success."""
    result = evaluate(policy, session, make_swap_event(success=False))
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "transaction_failed"


@pytest.mark.parametrize(
    ("confidence", "venue", "expected"),
    [
        (DecodeConfidence.PARTIAL, Venue.PUMP_FUN, "decode_incomplete"),
        (DecodeConfidence.UNKNOWN, Venue.UNKNOWN, "unknown_venue"),
    ],
)
def test_incompletely_decoded_events_are_refused(
    policy: SignalPolicy, session: Session, confidence, venue, expected: str
) -> None:
    event = make_swap_event(decode_confidence=confidence, venue=venue)
    result = evaluate(policy, session, event)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == expected


def test_an_unsupported_venue_is_refused(policy: SignalPolicy, session: Session) -> None:
    event = make_swap_event(venue=Venue.ORCA_WHIRLPOOL)
    result = evaluate(policy, session, event)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "unknown_venue"


def test_an_insufficiently_final_event_is_refused(policy: SignalPolicy, session: Session) -> None:
    event = make_swap_event(finality=Finality.PROCESSED)
    result = evaluate(policy, session, event)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "event_not_final"


def test_a_stale_signal_is_refused(policy: SignalPolicy, session: Session) -> None:
    old = make_swap_event(block_time=T0 - timedelta(seconds=120))
    result = evaluate(policy, session, old)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "event_too_old"


def test_a_non_sol_quote_is_refused(policy: SignalPolicy, session: Session) -> None:
    event = make_swap_event(quote_mint=USDC_MINT, quote_decimals=6)
    result = evaluate(policy, session, event)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "unsupported_token"


def test_a_blocked_mint_is_refused(
    clock: FrozenClock, positions: PositionRepository, risk, session: Session
) -> None:
    policy = SignalPolicy(
        PolicyConfig(blocked_mints=(MINT_X,)), clock=clock, positions=positions, risk=risk
    )
    result = evaluate(policy, session, make_swap_event(base_mint=MINT_X))
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "unsupported_token"


def test_a_size_below_the_minimum_is_refused(
    clock: FrozenClock, positions: PositionRepository, risk, session: Session
) -> None:
    policy = SignalPolicy(
        PolicyConfig(base_buy_size_sol=Decimal("0.02"), min_buy_size_sol=Decimal("0.02")),
        clock=clock,
        positions=positions,
        risk=risk,
    )
    selection = active_selection(make_selection(wallets=(WALLET_A, WALLET_B)))  # halves the size
    result = evaluate(policy, session, make_swap_event(), selection=selection)
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "size_below_minimum"


def test_a_duplicate_event_is_refused(policy: SignalPolicy, session: Session) -> None:
    event = make_swap_event()
    first = evaluate(policy, session, event)
    session.add(
        StoredCopyIntent(
            intent_id=first.intent.intent_id,
            source_event_id=event.event_id,
            source_wallet=event.wallet,
            decision=first.intent.decision.value,
            reason_code=None,
            side=event.side.value,
            base_mint=event.base_mint,
            quote_mint=event.quote_mint,
            mode="PAPER",
            selection_id="test_selection",
            policy_version="0.1.0",
            created_at_utc=T0,
            expires_at_utc=None,
            settled=False,
            payload=first.intent.to_wire(),
        )
    )
    session.flush()

    second = evaluate(policy, session, event)
    assert second.intent.reason_code is not None
    assert second.intent.reason_code.value == "duplicate_event"


def test_the_kill_switch_blocks_the_decision(policy: SignalPolicy, session: Session, risk) -> None:
    risk.kill_switch.engage("manual stop")
    result = evaluate(policy, session, make_swap_event())
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "kill_switch_active"


# -- dedup ------------------------------------------------------------------------------------
def test_first_signal_only_suppresses_a_second_wallet(
    policy: SignalPolicy, session: Session, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000,
        quote_spent_raw=1_000,
        now=T0,
    )
    selection = active_selection(make_selection(wallets=(WALLET_A, WALLET_B)))
    result = evaluate(policy, session, make_swap_event(wallet=WALLET_B), selection=selection)

    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "dedup_policy_suppressed"
    assert result.intent.dedup_policy is DedupPolicy.FIRST_SIGNAL_ONLY


def test_consensus_required_is_refused_rather_than_silently_downgraded(
    clock: FrozenClock, positions: PositionRepository, risk, session: Session
) -> None:
    """Pretending to implement a policy is worse than refusing to run it."""
    policy = SignalPolicy(
        PolicyConfig(dedup_policy=DedupPolicy.CONSENSUS_REQUIRED),
        clock=clock,
        positions=positions,
        risk=risk,
    )
    result = evaluate(policy, session, make_swap_event())
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "dedup_policy_suppressed"
    assert "not implemented" in (result.detail or "")


# -- sells ---------------------------------------------------------------------------------------
def test_a_sell_without_a_copy_position_is_logged_not_executed(
    policy: SignalPolicy, session: Session
) -> None:
    result = evaluate(policy, session, make_swap_event(side=Side.SELL))
    assert result.intent.reason_code is not None
    assert result.intent.reason_code.value == "no_copy_position_to_sell"


def test_a_partial_sell_copies_the_fraction_not_the_amount(
    policy: SignalPolicy, session: Session, positions: PositionRepository
) -> None:
    """The trader sells 30 % of their holding, so GundiX sells 30 % of its own."""
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=sol_to_lamports(Decimal("0.05")),
        now=T0,
    )
    event = make_swap_event(side=Side.SELL, base_amount_raw=300_000_000)
    context = SignalContext(source_pre_base_balance_raw=1_000_000_000)

    result = evaluate(policy, session, event, ctx=context)

    assert result.is_execute
    assert result.intent.target_fraction_bps == 3_000
    assert result.intent.target_base_raw == 300_000


def test_a_full_exit_sells_everything(
    policy: SignalPolicy, session: Session, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=sol_to_lamports(Decimal("0.05")),
        now=T0,
    )
    event = make_swap_event(side=Side.SELL, base_amount_raw=1_000_000_000)
    result = evaluate(
        policy, session, event, ctx=SignalContext(source_pre_base_balance_raw=1_000_000_000)
    )

    assert result.intent.target_fraction_bps == 10_000
    assert result.intent.target_base_raw == 1_000_000


def test_an_unobservable_sell_fraction_becomes_a_recorded_full_exit(
    policy: SignalPolicy, session: Session, positions: PositionRepository
) -> None:
    """The fallback is conservative and must never be silent."""
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000_000,
        quote_spent_raw=sol_to_lamports(Decimal("0.05")),
        now=T0,
    )
    result = evaluate(policy, session, make_swap_event(side=Side.SELL), ctx=SignalContext())

    assert result.intent.target_fraction_bps == 10_000
    assert "unobservable" in (result.intent.reason_detail or "")


def test_a_sell_can_never_exceed_the_copy_position(
    policy: SignalPolicy, session: Session, positions: PositionRepository
) -> None:
    positions.apply_buy(
        session,
        source_wallet=WALLET_A,
        base_mint=MINT_X,
        base_filled_raw=1_000,
        quote_spent_raw=1_000,
        now=T0,
    )
    event = make_swap_event(side=Side.SELL, base_amount_raw=999_999_999_999)
    result = evaluate(policy, session, event, ctx=SignalContext(source_pre_base_balance_raw=1_000))
    assert result.intent.target_base_raw is not None
    assert result.intent.target_base_raw <= 1_000


# -- broker ------------------------------------------------------------------------------------
def _buy_intent(policy: SignalPolicy, session: Session):
    return evaluate(policy, session, make_swap_event(side=Side.BUY)).intent


def _context(event=None) -> ExecutionContext:
    event = event or make_swap_event()
    return ExecutionContext(
        base_decimals=event.base_decimals,
        quote_decimals=event.quote_decimals,
        reference_price=event.price_in_quote(),
        block_time_utc=event.block_time_utc,
        received_at_utc=event.observed_at_utc,
        decoded_at_utc=event.observed_at_utc,
        decided_at_utc=T0,
    )


def test_a_paper_buy_fills_and_opens_a_position(
    policy: SignalPolicy, broker: PaperBroker, session: Session, positions: PositionRepository
) -> None:
    intent = _buy_intent(policy, session)
    result = broker.execute(session, intent, _context())

    assert result.status is ExecutionStatus.FILLED
    assert result.is_economically_effective
    assert result.filled_base_raw > 0
    assert result.filled_quote_raw == intent.target_size_quote_raw
    assert result.signature is None, "paper must never carry a signature"
    assert (
        positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == result.filled_base_raw
    )


def test_a_paper_fill_records_the_latency_stages(
    policy: SignalPolicy, broker: PaperBroker, session: Session
) -> None:
    result = broker.execute(session, _buy_intent(policy, session), _context())
    latencies = result.latencies_ms

    assert latencies.block_to_receive is not None
    assert latencies.receive_to_decode is not None
    assert latencies.decode_to_decision is not None
    # Paper never submits, so claiming a confirmation time would be inventing a measurement.
    assert latencies.submit_to_confirm is None


def test_an_expired_intent_does_not_fill(
    policy: SignalPolicy, broker: PaperBroker, session: Session, clock: FrozenClock
) -> None:
    intent = _buy_intent(policy, session)
    clock.advance(120)
    result = broker.execute(session, intent, _context())

    assert result.status is ExecutionStatus.EXPIRED
    assert not result.is_economically_effective
    assert result.error_code is not None


def test_a_stale_quote_does_not_fill(
    policy: SignalPolicy,
    session: Session,
    positions: PositionRepository,
    quotes,
    clock: FrozenClock,
) -> None:
    """The quote is fetched, then time passes before it is used."""

    class SlowClockBroker(PaperBroker):
        def _attempt(self, session, intent, context, attempt, started):
            clock.advance(60)
            return super()._attempt(session, intent, context, attempt, started)

    broker = SlowClockBroker(
        PaperConfig(land_probability=Decimal("1"), rng_seed="t"),
        clock=clock,
        quotes=quotes,
        positions=positions,
    )
    intent = _buy_intent(policy, session)
    result = broker.execute(session, intent, _context())
    assert result.status is ExecutionStatus.EXPIRED


def test_the_landing_model_is_deterministic(
    policy: SignalPolicy, session: Session, quotes, clock, tmp_path
) -> None:
    """Same seed, same intent, same outcome - otherwise a disagreement means nothing.

    Each run gets its own database, because running the same intent twice against one
    database is exactly what the double-fill invariant forbids.
    """
    from src.common.db import Base, create_db_engine, create_session_factory

    config = PaperConfig(land_probability=Decimal("0.5"), rng_seed="fixed-seed")
    intent = _buy_intent(policy, session)

    outcomes = []
    for run in range(3):
        engine = create_db_engine(f"sqlite:///{tmp_path / f'run{run}.sqlite'}")
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        broker = PaperBroker(config, clock=clock, quotes=quotes, positions=PositionRepository())
        with factory() as isolated:
            outcomes.append(broker.execute(isolated, intent, _context()).status)
            isolated.commit()
        engine.dispose()
    assert len(set(outcomes)) == 1


def test_an_intent_can_never_have_two_effective_fills(
    policy: SignalPolicy, broker: PaperBroker, session: Session
) -> None:
    """The core money invariant, enforced by the database rather than by a code path.

    A retry of an intent that already filled must be impossible even if some future caller
    forgets to check first.
    """
    from sqlalchemy.exc import IntegrityError

    intent = _buy_intent(policy, session)
    first = broker.execute(session, intent, _context())
    assert first.is_economically_effective

    with pytest.raises(IntegrityError, match=r"execution_results\.intent_id"):
        broker.execute(session, intent, _context())
        session.flush()

    # The transaction is now poisoned, which is the correct outcome: the whole unit of work
    # is discarded rather than half-applied.
    session.rollback()


def test_a_non_landing_fill_leaves_the_position_untouched(
    policy: SignalPolicy, session: Session, positions: PositionRepository, quotes, clock
) -> None:
    broker = PaperBroker(
        PaperConfig(land_probability=Decimal("0"), rng_seed="never"),
        clock=clock,
        quotes=quotes,
        positions=positions,
    )
    intent = _buy_intent(policy, session)
    result = broker.execute(session, intent, _context())

    assert result.status is ExecutionStatus.FAILED
    assert result.error_code is not None and result.error_code.value == "SIMULATED_FAIL"
    assert positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == 0


def test_a_slippage_floor_breach_is_rejected(
    policy: SignalPolicy, session: Session, positions: PositionRepository, quotes, clock
) -> None:
    """Extra slippage larger than the tolerance must not fill."""
    broker = PaperBroker(
        PaperConfig(land_probability=Decimal("1"), extra_slippage_bps=5_000, rng_seed="t"),
        clock=clock,
        quotes=quotes,
        positions=positions,
    )
    result = broker.execute(session, _buy_intent(policy, session), _context())

    assert result.status is ExecutionStatus.REJECTED
    assert result.error_code is not None and result.error_code.value == "SLIPPAGE_EXCEEDED"
    assert positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == 0


def test_price_impact_grows_with_size(quotes) -> None:
    from src.paper.quotes import QuoteRequest

    small = quotes.quote(
        QuoteRequest(
            in_mint="So11111111111111111111111111111111111111112",
            out_mint=MINT_X,
            in_amount_raw=sol_to_lamports(Decimal("0.05")),
            in_decimals=9,
            out_decimals=6,
            slippage_bps=300,
            reference_price=Decimal("0.02"),
        )
    )
    large = quotes.quote(
        QuoteRequest(
            in_mint="So11111111111111111111111111111111111111112",
            out_mint=MINT_X,
            in_amount_raw=sol_to_lamports(Decimal("25")),
            in_decimals=9,
            out_decimals=6,
            slippage_bps=300,
            reference_price=Decimal("0.02"),
        )
    )
    assert large.price_impact_bps > small.price_impact_bps
