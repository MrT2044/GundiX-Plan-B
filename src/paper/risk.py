"""Risk limits, kill switch and circuit breakers (02_PLAN_B B5/B9).

Everything here answers one question: *may this trade happen right now?* It never answers
*is this trade a good idea* - that is Plan A's job and Plan B does not have an opinion.

Two properties are deliberate:

* **The absolute ceilings from the code are re-checked here**, not only when the config is
  loaded. A limit that only exists at startup is one refactor away from being bypassed.
* **Daily limits are read from the database, not from memory.** Restarting the process
  must not reset a daily loss limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from gundix_contracts.enums import NoTradeReason
from sqlalchemy.orm import Session

from src.common.config import (
    ABSOLUTE_MAX_OPEN_POSITIONS,
    ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL,
    ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL,
    ABSOLUTE_MAX_TRADE_SIZE_SOL,
    RiskConfig,
)
from src.common.logging import get_logger, log_event
from src.paper.positions import PositionRepository, sol_to_lamports

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: NoTradeReason | None = None
    detail: str | None = None

    @classmethod
    def ok(cls) -> RiskDecision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: NoTradeReason, detail: str) -> RiskDecision:
        return cls(allowed=False, reason=reason, detail=detail)


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """What the engine saw when it decided. Recorded for audit."""

    total_exposure_lamports: int
    token_exposure_lamports: int
    open_positions: int
    daily_deployed_lamports: int
    daily_realized_pnl_lamports: int
    consecutive_failures: int
    free_balance_lamports: int


class KillSwitch:
    """A file on disk. Deliberately the dumbest possible mechanism.

    It works when the process is wedged, when the database is locked and when nobody
    remembers the admin interface. Creating the file stops trading; nothing in the code
    ever removes it.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def is_active(self) -> bool:
        return self._path.exists()

    def reason(self) -> str:
        if not self.is_active():
            return ""
        try:
            return self._path.read_text(encoding="utf-8").strip() or "kill switch file present"
        except OSError:
            return "kill switch file present (unreadable)"

    def engage(self, reason: str) -> None:
        """Used by the circuit breakers. There is no programmatic disengage."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(reason, encoding="utf-8")
        log_event(logger, 50, "kill switch engaged", reason=reason, path=str(self._path))


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        *,
        positions: PositionRepository,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._config = config
        self._positions = positions
        self._kill_switch = kill_switch or KillSwitch(config.kill_switch_file)
        self._enforce_absolute_ceilings()

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    def _enforce_absolute_ceilings(self) -> None:
        """Defence in depth: the config validator already checks these."""
        pairs = (
            ("max_trade_size_sol", self._config.max_trade_size_sol, ABSOLUTE_MAX_TRADE_SIZE_SOL),
            (
                "max_position_per_token_sol",
                self._config.max_position_per_token_sol,
                ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL,
            ),
            (
                "max_total_exposure_sol",
                self._config.max_total_exposure_sol,
                ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL,
            ),
        )
        for name, value, ceiling in pairs:
            if value > ceiling:
                raise ValueError(
                    f"risk.{name}={value} exceeds the absolute code ceiling {ceiling}; "
                    "configuration cannot widen a hard limit"
                )
        if self._config.max_open_positions > ABSOLUTE_MAX_OPEN_POSITIONS:
            raise ValueError("risk.max_open_positions exceeds the absolute code ceiling")

    def snapshot(self, session: Session, now: datetime) -> RiskSnapshot:
        deployed, realized, _trades, failures = self._positions.daily(session, now)
        total = self._positions.total_exposure_quote_raw(session)
        start = sol_to_lamports(self._config.starting_balance_sol)
        return RiskSnapshot(
            total_exposure_lamports=total,
            token_exposure_lamports=0,
            open_positions=self._positions.open_position_count(session),
            daily_deployed_lamports=deployed,
            daily_realized_pnl_lamports=realized,
            consecutive_failures=failures,
            free_balance_lamports=start + realized - total,
        )

    # -- gates -------------------------------------------------------------------------
    def check_common(self, session: Session, now: datetime) -> RiskDecision:
        """Gates that apply to every intent, buy or sell."""
        if self._kill_switch.is_active():
            return RiskDecision.deny(NoTradeReason.KILL_SWITCH_ACTIVE, self._kill_switch.reason())
        _deployed, realized, _trades, failures = self._positions.daily(session, now)
        max_loss = sol_to_lamports(self._config.max_daily_loss_sol)
        if realized <= -max_loss:
            return RiskDecision.deny(
                NoTradeReason.DAILY_LIMIT,
                f"daily realized loss {-realized} lamports reached the limit {max_loss}",
            )
        if failures >= self._config.max_consecutive_failures:
            return RiskDecision.deny(
                NoTradeReason.STATE_MISMATCH,
                f"{failures} consecutive execution failures reached the circuit breaker "
                f"threshold {self._config.max_consecutive_failures}",
            )
        return RiskDecision.ok()

    def check_buy(
        self,
        session: Session,
        *,
        base_mint: str,
        size_lamports: int,
        now: datetime,
    ) -> RiskDecision:
        common = self.check_common(session, now)
        if not common.allowed:
            return common

        if size_lamports <= 0:
            return RiskDecision.deny(NoTradeReason.SIZE_BELOW_MINIMUM, "size is not positive")

        max_trade = sol_to_lamports(self._config.max_trade_size_sol)
        if size_lamports > max_trade:
            return RiskDecision.deny(
                NoTradeReason.POSITION_LIMIT,
                f"size {size_lamports} exceeds max_trade_size {max_trade} lamports",
            )

        exposure = self._positions.get_exposure(session, base_mint)
        max_per_token = sol_to_lamports(self._config.max_position_per_token_sol)
        if exposure.quote_cost_raw + size_lamports > max_per_token:
            return RiskDecision.deny(
                NoTradeReason.POSITION_LIMIT,
                f"token exposure {exposure.quote_cost_raw} + {size_lamports} would exceed "
                f"max_position_per_token {max_per_token} lamports",
            )

        total = self._positions.total_exposure_quote_raw(session)
        max_total = sol_to_lamports(self._config.max_total_exposure_sol)
        if total + size_lamports > max_total:
            return RiskDecision.deny(
                NoTradeReason.POSITION_LIMIT,
                f"total exposure {total} + {size_lamports} would exceed "
                f"max_total_exposure {max_total} lamports",
            )

        open_positions = self._positions.open_position_count(session)
        if exposure.base_amount_raw == 0 and open_positions >= self._config.max_open_positions:
            return RiskDecision.deny(
                NoTradeReason.POSITION_LIMIT,
                f"{open_positions} open positions already at the limit "
                f"{self._config.max_open_positions}",
            )

        deployed, realized, _trades, _failures = self._positions.daily(session, now)
        max_daily = sol_to_lamports(self._config.max_daily_exposure_sol)
        if deployed + size_lamports > max_daily:
            return RiskDecision.deny(
                NoTradeReason.DAILY_LIMIT,
                f"daily deployed {deployed} + {size_lamports} would exceed {max_daily} lamports",
            )

        # Keep enough SOL to pay for the exits. A portfolio that cannot afford its own
        # transaction fees cannot close its positions.
        reserve = sol_to_lamports(self._config.fee_reserve_sol)
        free = sol_to_lamports(self._config.starting_balance_sol) + realized - total
        if free - size_lamports < reserve:
            return RiskDecision.deny(
                NoTradeReason.INSUFFICIENT_BALANCE,
                f"free balance {free} lamports minus size {size_lamports} would fall below the "
                f"fee reserve {reserve}",
            )
        return RiskDecision.ok()

    def check_sell(
        self,
        session: Session,
        *,
        source_wallet: str,
        base_mint: str,
        base_amount_raw: int,
        now: datetime,
    ) -> RiskDecision:
        """A sell reduces risk, so only the blocking gates apply.

        The kill switch still blocks it: when something is badly wrong, a forced automated
        exit into an unknown state is not obviously safer than stopping and looking.
        """
        if self._kill_switch.is_active():
            return RiskDecision.deny(NoTradeReason.KILL_SWITCH_ACTIVE, self._kill_switch.reason())
        position = self._positions.get_position(session, source_wallet, base_mint)
        if position.base_amount_raw <= 0:
            return RiskDecision.deny(
                NoTradeReason.NO_COPY_POSITION_TO_SELL,
                f"no copy position of {base_mint} attributed to {source_wallet}",
            )
        if base_amount_raw <= 0:
            return RiskDecision.deny(NoTradeReason.SIZE_BELOW_MINIMUM, "sell size is not positive")
        if base_amount_raw > position.base_amount_raw:
            return RiskDecision.deny(
                NoTradeReason.STATE_MISMATCH,
                f"sell of {base_amount_raw} exceeds the held {position.base_amount_raw}",
            )
        return RiskDecision.ok()

    # -- circuit breakers ---------------------------------------------------------------
    def evaluate_circuit_breakers(
        self,
        session: Session,
        *,
        now: datetime,
        feed_age_seconds: float | None,
        decoder_coverage: Decimal | None,
        observed_slippage_bps: int | None,
    ) -> list[str]:
        """Return the breakers that tripped. Engaging the kill switch is the caller's call.

        Returning rather than acting keeps the decision auditable: the pipeline logs what
        tripped, and only the operational layer decides whether that stops everything.
        """
        tripped: list[str] = []
        _deployed, realized, _trades, failures = self._positions.daily(session, now)

        if failures >= self._config.max_consecutive_failures:
            tripped.append(f"consecutive_failures={failures}")
        max_loss = sol_to_lamports(self._config.max_daily_loss_sol)
        if realized <= -max_loss:
            tripped.append(f"daily_loss={-realized}")
        if feed_age_seconds is not None and feed_age_seconds > self._config.stale_feed_seconds:
            tripped.append(f"stale_feed={feed_age_seconds:.1f}s")
        if decoder_coverage is not None and decoder_coverage < self._config.min_decoder_coverage:
            tripped.append(f"decoder_coverage={decoder_coverage}")
        if (
            observed_slippage_bps is not None
            and observed_slippage_bps > self._config.max_slippage_observed_bps
        ):
            tripped.append(f"slippage_bps={observed_slippage_bps}")
        return tripped


__all__ = ["KillSwitch", "RiskDecision", "RiskEngine", "RiskSnapshot"]
