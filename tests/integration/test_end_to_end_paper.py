"""End-to-end paper path (integration point I4).

    recorded transaction -> decoder -> SwapEvent -> watchlist -> CopyIntent -> paper fill
                         -> ExecutionResult -> artifact Plan A can read

Two halves, because they prove different things:

* :func:`test_a_real_recorded_transaction_runs_the_whole_path` uses an unmodified mainnet
  payload. It proves the production path works on reality.
* the scenario tests use constructed payloads with the same structure but chosen amounts,
  because I4 asks for buy, partial sell, full exit, duplicate, late event, illiquid token
  and restart - a sequence nobody recorded.

Everything runs through :func:`build_application`, the same composition root the runner
uses. A test that assembled its own pipeline would be testing an arrangement that never
runs in production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from gundix_contracts.artifacts import load_json_artifact, load_jsonl_artifact
from gundix_contracts.enums import (
    ArtifactType,
    Decision,
    ExecutionStatus,
    OperatingMode,
    Side,
)
from gundix_contracts.models import ExecutionResult

from src.common.app import Application, build_application
from src.common.clock import FrozenClock
from src.common.config import (
    DecoderConfig,
    ExportConfig,
    PaperConfig,
    PolicyConfig,
    QuoteConfig,
    RiskConfig,
    RuntimeConfig,
    SelectionConfig,
    StreamConfig,
)
from src.common.db import unit_of_work
from src.paper.store import StoredCopyIntent, StoredExecutionResult, StoredSwapEvent
from src.stream.sources import RawChainEvent
from tests.conftest import GOLDEN_TRANSACTIONS
from tests.factories import MINT_X, WALLET_A, make_selection, write_selection_artifact
from tests.integration import tx_builder

BLOCK_TIME = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
BLOCK_TIME_UNIX = int(BLOCK_TIME.timestamp())


def build_config(
    tmp_path: Path,
    *,
    replay_dir: Path,
    unknown_program_policy: str = "RECONCILE_BALANCES",
    min_liquidity_sol: str = "0",
) -> RuntimeConfig:
    return RuntimeConfig(
        selection=SelectionConfig(path=tmp_path / "selection.json", require_approval=True),
        stream=StreamConfig(source="replay", replay_path=replay_dir),
        policy=PolicyConfig(
            base_buy_size_sol=Decimal("0.05"),
            min_buy_size_sol=Decimal("0.001"),
            max_signal_age_seconds=90,
            intent_ttl_seconds=20,
            min_pool_liquidity_sol=Decimal(min_liquidity_sol),
        ),
        risk=RiskConfig(kill_switch_file=tmp_path / "KILL_SWITCH"),
        paper=PaperConfig(land_probability=Decimal("1"), rng_seed="e2e"),
        quotes=QuoteConfig(provider="simulated"),
        # The scenarios use constructed payloads that contain no unknown program at all, so
        # the policy choice does not change their outcome; it is set explicitly so the test
        # never depends on which CCR-001 decision is in force.
        decoder=DecoderConfig(unknown_program_policy=unknown_program_policy),
        export=ExportConfig(directory=tmp_path / "observations"),
    )


def build_app(
    tmp_path: Path,
    *,
    replay_dir: Path,
    clock: FrozenClock,
    wallets: tuple[str, ...] = (WALLET_A,),
    **config_kwargs: Any,
) -> Application:
    config = build_config(tmp_path, replay_dir=replay_dir, **config_kwargs)
    write_selection_artifact(
        config.selection.path, make_selection(wallets=wallets, now=clock.now())
    )
    app = build_application(
        config,
        OperatingMode.PAPER,
        clock=clock,
        create_tables=True,
        database_url=f"sqlite:///{tmp_path / 'gundix.sqlite'}",
    )
    app.selections.load()
    return app


def feed(app: Application, payload: dict[str, Any], clock: FrozenClock):
    """Hand one transaction to the pipeline exactly as a source would."""
    return app.pipeline.process(
        RawChainEvent(
            signature=payload["transaction"]["signatures"][0],
            slot=int(payload["slot"]),
            received_at_utc=clock.now(),
            source=app.source.source_kind,
            payload=payload,
            wallet_hint=WALLET_A,
        )
    )


# --------------------------------------------------------------------------------------
# the real transaction
# --------------------------------------------------------------------------------------
def test_a_real_recorded_transaction_runs_the_whole_path(tmp_path: Path) -> None:
    """An unmodified mainnet payload, from decode to a fill Plan A can read."""
    fixture = json.loads(
        (GOLDEN_TRANSACTIONS / "raydium_amm_v4_ok0.json").read_text(encoding="utf-8")
    )
    wallet = fixture["wallets"][0]
    payload = fixture["transaction"]

    # Anchored to the transaction's own block time: a recording is hours old by the time it
    # is replayed, and an age check would reject it for a reason that has nothing to do
    # with the path under test.
    clock = FrozenClock(datetime.fromtimestamp(payload["blockTime"], tz=UTC) + timedelta(seconds=2))
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "case.json").write_text(json.dumps(fixture), encoding="utf-8")

    app = build_app(tmp_path, replay_dir=replay_dir, clock=clock, wallets=(wallet,))
    try:
        outcomes = app.pipeline.run_once()

        events = [event for outcome in outcomes for event in outcome.events]
        intents = [intent for outcome in outcomes for intent in outcome.intents]
        results = [result for outcome in outcomes for result in outcome.results]

        assert events, "the recorded transaction produced no SwapEvent"
        assert events[0].wallet == wallet
        assert events[0].side is Side.BUY
        assert intents and intents[0].decision is Decision.EXECUTE
        assert results and results[0].status is ExecutionStatus.FILLED

        # And the position moved, because a fill actually happened.
        with unit_of_work(app.session_factory) as session:
            position = app.positions.get_position(session, wallet, events[0].base_mint)
        assert position.base_amount_raw == results[0].filled_base_raw

        artifacts = app.exporter.export(
            latencies=app.pipeline.latency_observations,
            results=app.pipeline.execution_results,
            coverage=app.pipeline.coverage,
            window_start=clock.now(),
            window_end=clock.now(),
        )
        assert artifacts.paper_fills_path is not None
        loaded = load_jsonl_artifact(
            artifacts.paper_fills_path, expected_type=ArtifactType.PAPER_FILLS
        )
        assert len(loaded.records) == 1
        # Plan A reads it back through the shared contract, with no transformation script.
        assert ExecutionResult.model_validate(loaded.records[0]).status is ExecutionStatus.FILLED
    finally:
        app.close()


# --------------------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------------------
@pytest.fixture
def scenario(tmp_path: Path):
    clock = FrozenClock(BLOCK_TIME + timedelta(seconds=2))
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    app = build_app(tmp_path, replay_dir=replay_dir, clock=clock)
    yield app, clock, tmp_path
    app.close()


def test_buy_then_partial_sell_then_full_exit(scenario) -> None:
    """The sequence I4 asks for, with the fraction rule doing real work in the middle."""
    app, clock, _ = scenario

    buy = tx_builder.pumpfun_buy(
        signature="1" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX,
    )
    outcome = feed(app, buy, clock)
    assert outcome.results and outcome.results[0].status is ExecutionStatus.FILLED
    bought = outcome.results[0].filled_base_raw

    with unit_of_work(app.session_factory) as session:
        assert app.positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == bought

    # The source wallet sells 30 % of what it holds, so GundiX sells 30 % of its own.
    clock.advance(10)
    sell = tx_builder.pumpfun_sell(
        signature="2" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        tokens_in=300_000_000,
        sol_out=18_000_000,
        slot=2,
        block_time=BLOCK_TIME_UNIX + 10,
        wallet_tokens_before=1_000_000_000,
    )
    outcome = feed(app, sell, clock)
    assert outcome.intents[0].target_fraction_bps == 3_000
    assert outcome.results[0].status is ExecutionStatus.FILLED

    with unit_of_work(app.session_factory) as session:
        remaining = app.positions.get_position(session, WALLET_A, MINT_X)
    assert remaining.base_amount_raw == bought - (bought * 3_000 // 10_000)

    # Then the source exits completely.
    clock.advance(10)
    exit_tx = tx_builder.pumpfun_sell(
        signature="3" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        tokens_in=700_000_000,
        sol_out=40_000_000,
        slot=3,
        block_time=BLOCK_TIME_UNIX + 20,
        wallet_tokens_before=700_000_000,
    )
    outcome = feed(app, exit_tx, clock)
    assert outcome.intents[0].target_fraction_bps == 10_000

    with unit_of_work(app.session_factory) as session:
        closed = app.positions.get_position(session, WALLET_A, MINT_X)
        exposure = app.positions.get_exposure(session, MINT_X)
    assert closed.base_amount_raw == 0
    assert exposure.open_positions == 0


def test_a_duplicate_transaction_produces_no_second_fill(scenario) -> None:
    """Replay, reconnect or restart must never turn one trade into two."""
    app, clock, _ = scenario
    buy = tx_builder.pumpfun_buy(
        signature="4" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX,
    )

    first = feed(app, buy, clock)
    second = feed(app, buy, clock)

    assert first.results and first.results[0].status is ExecutionStatus.FILLED
    assert second.skipped_duplicate
    assert second.results == []

    with unit_of_work(app.session_factory) as session:
        fills = session.query(StoredExecutionResult).all()
        events = session.query(StoredSwapEvent).all()
        position = app.positions.get_position(session, WALLET_A, MINT_X)
    assert len(fills) == 1
    assert len(events) == 1
    assert position.base_amount_raw == first.results[0].filled_base_raw


def test_a_late_event_is_recorded_but_not_traded(scenario) -> None:
    app, clock, _ = scenario
    old = tx_builder.pumpfun_buy(
        signature="5" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX - 3_600,
    )
    outcome = feed(app, old, clock)

    assert outcome.events, "a late event is still decoded and stored"
    assert outcome.intents[0].decision is Decision.NO_TRADE
    assert outcome.intents[0].reason_code is not None
    assert outcome.intents[0].reason_code.value == "event_too_old"
    assert outcome.results == []


def test_a_failed_transaction_produces_no_event_and_no_intent(scenario) -> None:
    app, clock, _ = scenario
    broken = tx_builder.failed(
        tx_builder.pumpfun_buy(
            signature="6" * 88,
            wallet=WALLET_A,
            mint=MINT_X,
            sol_in=50_000_000,
            tokens_out=1_000_000_000,
            slot=1,
            block_time=BLOCK_TIME_UNIX,
        )
    )
    outcome = feed(app, broken, clock)

    assert outcome.events == []
    assert outcome.intents == []
    assert app.pipeline.coverage.transactions_failed_onchain == 1


def test_a_plain_transfer_is_never_a_swap(scenario) -> None:
    """S0 rule 4: an SPL transfer with no swap program is not a trade."""
    app, clock, _ = scenario
    transfer = tx_builder.plain_transfer(
        signature="7" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        amount=500_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX,
        wallet_tokens_before=1_000_000_000,
    )
    outcome = feed(app, transfer, clock)

    assert outcome.events == []
    assert outcome.intents == []


def test_a_sell_without_a_copy_position_is_logged_not_executed(scenario) -> None:
    app, clock, _ = scenario
    sell = tx_builder.pumpfun_sell(
        signature="8" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        tokens_in=300_000_000,
        sol_out=18_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX,
        wallet_tokens_before=1_000_000_000,
    )
    outcome = feed(app, sell, clock)

    assert outcome.intents[0].reason_code is not None
    assert outcome.intents[0].reason_code.value == "no_copy_position_to_sell"
    assert outcome.results == []


def test_an_illiquid_token_is_refused(tmp_path: Path) -> None:
    """A pool too thin to exit is refused at entry, using liquidity read from the transaction.

    The pool's own SOL balance is visible in the payload, so this costs no extra RPC call
    and cannot be fooled by a quote that happens to look fine at entry size.
    """
    clock = FrozenClock(BLOCK_TIME + timedelta(seconds=2))
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    app = build_app(tmp_path, replay_dir=replay_dir, clock=clock, min_liquidity_sol="20")
    try:
        # The builder's bonding curve holds about 1 SOL, far below the 20 SOL minimum.
        buy = tx_builder.pumpfun_buy(
            signature="9" * 88,
            wallet=WALLET_A,
            mint=MINT_X,
            sol_in=50_000_000,
            tokens_out=1_000_000_000,
            slot=1,
            block_time=BLOCK_TIME_UNIX,
        )
        outcome = feed(app, buy, clock)

        assert outcome.events, "the swap is still decoded and recorded"
        assert outcome.intents[0].decision is Decision.NO_TRADE
        assert outcome.intents[0].reason_code is not None
        assert outcome.intents[0].reason_code.value == "liquidity_too_low"
        assert outcome.results == []
        with unit_of_work(app.session_factory) as session:
            assert app.positions.get_position(session, WALLET_A, MINT_X).base_amount_raw == 0
    finally:
        app.close()


def test_state_survives_a_restart(tmp_path: Path) -> None:
    """A new process, the same database: the position and the daily ledger are still there."""
    clock = FrozenClock(BLOCK_TIME + timedelta(seconds=2))
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()

    app = build_app(tmp_path, replay_dir=replay_dir, clock=clock)
    buy = tx_builder.pumpfun_buy(
        signature="A" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX,
    )
    outcome = feed(app, buy, clock)
    bought = outcome.results[0].filled_base_raw
    app.close()

    # Second process, same files.
    config = build_config(tmp_path, replay_dir=replay_dir)
    restarted = build_application(
        config,
        OperatingMode.PAPER,
        clock=clock,
        create_tables=False,
        database_url=f"sqlite:///{tmp_path / 'gundix.sqlite'}",
    )
    try:
        restarted.selections.load()
        with unit_of_work(restarted.session_factory) as session:
            position = restarted.positions.get_position(session, WALLET_A, MINT_X)
            deployed, _pnl, trades, _fails = restarted.positions.daily(session, clock.now())
            report = restarted.reconciler.run(session)

        assert position.base_amount_raw == bought
        assert deployed > 0 and trades == 1
        assert not report.blocks_execution, report.render()

        # And replaying the same transaction after the restart still does not double-fill.
        again = feed(restarted, buy, clock)
        assert again.skipped_duplicate
    finally:
        restarted.close()


def test_every_decision_is_recorded_for_plan_a(scenario) -> None:
    """Plan A derives the real signal frequency from the rejections, so they must persist."""
    app, clock, _ = scenario
    late = tx_builder.pumpfun_buy(
        signature="B" * 88,
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=BLOCK_TIME_UNIX - 3_600,
    )
    feed(app, late, clock)

    with unit_of_work(app.session_factory) as session:
        rows = session.query(StoredCopyIntent).all()
    assert len(rows) == 1
    assert rows[0].decision == "NO_TRADE"
    assert rows[0].reason_code == "event_too_old"
    assert app.pipeline.coverage.decisions["event_too_old"] == 1


def test_the_coverage_artifact_reports_what_was_not_understood(scenario) -> None:
    app, clock, _tmp_path = scenario
    feed(
        app,
        tx_builder.pumpfun_buy(
            signature="C" * 88,
            wallet=WALLET_A,
            mint=MINT_X,
            sol_in=50_000_000,
            tokens_out=1_000_000_000,
            slot=1,
            block_time=BLOCK_TIME_UNIX,
        ),
        clock,
    )
    artifacts = app.exporter.export(
        latencies=app.pipeline.latency_observations,
        results=app.pipeline.execution_results,
        coverage=app.pipeline.coverage,
        window_start=clock.now(),
        window_end=clock.now(),
    )
    assert artifacts.coverage_path is not None
    loaded = load_json_artifact(
        artifacts.coverage_path, expected_type=ArtifactType.DECODER_COVERAGE
    )
    document = loaded.records[0]
    assert document["transactions_seen"] == 1
    assert document["events_emitted"] == 1
    assert Decimal(document["coverage_ratio"]) == Decimal(1)
