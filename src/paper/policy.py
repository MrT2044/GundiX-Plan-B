"""Signal and copy logic (02_PLAN_B B4).

The single rule this module implements: **a SwapEvent becomes a CopyIntent only if every
condition holds. Otherwise a NO_TRADE with a machine readable reason is recorded.**

Recording the rejections is not bookkeeping for its own sake. Plan A derives the real
signal frequency and the rejection distribution from them (A10), and a copy strategy whose
signals are mostly rejected for liquidity reasons is a different strategy from one whose
signals mostly go through - you cannot see that difference without the counts.

The checks run in a deliberate order: cheapest and most specific first, so the reason that
gets recorded is the *first* thing actually wrong rather than whichever gate happened to be
evaluated first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from gundix_contracts.enums import (
    Decision,
    DecodeConfidence,
    DedupPolicy,
    Finality,
    NoTradeReason,
    OperatingMode,
    Side,
    Venue,
)
from gundix_contracts.ids import compute_intent_id
from gundix_contracts.mints import LAMPORTS_PER_SOL
from gundix_contracts.models import SCHEMA_VERSIONS, CopyIntent, SwapEvent
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.common.clock import Clock
from src.common.config import PolicyConfig
from src.common.logging import get_logger, log_event
from src.paper.positions import PositionRepository, sol_to_lamports
from src.paper.risk import RiskEngine
from src.paper.selection import ActiveSelection, selection_blocks_execution
from src.paper.store import StoredCopyIntent

logger = get_logger(__name__)

#: Venues whose swaps Plan B is willing to act on in phase 1. Everything else is decoded
#: and recorded but never traded, until a golden fixture proves it behaves.
SUPPORTED_VENUES: frozenset[Venue] = frozenset(
    {
        Venue.PUMP_FUN,
        Venue.PUMPSWAP,
        Venue.RAYDIUM_AMM_V4,
        Venue.RAYDIUM_CPMM,
        Venue.RAYDIUM_LAUNCHLAB,
    }
)


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Facts about a signal that are real but not part of the SwapEvent contract.

    ``source_pre_base_balance_raw`` is what the source wallet held *before* the observed
    swap, read from ``preTokenBalances``. It is what turns "the trader sold 1.2M tokens"
    into "the trader sold 30 % of their position", which is the only form a copier can
    apply to a differently sized holding.
    """

    source_pre_base_balance_raw: int | None = None
    pool_quote_liquidity_raw: int | None = None
    received_at_utc: datetime | None = None
    decoded_at_utc: datetime | None = None


#: Builds a recorded NO_TRADE decision. Passed down so each branch names its own reason
#: without rebuilding the intent boilerplate.
NoTradeFactory = Callable[..., "PolicyResult"]


@dataclass(frozen=True, slots=True)
class PolicyResult:
    intent: CopyIntent
    detail: str | None = None

    @property
    def is_execute(self) -> bool:
        return self.intent.decision is Decision.EXECUTE


class SignalPolicy:
    """Turns a SwapEvent plus the current state into exactly one recorded decision."""

    def __init__(
        self,
        config: PolicyConfig,
        *,
        clock: Clock,
        positions: PositionRepository,
        risk: RiskEngine,
        required_finality: Finality = Finality.CONFIRMED,
    ) -> None:
        self._config = config
        self._clock = clock
        self._positions = positions
        self._risk = risk
        self._required_finality = required_finality

    # -- entry point -------------------------------------------------------------------
    def evaluate(
        self,
        session: Session,
        event: SwapEvent,
        *,
        selection: ActiveSelection | None,
        mode: OperatingMode,
        context: SignalContext | None = None,
    ) -> PolicyResult:
        ctx = context or SignalContext()
        now = self._clock.now()
        selection_id = selection.selection_id if selection else "none"
        intent_id = compute_intent_id(event.event_id, selection_id, self._config.policy_version)

        def no_trade(reason: NoTradeReason, detail: str | None = None) -> PolicyResult:
            return PolicyResult(
                intent=self._build_intent(
                    event=event,
                    intent_id=intent_id,
                    selection_id=selection_id,
                    mode=mode,
                    now=now,
                    decision=Decision.NO_TRADE,
                    reason=reason,
                    detail=detail,
                ),
                detail=detail,
            )

        blocked = selection_blocks_execution(selection, now=now, mode=mode)
        if blocked is not None:
            return no_trade(NoTradeReason(blocked), f"selection gate: {blocked}")
        assert selection is not None  # narrowed by selection_blocks_execution

        entry = selection.wallet(event.wallet)
        if entry is None or not entry.status.may_trade:
            status = entry.status.value if entry else "absent"
            return no_trade(
                NoTradeReason.WALLET_NOT_SELECTED, f"wallet status in selection: {status}"
            )

        # S0 4.1.7: a failed transaction is not a trade. Filtering on success is mandatory
        # for every economic consumer, so it happens before anything is computed from it.
        if not event.success:
            return no_trade(NoTradeReason.TRANSACTION_FAILED, "source transaction failed on chain")

        if event.decode_confidence is not DecodeConfidence.COMPLETE:
            reason = (
                NoTradeReason.UNKNOWN_VENUE
                if event.venue is Venue.UNKNOWN
                else NoTradeReason.DECODE_INCOMPLETE
            )
            return no_trade(reason, f"decode_confidence={event.decode_confidence.value}")

        if event.venue not in SUPPORTED_VENUES:
            return no_trade(
                NoTradeReason.UNKNOWN_VENUE,
                f"venue {event.venue.value} is decoded but not enabled for trading in phase 1",
            )

        if not event.finality.at_least(self._required_finality):
            return no_trade(
                NoTradeReason.EVENT_NOT_FINAL,
                f"finality {event.finality.value} below required {self._required_finality.value}",
            )

        age = (now - event.block_time_utc).total_seconds()
        if age > self._config.max_signal_age_seconds:
            return no_trade(
                NoTradeReason.EVENT_TOO_OLD,
                f"signal is {age:.1f}s old, limit is {self._config.max_signal_age_seconds}s",
            )

        if self._already_decided(session, event.event_id):
            return no_trade(
                NoTradeReason.DUPLICATE_EVENT, f"event {event.event_id[:16]} already decided"
            )

        if event.quote_mint not in self._config.allowed_quote_mints:
            return no_trade(
                NoTradeReason.UNSUPPORTED_TOKEN,
                f"quote mint {event.quote_mint} is not in the allowed set",
            )
        if event.base_mint in self._config.blocked_mints:
            return no_trade(NoTradeReason.UNSUPPORTED_TOKEN, f"{event.base_mint} is blocked")

        if event.side is Side.BUY:
            return self._evaluate_buy(
                session,
                event=event,
                entry_weight=entry.weight,
                intent_id=intent_id,
                selection=selection,
                mode=mode,
                now=now,
                ctx=ctx,
                no_trade=no_trade,
            )
        return self._evaluate_sell(
            session,
            event=event,
            intent_id=intent_id,
            selection=selection,
            mode=mode,
            now=now,
            ctx=ctx,
            no_trade=no_trade,
        )

    # -- buy ----------------------------------------------------------------------------
    def _evaluate_buy(
        self,
        session: Session,
        *,
        event: SwapEvent,
        entry_weight: Decimal,
        intent_id: str,
        selection: ActiveSelection,
        mode: OperatingMode,
        now: datetime,
        ctx: SignalContext,
        no_trade: NoTradeFactory,
    ) -> PolicyResult:
        suppressed = self._dedup_suppresses_buy(session, event)
        if suppressed is not None:
            return no_trade(NoTradeReason.DEDUP_POLICY_SUPPRESSED, suppressed)

        if ctx.pool_quote_liquidity_raw is not None:
            min_liquidity = sol_to_lamports(self._config.min_pool_liquidity_sol)
            if ctx.pool_quote_liquidity_raw < min_liquidity:
                return no_trade(
                    NoTradeReason.LIQUIDITY_TOO_LOW,
                    f"pool holds {ctx.pool_quote_liquidity_raw} lamports, minimum is "
                    f"{min_liquidity}",
                )

        # Never the trader's absolute size: a wallet with 300 SOL buying 20 SOL of a token
        # says nothing about what GundiX should risk. Own base size, scaled by the weight
        # Plan A assigned to that wallet.
        size_lamports = int(
            (self._config.base_buy_size_sol * entry_weight * LAMPORTS_PER_SOL).to_integral_value(
                rounding="ROUND_DOWN"
            )
        )
        min_size = sol_to_lamports(self._config.min_buy_size_sol)
        if size_lamports < min_size:
            return no_trade(
                NoTradeReason.SIZE_BELOW_MINIMUM,
                f"weighted size {size_lamports} lamports is below the minimum {min_size}",
            )

        decision = self._risk.check_buy(
            session, base_mint=event.base_mint, size_lamports=size_lamports, now=now
        )
        if not decision.allowed:
            assert decision.reason is not None
            return no_trade(decision.reason, decision.detail)

        return PolicyResult(
            intent=self._build_intent(
                event=event,
                intent_id=intent_id,
                selection_id=selection.selection_id,
                mode=mode,
                now=now,
                decision=Decision.EXECUTE,
                reason=None,
                detail=None,
                target_size_quote_raw=size_lamports,
            )
        )

    def _dedup_suppresses_buy(self, session: Session, event: SwapEvent) -> str | None:
        """Several selected wallets buying the same token need an explicit policy (B4)."""
        policy = self._config.dedup_policy
        if policy is DedupPolicy.INCREMENT_WITH_CAP:
            # Every wallet may add, and the per-token cap in the risk engine is what stops
            # accumulation. No suppression here by design.
            return None

        exposure = self._positions.get_exposure(session, event.base_mint)
        own = self._positions.get_position(session, event.wallet, event.base_mint)

        if policy is DedupPolicy.FIRST_SIGNAL_ONLY:
            if exposure.base_amount_raw > 0 and own.base_amount_raw == 0:
                return (
                    f"FIRST_SIGNAL_ONLY: {event.base_mint} is already held from another "
                    "selected wallet"
                )
            if own.base_amount_raw > 0:
                return f"FIRST_SIGNAL_ONLY: already holding {event.base_mint} from this wallet"
            return None

        if policy is DedupPolicy.CONSENSUS_REQUIRED:
            # Consensus needs a second independent signal inside a time window. Counting it
            # requires a signal history this build does not yet keep, so the honest
            # behaviour is to suppress rather than to silently act like FIRST_SIGNAL_ONLY.
            return (
                "CONSENSUS_REQUIRED is not implemented in this build; phase 1 runs "
                "FIRST_SIGNAL_ONLY (02_PLAN_B B4)"
            )
        # Every DedupPolicy member is handled above; adding one without a branch here is a
        # type error rather than a silent fall-through.
        raise AssertionError(f"unhandled dedup policy {policy!r}")

    # -- sell ---------------------------------------------------------------------------
    def _evaluate_sell(
        self,
        session: Session,
        *,
        event: SwapEvent,
        intent_id: str,
        selection: ActiveSelection,
        mode: OperatingMode,
        now: datetime,
        ctx: SignalContext,
        no_trade: NoTradeFactory,
    ) -> PolicyResult:
        position = self._positions.get_position(session, event.wallet, event.base_mint)
        if position.base_amount_raw <= 0:
            # A sell without a matching copy position is logged, never executed. Selling
            # something we do not hold is not a trade, it is a bug.
            return no_trade(
                NoTradeReason.NO_COPY_POSITION_TO_SELL,
                f"no copy position of {event.base_mint} attributed to {event.wallet}",
            )

        fraction_bps, fraction_detail = self._sell_fraction_bps(event, ctx)
        target_base = position.base_amount_raw * fraction_bps // 10_000
        if target_base <= 0:
            return no_trade(
                NoTradeReason.SIZE_BELOW_MINIMUM,
                f"{fraction_bps} bps of {position.base_amount_raw} rounds to zero",
            )
        # The invariant, enforced rather than assumed.
        target_base = min(target_base, position.base_amount_raw)

        decision = self._risk.check_sell(
            session,
            source_wallet=event.wallet,
            base_mint=event.base_mint,
            base_amount_raw=target_base,
            now=now,
        )
        if not decision.allowed:
            assert decision.reason is not None
            return no_trade(decision.reason, decision.detail)

        # Informational: what that base amount is worth at the price the source got.
        expected_quote = int(
            (
                Decimal(target_base)
                * event.price_in_quote()
                * (Decimal(10) ** event.quote_decimals)
                / (Decimal(10) ** event.base_decimals)
            ).to_integral_value(rounding="ROUND_DOWN")
        )
        return PolicyResult(
            intent=self._build_intent(
                event=event,
                intent_id=intent_id,
                selection_id=selection.selection_id,
                mode=mode,
                now=now,
                decision=Decision.EXECUTE,
                reason=None,
                detail=fraction_detail,
                target_size_quote_raw=max(0, expected_quote),
                target_fraction_bps=fraction_bps,
                target_base_raw=target_base,
            ),
            detail=fraction_detail,
        )

    def _sell_fraction_bps(self, event: SwapEvent, ctx: SignalContext) -> tuple[int, str | None]:
        """What fraction of its own holding did the source wallet sell?

        Read from the balance the wallet held before the swap. When that is not observable,
        the conservative reading of "the trader is getting out" is a full exit: staying in a
        position whose thesis just changed is the larger risk. The fallback is always
        recorded in ``reason_detail`` so it never hides in the statistics.
        """
        prior = ctx.source_pre_base_balance_raw
        if prior is None or prior <= 0:
            return 10_000, "sell fraction unobservable, treated as a full exit"
        fraction = min(10_000, max(1, event.base_amount_raw * 10_000 // prior))
        return fraction, None

    # -- helpers -------------------------------------------------------------------------
    def _already_decided(self, session: Session, event_id: str) -> bool:
        found = session.execute(
            select(StoredCopyIntent.intent_id).where(StoredCopyIntent.source_event_id == event_id)
        ).first()
        return found is not None

    def _build_intent(
        self,
        *,
        event: SwapEvent,
        intent_id: str,
        selection_id: str,
        mode: OperatingMode,
        now: datetime,
        decision: Decision,
        reason: NoTradeReason | None,
        detail: str | None,
        target_size_quote_raw: int = 0,
        target_fraction_bps: int | None = None,
        target_base_raw: int | None = None,
    ) -> CopyIntent:
        return CopyIntent(
            schema_version=SCHEMA_VERSIONS["copy_intent"],
            intent_id=intent_id,
            source_event_id=event.event_id,
            source_wallet=event.wallet,
            created_at_utc=now,
            base_mint=event.base_mint,
            quote_mint=event.quote_mint,
            side=event.side,
            target_size_quote_raw=target_size_quote_raw,
            target_fraction_bps=target_fraction_bps,
            target_base_raw=target_base_raw,
            max_slippage_bps=self._config.max_slippage_bps,
            expires_at_utc=now + timedelta(seconds=self._config.intent_ttl_seconds),
            mode=mode,
            decision=decision,
            reason_code=reason,
            reason_detail=detail,
            selection_id=selection_id,
            policy_version=self._config.policy_version,
            dedup_policy=self._config.dedup_policy,
        )


def log_decision(intent: CopyIntent, detail: str | None) -> None:
    log_event(
        logger,
        20 if intent.decision is Decision.EXECUTE else 10,
        "signal decision",
        decision=intent.decision.value,
        reason=intent.reason_code.value if intent.reason_code else None,
        detail=detail,
        wallet=intent.source_wallet,
        base_mint=intent.base_mint,
        side=intent.side.value,
        intent_id=intent.intent_id[:16],
    )


__all__ = ["SUPPORTED_VENUES", "PolicyResult", "SignalContext", "SignalPolicy", "log_decision"]
