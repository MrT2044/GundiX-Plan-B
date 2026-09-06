"""Brokers (02_PLAN_B B6).

The paper broker exists to run **the entire production decision** - quote, staleness,
slippage floor, fees, landing - without a signature and without a broadcast. Everything
except the last step is the same code path that a live broker would use, which is the only
way a paper result says anything about the live one.

Three properties are non-negotiable here:

* **A position changes only after an economically effective fill.** Never because an
  attempt was made.
* **Randomness is seeded deterministically** from the configured seed and the intent id, so
  the same run produces the same fills (S0 9.1) and a disagreement between two runs is a
  real bug rather than luck.
* **Every assumption that turned a quote into a fill is recorded**, so Plan A can
  recalibrate later instead of trusting a number with no provenance (A10).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from gundix_contracts.enums import ExecutionErrorCode, ExecutionStatus, OperatingMode, Side
from gundix_contracts.ids import compute_result_id
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    CopyIntent,
    ExecutionFees,
    ExecutionResult,
    LatenciesMs,
    QuoteSnapshot,
)
from sqlalchemy.orm import Session

from src.common.clock import Clock, millis_between
from src.common.config import PaperConfig
from src.common.logging import get_logger, log_event
from src.paper.positions import PositionRepository
from src.paper.quotes import QuoteError, QuoteProvider, QuoteRequest, min_out_amount
from src.paper.store import ExecutionAttempt, StoredExecutionResult, StoredQuote

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """What the broker needs that is not in the intent itself."""

    base_decimals: int
    quote_decimals: int
    #: Price of one whole base token in whole quote tokens, from the source trade.
    reference_price: Decimal
    pool_quote_liquidity_raw: int | None = None
    block_time_utc: datetime | None = None
    received_at_utc: datetime | None = None
    decoded_at_utc: datetime | None = None
    decided_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class SimulationRecord:
    """The paper model's assumptions.

    Kept in Plan B's own database rather than written into ``paper_fills.jsonl``, because
    S0 4.6 does not define these fields yet - see CCR-002.
    """

    model_version: str
    assumed_land_probability: str
    landed: bool
    rng_seed: str
    applied_extra_slippage_bps: int
    assumed_priority_fee_lamports: int
    quote_age_ms_at_fill: int


@runtime_checkable
class Broker(Protocol):
    kind: str

    def execute(
        self, session: Session, intent: CopyIntent, context: ExecutionContext
    ) -> ExecutionResult:
        """Attempt the intent once and return exactly one result."""


class PaperBroker:
    """Simulated execution. Never signs, never broadcasts, never imports a signer."""

    kind = "paper"

    def __init__(
        self,
        config: PaperConfig,
        *,
        clock: Clock,
        quotes: QuoteProvider,
        positions: PositionRepository,
        mode: OperatingMode = OperatingMode.PAPER,
    ) -> None:
        if mode.may_broadcast:
            raise ValueError("the paper broker must never run in a mode that can broadcast")
        self._config = config
        self._clock = clock
        self._quotes = quotes
        self._positions = positions
        self._mode = mode

    # -- public ------------------------------------------------------------------------
    def execute(
        self, session: Session, intent: CopyIntent, context: ExecutionContext
    ) -> ExecutionResult:
        started = self._clock.now()
        attempt = self._next_attempt(session, intent.intent_id)
        # Written before the attempt, so a crash mid-flight leaves a visible open attempt
        # rather than nothing at all.
        session.add(
            ExecutionAttempt(
                intent_id=intent.intent_id,
                attempt=attempt,
                broker=self.kind,
                started_at_utc=started,
            )
        )
        session.flush()

        result, simulation = self._attempt(session, intent, context, attempt, started)
        self._persist(session, intent, result, attempt, simulation)
        return result

    # -- internals ---------------------------------------------------------------------
    def _attempt(
        self,
        session: Session,
        intent: CopyIntent,
        context: ExecutionContext,
        attempt: int,
        started: datetime,
    ) -> tuple[ExecutionResult, SimulationRecord | None]:
        now = self._clock.now()
        if now >= intent.expires_at_utc:
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    ExecutionStatus.EXPIRED,
                    ExecutionErrorCode.QUOTE_EXPIRED,
                    f"intent expired at {intent.expires_at_utc.isoformat()}",
                    quote=None,
                ),
                None,
            )

        request = self._quote_request(intent, context)
        try:
            quote = self._quotes.quote(request)
        except QuoteError as exc:
            status = (
                ExecutionStatus.REJECTED
                if exc.code in {"NO_ROUTE", "INSUFFICIENT_LIQUIDITY"}
                else ExecutionStatus.FAILED
            )
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    status,
                    ExecutionErrorCode(exc.code),
                    str(exc),
                    quote=None,
                ),
                None,
            )

        quoted_at = self._clock.now()
        if quoted_at >= quote.expires_at_utc:
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    ExecutionStatus.EXPIRED,
                    ExecutionErrorCode.QUOTE_EXPIRED,
                    "quote expired before it could be used",
                    quote=quote,
                ),
                None,
            )

        floor = min_out_amount(quote, intent.max_slippage_bps)
        seed = f"{self._config.rng_seed}|{intent.intent_id}"
        rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
        landed = rng.random() < float(self._config.land_probability)

        # Real execution arrives a moment after the quote and at a slightly worse price.
        # The extra slippage is an assumption, not a measurement, and it is recorded as one.
        extra_bps = self._config.extra_slippage_bps
        realized_out = int(
            (
                Decimal(quote.out_amount_raw)
                * (Decimal(10_000) - Decimal(extra_bps))
                / Decimal(10_000)
            ).to_integral_value(rounding="ROUND_DOWN")
        )
        simulation = SimulationRecord(
            model_version=self._config.model_version,
            assumed_land_probability=str(self._config.land_probability),
            landed=landed,
            rng_seed=seed,
            applied_extra_slippage_bps=extra_bps,
            assumed_priority_fee_lamports=self._config.priority_fee_lamports,
            quote_age_ms_at_fill=max(0, millis_between(quote.requested_at_utc, quoted_at)),
        )

        if not landed:
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    ExecutionStatus.FAILED,
                    ExecutionErrorCode.SIMULATED_FAIL,
                    f"simulated landing failed at p={self._config.land_probability}",
                    quote=quote,
                ),
                simulation,
            )

        if realized_out < floor:
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    ExecutionStatus.REJECTED,
                    ExecutionErrorCode.SLIPPAGE_EXCEEDED,
                    f"realized output {realized_out} below the committed floor {floor}",
                    quote=quote,
                ),
                simulation,
            )

        if intent.side is Side.BUY:
            filled_quote = quote.in_amount_raw
            filled_base = realized_out
        else:
            filled_base = quote.in_amount_raw
            filled_quote = realized_out

        if filled_base <= 0 or filled_quote <= 0:
            return (
                self._failure(
                    intent,
                    context,
                    attempt,
                    started,
                    ExecutionStatus.REJECTED,
                    ExecutionErrorCode.INSUFFICIENT_LIQUIDITY,
                    "fill rounds to zero on one side",
                    quote=quote,
                ),
                simulation,
            )

        # The position moves only here, after a fill that actually happened.
        self._positions.apply_fill(
            session,
            source_wallet=intent.source_wallet,
            base_mint=intent.base_mint,
            side=intent.side,
            filled_base_raw=filled_base,
            filled_quote_raw=filled_quote,
            now=self._clock.now(),
        )
        self._positions.record_execution_outcome(session, now=self._clock.now(), succeeded=True)

        expected_price = self._price(quote.out_amount_raw, quote.in_amount_raw, intent, context)
        realized_price = self._price(realized_out, quote.in_amount_raw, intent, context)
        finished = self._clock.now()

        result = ExecutionResult(
            schema_version=SCHEMA_VERSIONS["execution_result"],
            result_id=compute_result_id(intent.intent_id, attempt),
            intent_id=intent.intent_id,
            mode=self._mode,
            status=ExecutionStatus.FILLED,
            quote=quote,
            expected_price=expected_price,
            realized_price=realized_price,
            filled_base_raw=filled_base,
            filled_quote_raw=filled_quote,
            fees=ExecutionFees(
                network_fee_lamports=self._config.base_fee_lamports,
                priority_fee_lamports=self._config.priority_fee_lamports,
                tip_lamports=0,
                venue_fee_raw=quote.platform_fee_raw,
            ),
            latencies_ms=self._latencies(context, quoted_at, finished),
            signature=None,
            error_code=None,
            error_detail=None,
            created_at_utc=started,
            finalized_at_utc=finished,
        )
        log_event(
            logger,
            20,
            "paper fill",
            intent_id=intent.intent_id[:16],
            side=intent.side.value,
            base_mint=intent.base_mint,
            filled_base_raw=str(filled_base),
            filled_quote_raw=str(filled_quote),
            price_impact_bps=quote.price_impact_bps,
        )
        return result, simulation

    def _quote_request(self, intent: CopyIntent, context: ExecutionContext) -> QuoteRequest:
        if intent.side is Side.BUY:
            return QuoteRequest(
                in_mint=intent.quote_mint,
                out_mint=intent.base_mint,
                in_amount_raw=intent.target_size_quote_raw,
                in_decimals=context.quote_decimals,
                out_decimals=context.base_decimals,
                slippage_bps=intent.max_slippage_bps,
                # For a buy the reference is "base per quote", the inverse of the observed
                # price, which is quoted as "quote per base".
                reference_price=(
                    Decimal(1) / context.reference_price if context.reference_price > 0 else None
                ),
                pool_quote_liquidity_raw=context.pool_quote_liquidity_raw,
            )
        return QuoteRequest(
            in_mint=intent.base_mint,
            out_mint=intent.quote_mint,
            in_amount_raw=intent.target_base_raw or 0,
            in_decimals=context.base_decimals,
            out_decimals=context.quote_decimals,
            slippage_bps=intent.max_slippage_bps,
            reference_price=context.reference_price,
            pool_quote_liquidity_raw=context.pool_quote_liquidity_raw,
        )

    @staticmethod
    def _price(out_raw: int, in_raw: int, intent: CopyIntent, context: ExecutionContext) -> Decimal:
        """Always quote per base, whatever the direction, so the two are comparable."""
        if out_raw <= 0 or in_raw <= 0:
            return Decimal(0)
        base_scale = Decimal(10) ** context.base_decimals
        quote_scale = Decimal(10) ** context.quote_decimals
        if intent.side is Side.BUY:
            base = Decimal(out_raw) / base_scale
            quote = Decimal(in_raw) / quote_scale
        else:
            base = Decimal(in_raw) / base_scale
            quote = Decimal(out_raw) / quote_scale
        if base == 0:
            return Decimal(0)
        return (quote / base).quantize(Decimal("0.000000000000000001"))

    def _latencies(
        self, context: ExecutionContext, quoted_at: datetime, finished: datetime
    ) -> LatenciesMs:
        def span(a: datetime | None, b: datetime | None) -> int | None:
            if a is None or b is None:
                return None
            return millis_between(a, b)

        return LatenciesMs(
            block_to_receive=span(context.block_time_utc, context.received_at_utc),
            receive_to_decode=span(context.received_at_utc, context.decoded_at_utc),
            decode_to_decision=span(context.decoded_at_utc, context.decided_at_utc),
            decision_to_quote=span(context.decided_at_utc, quoted_at),
            quote_to_submit=span(quoted_at, finished),
            # Paper never submits, so there is nothing to confirm. Reporting a number here
            # would be inventing a measurement.
            submit_to_confirm=None,
        )

    def _failure(
        self,
        intent: CopyIntent,
        context: ExecutionContext,
        attempt: int,
        started: datetime,
        status: ExecutionStatus,
        error_code: ExecutionErrorCode,
        detail: str,
        *,
        quote: QuoteSnapshot | None,
    ) -> ExecutionResult:
        finished = self._clock.now()
        log_event(
            logger,
            20,
            "paper execution did not fill",
            intent_id=intent.intent_id[:16],
            status=status.value,
            error_code=error_code.value,
            detail=detail,
        )
        return ExecutionResult(
            schema_version=SCHEMA_VERSIONS["execution_result"],
            result_id=compute_result_id(intent.intent_id, attempt),
            intent_id=intent.intent_id,
            mode=self._mode,
            status=status,
            quote=quote,
            expected_price=Decimal(0),
            realized_price=Decimal(0),
            filled_base_raw=0,
            filled_quote_raw=0,
            fees=ExecutionFees(),
            latencies_ms=self._latencies(
                context, quote.requested_at_utc if quote else finished, finished
            ),
            signature=None,
            error_code=error_code,
            error_detail=detail,
            created_at_utc=started,
            finalized_at_utc=finished,
        )

    def _next_attempt(self, session: Session, intent_id: str) -> int:
        existing = (
            session.query(ExecutionAttempt).filter(ExecutionAttempt.intent_id == intent_id).count()
        )
        return existing + 1

    def _persist(
        self,
        session: Session,
        intent: CopyIntent,
        result: ExecutionResult,
        attempt: int,
        simulation: SimulationRecord | None,
    ) -> None:
        if result.quote is not None:
            session.add(
                StoredQuote(
                    intent_id=intent.intent_id,
                    provider=result.quote.provider,
                    requested_at_utc=result.quote.requested_at_utc,
                    expires_at_utc=result.quote.expires_at_utc,
                    in_amount_raw=str(result.quote.in_amount_raw),
                    out_amount_raw=str(result.quote.out_amount_raw),
                    price_impact_bps=result.quote.price_impact_bps,
                    payload=result.quote.to_wire(),
                )
            )
        payload = result.to_wire()
        if simulation is not None:
            # Local audit only; deliberately not part of the shared artifact until CCR-002
            # is decided.
            payload = dict(payload)
            payload["_local_simulation"] = asdict(simulation)
        session.add(
            StoredExecutionResult(
                result_id=result.result_id,
                intent_id=result.intent_id,
                attempt=attempt,
                broker=self.kind,
                status=result.status.value,
                economically_effective=result.is_economically_effective,
                signature=None,
                finished_at_utc=result.finalized_at_utc,
                payload=payload,
            )
        )
        row = (
            session.query(ExecutionAttempt)
            .filter(
                ExecutionAttempt.intent_id == intent.intent_id,
                ExecutionAttempt.attempt == attempt,
            )
            .one()
        )
        row.finished_at_utc = result.finalized_at_utc
        row.outcome = result.status.value
        if not result.is_economically_effective and result.status is not ExecutionStatus.EXPIRED:
            self._positions.record_execution_outcome(
                session, now=result.finalized_at_utc, succeeded=False
            )


__all__ = ["Broker", "ExecutionContext", "PaperBroker", "SimulationRecord"]
