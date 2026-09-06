"""Live safety gates (02_PLAN_B B9).

Nothing here executes anything. It answers one question, twice: at startup, *may this
process run in LIVE at all*, and before each intent, *may this specific trade be built and
sent*.

The design rule is that a gate never returns "probably". Every check is either satisfied or
it blocks, and a check that cannot be evaluated counts as failed. A gate that degrades to a
warning when it cannot verify something is not a gate.

Live execution is additionally blocked unconditionally in this build: no signer and no
execution adapter exist yet, and the Go/No-Go criteria of 00_GESAMTPLAN section 11 have not
been demonstrated. :func:`assert_live_allowed` says so explicitly rather than letting a
future configuration change quietly enable it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gundix_contracts.enums import OperatingMode
from gundix_contracts.models import CopyIntent, QuoteSnapshot
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.common.clock import Clock
from src.common.config import RuntimeConfig
from src.common.logging import get_logger, log_event
from src.paper.risk import KillSwitch
from src.paper.selection import ActiveSelection
from src.paper.store import ExecutionAttempt, StoredExecutionResult

logger = get_logger(__name__)

#: How far the system clock may differ from a trusted reference before we refuse to trade.
MAX_CLOCK_SKEW_SECONDS = 5.0


class LiveBlockedError(RuntimeError):
    """Live execution is not permitted. Always fatal, never a warning."""


@dataclass(slots=True)
class PreflightReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(passed for _name, passed, _detail in self.checks)

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [(name, detail) for name, passed, detail in self.checks if not passed]

    def render(self) -> str:
        lines = [
            f"[{'ok  ' if passed else 'FAIL'}] {name}{f' - {detail}' if detail else ''}"
            for name, passed, detail in self.checks
        ]
        return "\n".join(lines)


def assert_live_allowed() -> None:
    """Unconditional block for this milestone.

    02_PLAN_B section 8: code existing and a passing mock test are not evidence of live
    readiness. Nothing in this build has produced that evidence.
    """
    raise LiveBlockedError(
        "LIVE execution is blocked in this build. Missing: an isolated signer, a verified "
        "execution adapter, demonstrated submit/confirm/reconcile behaviour, a practically "
        "tested kill switch, and the Go/No-Go evidence required by 00_GESAMTPLAN section 11."
    )


def run_preflight(
    session: Session,
    *,
    config: RuntimeConfig,
    mode: OperatingMode,
    selection: ActiveSelection | None,
    clock: Clock,
    kill_switch: KillSwitch,
    migrations_current: bool,
    rpc_reachable: bool,
    quote_provider_reachable: bool,
    expected_live_wallet: str | None = None,
    actual_live_wallet: str | None = None,
    trusted_now: datetime | None = None,
) -> PreflightReport:
    """Every startup check from B9, evaluated once and reported together.

    Reported together on purpose: fixing one blocker only to discover the next one on the
    following run wastes the operator's time and encourages checks to be disabled.
    """
    report = PreflightReport()
    now = clock.now()

    report.add("mode_is_explicit", mode is not None, f"mode={mode.value}")

    if selection is None:
        report.add("selection_loaded", False, "no selection is active")
    else:
        report.add("selection_loaded", True, selection.selection_id)
        report.add(
            "selection_approved",
            selection.approved,
            f"approved_by={selection.selection.manual_approval.approved_by}",
        )
        report.add(
            "selection_not_expired",
            not selection.is_expired_at(now),
            f"expires {selection.selection.expires_at_utc.isoformat()}",
        )
        report.add(
            "selection_has_active_wallets",
            selection.active_wallet_count > 0,
            f"{selection.active_wallet_count} active wallets",
        )

    report.add("rpc_reachable", rpc_reachable)
    report.add("quote_provider_reachable", quote_provider_reachable)
    report.add("db_migrations_current", migrations_current)
    report.add(
        "kill_switch_inactive",
        not kill_switch.is_active(),
        kill_switch.reason() or str(kill_switch.path),
    )

    limits_ok, limits_detail = _limits_within_ceilings(config)
    report.add("limits_within_code_ceilings", limits_ok, limits_detail)

    skew_ok, skew_detail = _clock_plausible(now, trusted_now)
    report.add("system_clock_plausible", skew_ok, skew_detail)

    open_intents = _open_execution_attempts(session)
    report.add(
        "no_open_inconsistent_intents",
        not open_intents,
        f"{len(open_intents)} attempts without a result" if open_intents else "",
    )

    unknown = _unknown_status_results(session)
    report.add(
        "no_unresolved_unknown_results",
        not unknown,
        f"{len(unknown)} results in status UNKNOWN" if unknown else "",
    )

    if mode is OperatingMode.LIVE:
        report.add(
            "live_wallet_matches_expectation",
            bool(expected_live_wallet) and expected_live_wallet == actual_live_wallet,
            f"expected={expected_live_wallet} actual={actual_live_wallet}",
        )

    log_event(
        logger,
        20 if report.passed else 40,
        "preflight complete",
        passed=report.passed,
        failures=[name for name, _ in report.failures],
        mode=mode.value,
    )
    return report


def _limits_within_ceilings(config: RuntimeConfig) -> tuple[bool, str]:
    """The config validator already enforces this; re-checking is cheap and catches drift."""
    from src.common.config import (
        ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL,
        ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL,
        ABSOLUTE_MAX_TRADE_SIZE_SOL,
    )

    risk = config.risk
    checks = (
        risk.max_trade_size_sol <= ABSOLUTE_MAX_TRADE_SIZE_SOL,
        risk.max_position_per_token_sol <= ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL,
        risk.max_total_exposure_sol <= ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL,
    )
    if all(checks):
        return True, (
            f"trade<={risk.max_trade_size_sol} token<={risk.max_position_per_token_sol} "
            f"total<={risk.max_total_exposure_sol} SOL"
        )
    return False, "a configured limit exceeds an absolute code ceiling"


def _clock_plausible(now: datetime, trusted_now: datetime | None) -> tuple[bool, str]:
    if trusted_now is None:
        # Without a reference the only sanity check available is that the clock is not
        # absurd. It is weak, and it is reported as weak.
        wall = time.time()
        drift = abs(now.timestamp() - wall)
        return drift < MAX_CLOCK_SKEW_SECONDS, f"no trusted reference; local drift {drift:.2f}s"
    skew = abs((now - trusted_now).total_seconds())
    return skew < MAX_CLOCK_SKEW_SECONDS, f"skew {skew:.2f}s against the reference"


def _open_execution_attempts(session: Session) -> list[str]:
    rows = (
        session.execute(
            select(ExecutionAttempt.intent_id).where(ExecutionAttempt.finished_at_utc.is_(None))
        )
        .scalars()
        .all()
    )
    return list(rows)


def _unknown_status_results(session: Session) -> list[str]:
    rows = (
        session.execute(
            select(StoredExecutionResult.result_id).where(StoredExecutionResult.status == "UNKNOWN")
        )
        .scalars()
        .all()
    )
    return list(rows)


@dataclass(frozen=True, slots=True)
class IntentGateResult:
    allowed: bool
    reason: str | None = None


def gate_intent(
    intent: CopyIntent,
    *,
    now: datetime,
    selection: ActiveSelection | None,
    quote: QuoteSnapshot | None,
    kill_switch: KillSwitch,
    position_reconciled: bool,
) -> IntentGateResult:
    """The per-intent checks from B9, applied immediately before build/sign/submit."""
    if kill_switch.is_active():
        return IntentGateResult(False, "kill_switch_active")
    if now >= intent.expires_at_utc:
        return IntentGateResult(False, "event_too_old")
    if selection is None or not selection.is_tradeable_wallet(intent.source_wallet):
        return IntentGateResult(False, "wallet_not_selected")
    if selection.is_expired_at(now):
        return IntentGateResult(False, "selection_expired")
    if quote is None:
        return IntentGateResult(False, "no_route")
    if now >= quote.expires_at_utc:
        return IntentGateResult(False, "quote_stale")
    if not position_reconciled:
        return IntentGateResult(False, "state_mismatch")
    return IntentGateResult(True)


def kill_switch_from(config: RuntimeConfig) -> KillSwitch:
    return KillSwitch(Path(config.risk.kill_switch_file))


__all__ = [
    "MAX_CLOCK_SKEW_SECONDS",
    "IntentGateResult",
    "LiveBlockedError",
    "PreflightReport",
    "assert_live_allowed",
    "gate_intent",
    "kill_switch_from",
    "run_preflight",
]
