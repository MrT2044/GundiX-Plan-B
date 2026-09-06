"""Copy positions and exposure (02_PLAN_B B5).

Two ideas carry this module:

**A position is attributed to the source wallet that caused it.** If two selected wallets
both bought the same token, GundiX holds two separate copy positions. That is what makes a
proportional sell meaningful: when wallet X sells 30 % of its own holding, GundiX sells
30 % of *the position X caused*, not 30 % of everything it holds in that token.

**Exposure is aggregated per token.** Limits are about risk in a token, and risk does not
care which wallet caused it.

Every mutation here happens inside the caller's transaction, together with the execution
result that justifies it. A position is only ever changed by an economically effective
fill - never by the fact that an attempt was made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from gundix_contracts.enums import Side
from gundix_contracts.mints import LAMPORTS_PER_SOL
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.paper.store import DailyRiskLedger, SourceWalletPosition, TokenExposure


class PositionError(RuntimeError):
    """A position update would violate an invariant. The caller must not proceed."""


@dataclass(frozen=True, slots=True)
class PositionView:
    source_wallet: str
    base_mint: str
    base_amount_raw: int
    quote_cost_raw: int
    quote_proceeds_raw: int

    @property
    def is_open(self) -> bool:
        return self.base_amount_raw > 0

    @property
    def realized_pnl_quote_raw(self) -> int:
        """Only meaningful once the position is closed; before that it is partial."""
        return self.quote_proceeds_raw - self.quote_cost_raw


@dataclass(frozen=True, slots=True)
class ExposureView:
    base_mint: str
    base_amount_raw: int
    quote_cost_raw: int
    open_positions: int


def _to_int(value: str | None) -> int:
    return int(value) if value else 0


class PositionRepository:
    """Reads and writes copy positions, token exposure and the daily risk ledger."""

    def get_position(self, session: Session, wallet: str, base_mint: str) -> PositionView:
        row = session.get(SourceWalletPosition, (wallet, base_mint))
        if row is None:
            return PositionView(wallet, base_mint, 0, 0, 0)
        return PositionView(
            source_wallet=wallet,
            base_mint=base_mint,
            base_amount_raw=_to_int(row.base_amount_raw),
            quote_cost_raw=_to_int(row.quote_cost_raw),
            quote_proceeds_raw=_to_int(row.quote_proceeds_raw),
        )

    def get_exposure(self, session: Session, base_mint: str) -> ExposureView:
        row = session.get(TokenExposure, base_mint)
        if row is None:
            return ExposureView(base_mint, 0, 0, 0)
        return ExposureView(
            base_mint=base_mint,
            base_amount_raw=_to_int(row.base_amount_raw),
            quote_cost_raw=_to_int(row.quote_cost_raw),
            open_positions=row.open_positions,
        )

    def total_exposure_quote_raw(self, session: Session) -> int:
        rows = session.execute(select(TokenExposure.quote_cost_raw)).scalars().all()
        return sum(_to_int(value) for value in rows)

    def open_position_count(self, session: Session) -> int:
        rows = (
            session.execute(
                select(SourceWalletPosition.base_amount_raw).where(
                    SourceWalletPosition.base_amount_raw != "0"
                )
            )
            .scalars()
            .all()
        )
        return sum(1 for value in rows if _to_int(value) > 0)

    def open_positions(self, session: Session) -> list[PositionView]:
        rows = session.execute(select(SourceWalletPosition)).scalars().all()
        views = [
            PositionView(
                source_wallet=row.source_wallet,
                base_mint=row.base_mint,
                base_amount_raw=_to_int(row.base_amount_raw),
                quote_cost_raw=_to_int(row.quote_cost_raw),
                quote_proceeds_raw=_to_int(row.quote_proceeds_raw),
            )
            for row in rows
        ]
        return [view for view in views if view.is_open]

    # -- mutation ----------------------------------------------------------------------
    def apply_buy(
        self,
        session: Session,
        *,
        source_wallet: str,
        base_mint: str,
        base_filled_raw: int,
        quote_spent_raw: int,
        now: datetime,
    ) -> PositionView:
        if base_filled_raw <= 0 or quote_spent_raw <= 0:
            raise PositionError("a buy must add a positive base amount for a positive cost")

        row = session.get(SourceWalletPosition, (source_wallet, base_mint))
        was_open = row is not None and _to_int(row.base_amount_raw) > 0
        if row is None:
            row = SourceWalletPosition(
                source_wallet=source_wallet,
                base_mint=base_mint,
                base_amount_raw="0",
                quote_cost_raw="0",
                quote_proceeds_raw="0",
                opened_at_utc=now,
                updated_at_utc=now,
            )
            session.add(row)
        row.base_amount_raw = str(_to_int(row.base_amount_raw) + base_filled_raw)
        row.quote_cost_raw = str(_to_int(row.quote_cost_raw) + quote_spent_raw)
        row.updated_at_utc = now

        self._bump_exposure(
            session,
            base_mint=base_mint,
            base_delta=base_filled_raw,
            quote_delta=quote_spent_raw,
            position_delta=0 if was_open else 1,
            now=now,
        )
        self._bump_daily(session, now=now, quote_deployed=quote_spent_raw, realized_pnl=0, trades=1)
        return self.get_position(session, source_wallet, base_mint)

    def apply_sell(
        self,
        session: Session,
        *,
        source_wallet: str,
        base_mint: str,
        base_sold_raw: int,
        quote_received_raw: int,
        now: datetime,
    ) -> PositionView:
        """Reduce a position. Sold amount can never exceed what is actually held."""
        row = session.get(SourceWalletPosition, (source_wallet, base_mint))
        held = _to_int(row.base_amount_raw) if row is not None else 0
        if row is None or held <= 0:
            raise PositionError(
                f"no copy position of {base_mint} attributed to {source_wallet} to sell"
            )
        if base_sold_raw <= 0:
            raise PositionError("a sell must remove a positive base amount")
        if base_sold_raw > held:
            raise PositionError(
                f"sell of {base_sold_raw} exceeds the held copy amount {held}; "
                "the invariant 'sold <= held' must hold at all times"
            )

        # Cost basis is released proportionally to the fraction sold, so a partial exit
        # leaves a proportional cost behind rather than a distorted average.
        cost = _to_int(row.quote_cost_raw)
        released_cost = cost * base_sold_raw // held if held else 0
        remaining = held - base_sold_raw

        row.base_amount_raw = str(remaining)
        row.quote_cost_raw = str(cost - released_cost)
        row.quote_proceeds_raw = str(_to_int(row.quote_proceeds_raw) + quote_received_raw)
        row.updated_at_utc = now

        self._bump_exposure(
            session,
            base_mint=base_mint,
            base_delta=-base_sold_raw,
            quote_delta=-released_cost,
            position_delta=-1 if remaining == 0 else 0,
            now=now,
        )
        self._bump_daily(
            session,
            now=now,
            quote_deployed=0,
            realized_pnl=quote_received_raw - released_cost,
            trades=1,
        )
        return self.get_position(session, source_wallet, base_mint)

    def apply_fill(
        self,
        session: Session,
        *,
        source_wallet: str,
        base_mint: str,
        side: Side,
        filled_base_raw: int,
        filled_quote_raw: int,
        now: datetime,
    ) -> PositionView:
        if side is Side.BUY:
            return self.apply_buy(
                session,
                source_wallet=source_wallet,
                base_mint=base_mint,
                base_filled_raw=filled_base_raw,
                quote_spent_raw=filled_quote_raw,
                now=now,
            )
        return self.apply_sell(
            session,
            source_wallet=source_wallet,
            base_mint=base_mint,
            base_sold_raw=filled_base_raw,
            quote_received_raw=filled_quote_raw,
            now=now,
        )

    # -- internals ---------------------------------------------------------------------
    def _bump_exposure(
        self,
        session: Session,
        *,
        base_mint: str,
        base_delta: int,
        quote_delta: int,
        position_delta: int,
        now: datetime,
    ) -> None:
        row = session.get(TokenExposure, base_mint)
        if row is None:
            row = TokenExposure(
                base_mint=base_mint,
                base_amount_raw="0",
                quote_cost_raw="0",
                open_positions=0,
                updated_at_utc=now,
            )
            session.add(row)
        row.base_amount_raw = str(max(0, _to_int(row.base_amount_raw) + base_delta))
        row.quote_cost_raw = str(max(0, _to_int(row.quote_cost_raw) + quote_delta))
        row.open_positions = max(0, row.open_positions + position_delta)
        row.updated_at_utc = now

    def _bump_daily(
        self,
        session: Session,
        *,
        now: datetime,
        quote_deployed: int,
        realized_pnl: int,
        trades: int,
    ) -> None:
        day = now.date().isoformat()
        row = session.get(DailyRiskLedger, day)
        if row is None:
            row = DailyRiskLedger(
                day=day,
                quote_deployed_raw="0",
                realized_pnl_quote_raw="0",
                trades=0,
                consecutive_failures=0,
                updated_at_utc=now,
            )
            session.add(row)
        row.quote_deployed_raw = str(_to_int(row.quote_deployed_raw) + quote_deployed)
        row.realized_pnl_quote_raw = str(_to_int(row.realized_pnl_quote_raw) + realized_pnl)
        row.trades += trades
        row.updated_at_utc = now

    # -- daily ledger ------------------------------------------------------------------
    def daily(self, session: Session, now: datetime) -> tuple[int, int, int, int]:
        """``(quote_deployed, realized_pnl, trades, consecutive_failures)`` for today."""
        row = session.get(DailyRiskLedger, now.date().isoformat())
        if row is None:
            return 0, 0, 0, 0
        return (
            _to_int(row.quote_deployed_raw),
            _to_int(row.realized_pnl_quote_raw),
            row.trades,
            row.consecutive_failures,
        )

    def record_execution_outcome(self, session: Session, *, now: datetime, succeeded: bool) -> int:
        """Track consecutive failures across restarts. Returns the new streak length."""
        day = now.date().isoformat()
        row = session.get(DailyRiskLedger, day)
        if row is None:
            row = DailyRiskLedger(
                day=day,
                quote_deployed_raw="0",
                realized_pnl_quote_raw="0",
                trades=0,
                consecutive_failures=0,
                updated_at_utc=now,
            )
            session.add(row)
        row.consecutive_failures = 0 if succeeded else row.consecutive_failures + 1
        row.updated_at_utc = now
        return row.consecutive_failures


def sol_to_lamports(amount_sol: Decimal) -> int:
    """Exact conversion. Fractional lamports are truncated, never rounded up."""
    return int((amount_sol * LAMPORTS_PER_SOL).to_integral_value(rounding="ROUND_DOWN"))


def lamports_to_sol(lamports: int) -> Decimal:
    return Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)


__all__ = [
    "ExposureView",
    "PositionError",
    "PositionRepository",
    "PositionView",
    "lamports_to_sol",
    "sol_to_lamports",
]
