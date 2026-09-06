"""Shadow mode (02_PLAN_B B7).

Shadow runs the production decision against **real** quotes and records what would have
happened. It signs nothing and broadcasts nothing, and it cannot: it reuses the paper
broker's fill logic, which has no path to a signer.

What shadow adds over paper is the thing paper cannot give: a measurement of how wrong the
paper model is. It re-quotes after the decision and records the drift, so Plan A can
compare "what the model assumed" against "what the market actually offered a moment later"
(I5).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gundix_contracts.enums import OperatingMode, Side
from gundix_contracts.models import CopyIntent, ExecutionResult
from sqlalchemy.orm import Session

from src.common.clock import Clock, millis_between
from src.common.config import PaperConfig
from src.common.logging import get_logger, log_event
from src.paper.broker import ExecutionContext, PaperBroker
from src.paper.positions import PositionRepository
from src.paper.quotes import QuoteError, QuoteProvider, QuoteRequest

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QuoteDrift:
    """How much the quote moved between the decision and a moment later."""

    intent_id: str
    first_out_amount_raw: int
    second_out_amount_raw: int
    elapsed_ms: int

    @property
    def drift_bps(self) -> int:
        if self.first_out_amount_raw <= 0:
            return 0
        delta = Decimal(self.first_out_amount_raw - self.second_out_amount_raw)
        return int(
            (delta / Decimal(self.first_out_amount_raw) * Decimal(10_000)).to_integral_value()
        )


class ShadowBroker(PaperBroker):
    """Real quotes, simulated fills, no signing and no broadcast."""

    kind = "shadow"

    def __init__(
        self,
        config: PaperConfig,
        *,
        clock: Clock,
        quotes: QuoteProvider,
        positions: PositionRepository,
    ) -> None:
        super().__init__(
            config, clock=clock, quotes=quotes, positions=positions, mode=OperatingMode.SHADOW
        )
        self._quotes_provider = quotes
        self.drifts: list[QuoteDrift] = []

    def execute(
        self, session: Session, intent: CopyIntent, context: ExecutionContext
    ) -> ExecutionResult:
        result = super().execute(session, intent, context)
        if result.quote is not None:
            self._measure_drift(intent, context, result)
        return result

    def _measure_drift(
        self, intent: CopyIntent, context: ExecutionContext, result: ExecutionResult
    ) -> None:
        """Ask again. The difference is the cost of being a moment late."""
        assert result.quote is not None
        if intent.side is Side.BUY:
            request = QuoteRequest(
                in_mint=intent.quote_mint,
                out_mint=intent.base_mint,
                in_amount_raw=intent.target_size_quote_raw,
                in_decimals=context.quote_decimals,
                out_decimals=context.base_decimals,
                slippage_bps=intent.max_slippage_bps,
                reference_price=(
                    Decimal(1) / context.reference_price if context.reference_price > 0 else None
                ),
                pool_quote_liquidity_raw=context.pool_quote_liquidity_raw,
            )
        else:
            request = QuoteRequest(
                in_mint=intent.base_mint,
                out_mint=intent.quote_mint,
                in_amount_raw=intent.target_base_raw or 0,
                in_decimals=context.base_decimals,
                out_decimals=context.quote_decimals,
                slippage_bps=intent.max_slippage_bps,
                reference_price=context.reference_price,
                pool_quote_liquidity_raw=context.pool_quote_liquidity_raw,
            )
        try:
            second = self._quotes_provider.quote(request)
        except QuoteError as exc:
            log_event(
                logger,
                30,
                "shadow re-quote failed",
                intent_id=intent.intent_id[:16],
                error=str(exc),
            )
            return
        drift = QuoteDrift(
            intent_id=intent.intent_id,
            first_out_amount_raw=result.quote.out_amount_raw,
            second_out_amount_raw=second.out_amount_raw,
            elapsed_ms=max(
                0, millis_between(result.quote.requested_at_utc, second.requested_at_utc)
            ),
        )
        self.drifts.append(drift)
        log_event(
            logger,
            10,
            "shadow quote drift",
            intent_id=intent.intent_id[:16],
            drift_bps=drift.drift_bps,
            elapsed_ms=drift.elapsed_ms,
        )


__all__ = ["QuoteDrift", "ShadowBroker"]
