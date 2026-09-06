"""Safety separation, preflight and reconciliation (B9, B10, S0 8.2/8.3).

These are the tests whose failure would mean the system can do something it must not do.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from gundix_contracts.enums import OperatingMode
from sqlalchemy.orm import Session

from src.common.clock import FrozenClock
from src.common.config import RuntimeConfig, SelectionConfig
from src.common.mode import ModeError, assert_live_confirmation, resolve_mode
from src.execution.live_gate import (
    LiveBlockedError,
    assert_live_allowed,
    gate_intent,
    run_preflight,
)
from src.execution.reconcile import Reconciler
from src.paper.positions import PositionRepository
from src.paper.risk import KillSwitch
from src.paper.selection import ActiveSelection
from src.paper.store import ExecutionAttempt, SourceWalletPosition, TokenExposure
from tests.factories import T0, WALLET_A, make_selection

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- mode resolution (S0 8.2) ---------------------------------------------------------------
def test_an_empty_environment_never_yields_live() -> None:
    """The test S0 8.2 requires both repos to carry."""
    with pytest.raises(ModeError):
        resolve_mode({})


def test_plan_b_refuses_to_start_without_a_mode() -> None:
    """Plan A may fall back to RESEARCH; Plan B, which can execute, must not guess."""
    with pytest.raises(ModeError, match="does not guess"):
        resolve_mode({"GUNDIX_MODE": "   "})


def test_an_invalid_mode_is_refused_rather_than_interpreted() -> None:
    with pytest.raises(ModeError, match="Refusing to guess"):
        resolve_mode({"GUNDIX_MODE": "live-ish"})


@pytest.mark.parametrize("mode", ["RESEARCH", "OBSERVE", "PAPER", "SHADOW"])
def test_valid_non_live_modes_resolve(mode: str) -> None:
    assert resolve_mode({"GUNDIX_MODE": mode}).value == mode


def test_live_needs_a_second_independent_switch() -> None:
    with pytest.raises(ModeError, match="GUNDIX_LIVE_CONFIG"):
        resolve_mode({"GUNDIX_MODE": "LIVE"})


def test_live_needs_confirmation_naming_the_actual_selection() -> None:
    """Confirming 'live trading' in the abstract is not enough; it names this watchlist."""
    with pytest.raises(ModeError, match="GUNDIX_LIVE_CONFIRM"):
        assert_live_confirmation(OperatingMode.LIVE, "sel_abc", {})

    with pytest.raises(ModeError, match="does not match"):
        assert_live_confirmation(
            OperatingMode.LIVE, "sel_abc", {"GUNDIX_LIVE_CONFIRM": "sel_other"}
        )

    assert_live_confirmation(OperatingMode.LIVE, "sel_abc", {"GUNDIX_LIVE_CONFIRM": "sel_abc"})


def test_confirmation_is_irrelevant_outside_live() -> None:
    assert_live_confirmation(OperatingMode.PAPER, "sel_abc", {})


# -- signer isolation (S0 8.3) -----------------------------------------------------------------
@pytest.mark.parametrize("mode", ["PAPER", "SHADOW", "OBSERVE", "RESEARCH", ""])
def test_the_signer_module_is_not_importable_outside_live(mode: str) -> None:
    """Not "refuses to work" - not importable at all."""
    env = {"GUNDIX_MODE": mode, "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    result = subprocess.run(
        [sys.executable, "-c", "import src.execution.signer"],
        cwd=REPO_ROOT,
        env={**env, "PYTHONPATH": f"{REPO_ROOT};{REPO_ROOT / 'contracts' / 'python'}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "must not be imported outside LIVE" in result.stderr


def test_the_signer_is_not_implemented_even_in_live() -> None:
    """Building one before the Go/No-Go evidence exists would leave only a config flag
    between the system and real money."""
    result = subprocess.run(
        [sys.executable, "-c", "from src.execution.signer import Signer; Signer()"],
        cwd=REPO_ROOT,
        env={
            "GUNDIX_MODE": "LIVE",
            "PATH": "",
            "SYSTEMROOT": "C:\\Windows",
            "PYTHONPATH": f"{REPO_ROOT};{REPO_ROOT / 'contracts' / 'python'}",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "no signer is implemented" in result.stderr


def test_no_private_key_material_anywhere_in_the_repository() -> None:
    """A key in a fixture is a key in the git history forever."""
    # Assembled at runtime so this file does not trip its own scan.
    suspicious = ("PRIVATE" + " KEY", "BEGIN" + " RSA", "keypair" + ".json", "secret" + "Key")
    roots = [REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "contracts", REPO_ROOT / "config"]
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".sqlite"}:
                continue
            if path == Path(__file__):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for needle in suspicious:
                assert needle not in text, f"{path} mentions {needle!r}"


def test_live_execution_is_blocked_in_this_build() -> None:
    with pytest.raises(LiveBlockedError, match="blocked in this build"):
        assert_live_allowed()


# -- preflight (B9) --------------------------------------------------------------------------------
def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(selection=SelectionConfig(path=tmp_path / "sel.json"))


def _active(now=T0) -> ActiveSelection:
    selection = make_selection(wallets=(WALLET_A,), now=now)
    return ActiveSelection(
        selection=selection,
        content_sha256="0" * 64,
        source_path=Path("test"),
        loaded_at_utc=now,
        by_wallet={entry.wallet: entry for entry in selection.wallets},
    )


def test_preflight_passes_when_everything_is_in_order(session: Session, tmp_path: Path) -> None:
    report = run_preflight(
        session,
        config=_config(tmp_path),
        mode=OperatingMode.PAPER,
        selection=_active(),
        clock=FrozenClock(T0),
        kill_switch=KillSwitch(tmp_path / "KILL"),
        migrations_current=True,
        rpc_reachable=True,
        quote_provider_reachable=True,
        trusted_now=T0,
    )
    assert report.passed, report.render()


def test_preflight_reports_every_blocker_at_once(session: Session, tmp_path: Path) -> None:
    """Fixing one blocker only to discover the next wastes time and invites disabling checks."""
    kill = KillSwitch(tmp_path / "KILL")
    kill.engage("test")
    report = run_preflight(
        session,
        config=_config(tmp_path),
        mode=OperatingMode.PAPER,
        selection=None,
        clock=FrozenClock(T0),
        kill_switch=kill,
        migrations_current=False,
        rpc_reachable=False,
        quote_provider_reachable=False,
        trusted_now=T0,
    )
    names = {name for name, _ in report.failures}
    assert not report.passed
    assert {
        "selection_loaded",
        "rpc_reachable",
        "db_migrations_current",
        "kill_switch_inactive",
    } <= names


def test_preflight_blocks_on_an_implausible_clock(session: Session, tmp_path: Path) -> None:
    report = run_preflight(
        session,
        config=_config(tmp_path),
        mode=OperatingMode.PAPER,
        selection=_active(),
        clock=FrozenClock(T0),
        kill_switch=KillSwitch(tmp_path / "KILL"),
        migrations_current=True,
        rpc_reachable=True,
        quote_provider_reachable=True,
        trusted_now=T0 + timedelta(minutes=5),
    )
    assert "system_clock_plausible" in {name for name, _ in report.failures}


def test_preflight_blocks_on_an_open_attempt(session: Session, tmp_path: Path) -> None:
    session.add(
        ExecutionAttempt(
            intent_id="a" * 64, attempt=1, broker="paper", started_at_utc=T0, finished_at_utc=None
        )
    )
    session.flush()
    report = run_preflight(
        session,
        config=_config(tmp_path),
        mode=OperatingMode.PAPER,
        selection=_active(),
        clock=FrozenClock(T0),
        kill_switch=KillSwitch(tmp_path / "KILL"),
        migrations_current=True,
        rpc_reachable=True,
        quote_provider_reachable=True,
        trusted_now=T0,
    )
    assert "no_open_inconsistent_intents" in {name for name, _ in report.failures}


# -- per-intent gate ----------------------------------------------------------------------------------
def test_the_intent_gate_blocks_each_of_its_conditions(tmp_path: Path) -> None:
    from gundix_contracts.models import QuoteSnapshot

    from tests.paper.test_policy_and_broker import active_selection  # reuse the builder

    quote = QuoteSnapshot(
        provider="test",
        requested_at_utc=T0,
        expires_at_utc=T0 + timedelta(seconds=10),
        in_amount_raw=1,
        out_amount_raw=1,
        price_impact_bps=0,
        route_hops=1,
    )
    intent = _dummy_intent()
    kill = KillSwitch(tmp_path / "KILL")
    selection = active_selection()

    assert gate_intent(
        intent,
        now=T0,
        selection=selection,
        quote=quote,
        kill_switch=kill,
        position_reconciled=True,
    ).allowed

    kill.engage("stop")
    assert (
        gate_intent(
            intent,
            now=T0,
            selection=selection,
            quote=quote,
            kill_switch=kill,
            position_reconciled=True,
        ).reason
        == "kill_switch_active"
    )
    (tmp_path / "KILL").unlink()

    assert (
        gate_intent(
            intent,
            now=T0 + timedelta(minutes=5),
            selection=selection,
            quote=quote,
            kill_switch=kill,
            position_reconciled=True,
        ).reason
        == "event_too_old"
    )

    assert (
        gate_intent(
            intent,
            now=T0,
            selection=selection,
            quote=None,
            kill_switch=kill,
            position_reconciled=True,
        ).reason
        == "no_route"
    )

    assert gate_intent(
        intent,
        now=T0 + timedelta(seconds=20),
        selection=selection,
        quote=quote,
        kill_switch=kill,
        position_reconciled=True,
    ).reason in {"event_too_old", "quote_stale"}

    assert (
        gate_intent(
            intent,
            now=T0,
            selection=selection,
            quote=quote,
            kill_switch=kill,
            position_reconciled=False,
        ).reason
        == "state_mismatch"
    )


def _dummy_intent():
    from gundix_contracts.enums import Decision, DedupPolicy, Side
    from gundix_contracts.models import SCHEMA_VERSIONS, CopyIntent

    from tests.factories import MINT_X

    return CopyIntent(
        schema_version=SCHEMA_VERSIONS["copy_intent"],
        intent_id="b" * 64,
        source_event_id="c" * 64,
        source_wallet=WALLET_A,
        created_at_utc=T0,
        base_mint=MINT_X,
        quote_mint="So11111111111111111111111111111111111111112",
        side=Side.BUY,
        target_size_quote_raw=1_000,
        target_fraction_bps=None,
        target_base_raw=None,
        max_slippage_bps=300,
        expires_at_utc=T0 + timedelta(seconds=20),
        mode=OperatingMode.PAPER,
        decision=Decision.EXECUTE,
        reason_code=None,
        selection_id="test_selection",
        policy_version="0.1.0",
        dedup_policy=DedupPolicy.FIRST_SIGNAL_ONLY,
    )


# -- reconciliation (B10) -------------------------------------------------------------------------------
def test_reconciliation_is_clean_on_an_empty_database(session: Session) -> None:
    report = Reconciler(clock=FrozenClock(T0), positions=PositionRepository()).run(session)
    assert not report.blocks_execution
    assert report.render() == "no discrepancies"


def test_a_stale_open_attempt_blocks(session: Session) -> None:
    session.add(
        ExecutionAttempt(
            intent_id="d" * 64, attempt=1, broker="paper", started_at_utc=T0, finished_at_utc=None
        )
    )
    session.flush()
    clock = FrozenClock(T0 + timedelta(hours=1))
    report = Reconciler(clock=clock, positions=PositionRepository()).run(session)

    assert report.blocks_execution
    assert any(item.kind == "open_attempt" for item in report.discrepancies)


def test_exposure_drift_is_repaired_deterministically(session: Session) -> None:
    """Positions are the source of truth; the aggregate is derived, so recomputing is safe."""
    from tests.factories import MINT_X

    session.add(
        SourceWalletPosition(
            source_wallet=WALLET_A,
            base_mint=MINT_X,
            base_amount_raw="1000",
            quote_cost_raw="500",
            quote_proceeds_raw="0",
            opened_at_utc=T0,
            updated_at_utc=T0,
        )
    )
    session.add(
        TokenExposure(
            base_mint=MINT_X,
            base_amount_raw="999999",
            quote_cost_raw="0",
            open_positions=7,
            updated_at_utc=T0,
        )
    )
    session.flush()

    report = Reconciler(clock=FrozenClock(T0), positions=PositionRepository()).run(session)
    exposure = session.get(TokenExposure, MINT_X)

    assert exposure is not None
    assert exposure.base_amount_raw == "1000"
    assert exposure.open_positions == 1
    assert not report.blocks_execution, "a derived aggregate is safe to recompute"
    assert any(item.kind == "exposure_drift" for item in report.discrepancies)


def test_a_negative_position_blocks(session: Session) -> None:
    from tests.factories import MINT_X

    session.add(
        SourceWalletPosition(
            source_wallet=WALLET_A,
            base_mint=MINT_X,
            base_amount_raw="-5",
            quote_cost_raw="0",
            quote_proceeds_raw="0",
            opened_at_utc=T0,
            updated_at_utc=T0,
        )
    )
    session.flush()
    report = Reconciler(clock=FrozenClock(T0), positions=PositionRepository()).run(session)

    assert report.blocks_execution
    assert any(item.kind == "negative_position" for item in report.discrepancies)
