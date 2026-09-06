"""Reconciliation and recovery (02_PLAN_B B10).

Runs at startup and periodically. It answers: *does what we believe match what happened?*

The rule that shapes every branch here: **automatic repair is only allowed where it is
deterministic and tested.** Anything ambiguous blocks new trades and keeps all diagnostic
data, because a wrong automatic repair on a position is worse than a stopped bot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gundix_contracts.enums import ExecutionStatus
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.common.clock import Clock
from src.common.logging import get_logger, log_event
from src.paper.positions import PositionRepository
from src.paper.store import (
    ExecutionAttempt,
    SourceWalletPosition,
    StoredCopyIntent,
    StoredExecutionResult,
    TokenExposure,
)

logger = get_logger(__name__)

#: An attempt still open after this long was interrupted rather than merely slow.
STALE_ATTEMPT_SECONDS = 300.0


@dataclass(slots=True)
class Discrepancy:
    kind: str
    identifier: str
    detail: str
    blocking: bool = True


@dataclass(slots=True)
class ReconciliationReport:
    checked_at_utc: datetime
    discrepancies: list[Discrepancy] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    @property
    def blocks_execution(self) -> bool:
        return any(item.blocking for item in self.discrepancies)

    def render(self) -> str:
        if not self.discrepancies:
            return "no discrepancies"
        return "\n".join(
            f"[{'BLOCK' if d.blocking else 'note '}] {d.kind} {d.identifier}: {d.detail}"
            for d in self.discrepancies
        )


class Reconciler:
    def __init__(self, *, clock: Clock, positions: PositionRepository) -> None:
        self._clock = clock
        self._positions = positions

    def run(self, session: Session) -> ReconciliationReport:
        now = self._clock.now()
        report = ReconciliationReport(checked_at_utc=now)

        self._check_open_attempts(session, report, now)
        self._check_unknown_results(session, report)
        self._check_executed_intents_have_results(session, report)
        self._check_exposure_matches_positions(session, report, now)
        self._check_no_negative_positions(session, report)

        log_event(
            logger,
            40 if report.blocks_execution else 20,
            "reconciliation complete",
            discrepancies=len(report.discrepancies),
            blocking=report.blocks_execution,
            repaired=len(report.repaired),
        )
        return report

    # -- checks -------------------------------------------------------------------------
    def _check_open_attempts(
        self, session: Session, report: ReconciliationReport, now: datetime
    ) -> None:
        """An attempt with no result is a process that died mid-flight."""
        cutoff = now - timedelta(seconds=STALE_ATTEMPT_SECONDS)
        rows = (
            session.execute(
                select(ExecutionAttempt).where(ExecutionAttempt.finished_at_utc.is_(None))
            )
            .scalars()
            .all()
        )
        for row in rows:
            has_result = session.execute(
                select(StoredExecutionResult.result_id).where(
                    StoredExecutionResult.intent_id == row.intent_id,
                    StoredExecutionResult.attempt == row.attempt,
                )
            ).first()
            if has_result is not None:
                # Deterministic and safe: the result exists, the attempt row simply never
                # got its closing write. Closing it loses no information.
                row.finished_at_utc = now
                row.outcome = "recovered"
                report.repaired.append(f"attempt {row.intent_id[:16]}#{row.attempt}")
                continue
            if row.started_at_utc < cutoff:
                report.discrepancies.append(
                    Discrepancy(
                        kind="open_attempt",
                        identifier=f"{row.intent_id[:16]}#{row.attempt}",
                        detail=(
                            f"started {row.started_at_utc.isoformat()} with no result. "
                            "For a paper broker nothing left the process, so this is safe to "
                            "review; in LIVE it must be resolved against the chain before "
                            "any retry."
                        ),
                    )
                )

    def _check_unknown_results(self, session: Session, report: ReconciliationReport) -> None:
        rows = (
            session.execute(
                select(StoredExecutionResult).where(
                    StoredExecutionResult.status == ExecutionStatus.UNKNOWN.value
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            report.discrepancies.append(
                Discrepancy(
                    kind="unknown_result",
                    identifier=row.result_id[:16],
                    detail=(
                        "status UNKNOWN is a blocking state, not a result. It must be resolved "
                        "against the chain and is never reinterpreted as FAILED."
                    ),
                )
            )

    def _check_executed_intents_have_results(
        self, session: Session, report: ReconciliationReport
    ) -> None:
        rows = (
            session.execute(
                select(StoredCopyIntent).where(
                    StoredCopyIntent.decision == "EXECUTE",
                    StoredCopyIntent.settled.is_(False),
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            result = session.execute(
                select(StoredExecutionResult.result_id).where(
                    StoredExecutionResult.intent_id == row.intent_id
                )
            ).first()
            if result is None:
                report.discrepancies.append(
                    Discrepancy(
                        kind="intent_without_result",
                        identifier=row.intent_id[:16],
                        detail=f"EXECUTE intent created {row.created_at_utc.isoformat()} was never attempted",
                    )
                )
            else:
                row.settled = True
                report.repaired.append(f"settled intent {row.intent_id[:16]}")

    def _check_exposure_matches_positions(
        self, session: Session, report: ReconciliationReport, now: datetime
    ) -> None:
        """Aggregate exposure must equal the sum of the positions it aggregates."""
        per_mint: dict[str, tuple[int, int, int]] = {}
        for position in session.execute(select(SourceWalletPosition)).scalars().all():
            base = int(position.base_amount_raw or 0)
            cost = int(position.quote_cost_raw or 0)
            total_base, total_cost, count = per_mint.get(position.base_mint, (0, 0, 0))
            per_mint[position.base_mint] = (
                total_base + base,
                total_cost + cost,
                count + (1 if base > 0 else 0),
            )

        for row in session.execute(select(TokenExposure)).scalars().all():
            expected = per_mint.get(row.base_mint, (0, 0, 0))
            actual = (
                int(row.base_amount_raw or 0),
                int(row.quote_cost_raw or 0),
                row.open_positions,
            )
            if actual != expected:
                # Deterministic: the positions are the source of truth and the aggregate is
                # derived from them, so recomputing it cannot lose information.
                row.base_amount_raw = str(expected[0])
                row.quote_cost_raw = str(expected[1])
                row.open_positions = expected[2]
                row.updated_at_utc = now
                report.repaired.append(f"exposure {row.base_mint}")
                report.discrepancies.append(
                    Discrepancy(
                        kind="exposure_drift",
                        identifier=row.base_mint,
                        detail=f"aggregate {actual} did not match positions {expected}; recomputed",
                        blocking=False,
                    )
                )

    def _check_no_negative_positions(self, session: Session, report: ReconciliationReport) -> None:
        for position in session.execute(select(SourceWalletPosition)).scalars().all():
            if int(position.base_amount_raw or 0) < 0 or int(position.quote_cost_raw or 0) < 0:
                report.discrepancies.append(
                    Discrepancy(
                        kind="negative_position",
                        identifier=f"{position.source_wallet[:8]}/{position.base_mint[:8]}",
                        detail=(
                            f"base={position.base_amount_raw} cost={position.quote_cost_raw}. "
                            "A negative copy position cannot arise from correct arithmetic."
                        ),
                    )
                )


__all__ = ["STALE_ATTEMPT_SECONDS", "Discrepancy", "Reconciler", "ReconciliationReport"]
