"""The pipeline that ties Plan B together.

    raw event -> persist -> decode -> persist SwapEvent -> policy -> persist CopyIntent
              -> broker -> persist ExecutionResult -> position update -> latency record

Everything for one raw transaction happens inside **one database transaction**. That is
what makes the restart guarantees real rather than aspirational: a process killed at any
point either did all of it or none of it, so there is no state in which an intent exists
without its decision, or a position moved without the result that justifies it.

Idempotency comes from identity, not from bookkeeping: ``event_id`` and ``intent_id`` are
deterministic (S0 4.1.1), so replaying the same transaction produces the same primary keys
and the second write is refused by the database rather than by a check somebody might
forget to write.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from gundix_contracts.enums import (
    Decision,
    DecodeConfidence,
    EventSource,
    OperatingMode,
    Venue,
)
from gundix_contracts.mints import WSOL_MINT
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    CopyIntent,
    ExecutionResult,
    LatenciesMs,
    LatencyObservation,
    SwapEvent,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.common.clock import Clock, millis_between
from src.common.config import RuntimeConfig
from src.common.db import unit_of_work
from src.common.logging import get_logger, log_event
from src.paper.broker import Broker, ExecutionContext
from src.paper.policy import SignalContext, SignalPolicy, log_decision
from src.paper.positions import PositionRepository
from src.paper.selection import SelectionRepository
from src.paper.store import (
    RawChainEvent as RawChainEventRow,
)
from src.paper.store import (
    ServiceCheckpoint,
    StoredCopyIntent,
    StoredSwapEvent,
)
from src.stream.decoder import SwapDecoder, UnknownProgramPolicy
from src.stream.sources import ChainEventSource, RawChainEvent

logger = get_logger(__name__)

PIPELINE_VERSION = "1.0.0"


@dataclass(slots=True)
class CoverageCounters:
    """Feeds ``decoder_coverage_<ts>.json``. Counts, never estimates."""

    transactions_seen: int = 0
    transactions_failed_onchain: int = 0
    transactions_decoded_complete: int = 0
    transactions_partial: int = 0
    transactions_unknown: int = 0
    transactions_no_swap: int = 0
    events_emitted: int = 0
    quarantined: int = 0
    wash_same_tx: int = 0
    unknown_programs: dict[str, int] = field(default_factory=dict)
    unknown_program_wallets: dict[str, set[str]] = field(default_factory=dict)
    venues: dict[str, int] = field(default_factory=dict)
    quarantine_reasons: dict[str, int] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)

    @property
    def coverage_ratio(self) -> Decimal:
        denominator = (
            self.transactions_seen - self.transactions_failed_onchain - self.transactions_no_swap
        )
        if denominator <= 0:
            return Decimal(0)
        return (Decimal(self.transactions_decoded_complete) / Decimal(denominator)).quantize(
            Decimal("0.0001")
        )


@dataclass(slots=True)
class ProcessOutcome:
    """What one raw transaction produced. Returned for tests and for the runner's log."""

    signature: str
    events: list[SwapEvent] = field(default_factory=list)
    intents: list[CopyIntent] = field(default_factory=list)
    results: list[ExecutionResult] = field(default_factory=list)
    latencies: list[LatencyObservation] = field(default_factory=list)
    skipped_duplicate: bool = False
    quarantined: bool = False


class Pipeline:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        mode: OperatingMode,
        clock: Clock,
        session_factory: sessionmaker[Session],
        source: ChainEventSource,
        selections: SelectionRepository,
        policy: SignalPolicy,
        broker: Broker | None,
        positions: PositionRepository,
    ) -> None:
        self._config = config
        self._mode = mode
        self._clock = clock
        self._sessions = session_factory
        self._source = source
        self._selections = selections
        self._policy = policy
        self._broker = broker
        self._positions = positions
        self._decoder = SwapDecoder(
            source=source.source_kind,
            source_provider=config.decoder.source_provider,
            unknown_program_policy=UnknownProgramPolicy(config.decoder.unknown_program_policy),
        )
        self.coverage = CoverageCounters()
        self._latency_buffer: list[LatencyObservation] = []
        self._results_buffer: list[ExecutionResult] = []

        if config.decoder.unknown_program_policy != "QUARANTINE_TRANSACTION":
            log_event(
                logger,
                30,
                "decoder is running a policy that deviates from S0 rule 9",
                policy=config.decoder.unknown_program_policy,
                ccr="CCR-001",
            )

    # -- buffers the exporter drains ----------------------------------------------------
    @property
    def latency_observations(self) -> list[LatencyObservation]:
        return self._latency_buffer

    @property
    def execution_results(self) -> list[ExecutionResult]:
        return self._results_buffer

    # -- main loop ----------------------------------------------------------------------
    def watched_wallets(self) -> tuple[str, ...]:
        active = self._selections.active
        if active is None:
            return ()
        return tuple(active.by_wallet)

    def run_once(self) -> list[ProcessOutcome]:
        """Drain whatever the source has and process it. One transaction at a time."""
        self._source.set_watchlist(self.watched_wallets())
        outcomes: list[ProcessOutcome] = []
        for raw in self._source.poll():
            outcomes.append(self.process(raw))
        return outcomes

    def process(self, raw: RawChainEvent) -> ProcessOutcome:
        outcome = ProcessOutcome(signature=raw.signature)
        wallets = self.watched_wallets()

        with unit_of_work(self._sessions) as session:
            if not self._persist_raw(session, raw):
                outcome.skipped_duplicate = True
                return outcome

            self.coverage.transactions_seen += 1
            # Derived from the payload, not from the source's flag: whether a transaction
            # failed is a fact about the transaction, and a counter that depends on a
            # caller remembering to set a boolean will eventually be wrong.
            failed = raw.failed_on_chain or transaction_failed(raw.payload)
            if failed:
                self.coverage.transactions_failed_onchain += 1
            if raw.payload is None:
                self.coverage.transactions_no_swap += 1
                return outcome

            decoded_at = self._clock.now()
            result = self._decoder.decode(
                raw.payload,
                wallets=wallets,
                observed_at_utc=raw.received_at_utc,
                finality=self._config.stream.commitment,
                signature=raw.signature,
            )
            self._count_decode(result)
            if result.quarantined_wallets:
                outcome.quarantined = True
            if not result.events:
                if not result.touched_swap and not raw.failed_on_chain:
                    self.coverage.transactions_no_swap += 1
                return outcome

            for event in result.events:
                if not self._persist_event(session, event, raw):
                    continue
                outcome.events.append(event)
                self.coverage.events_emitted += 1
                self.coverage.venues[event.venue.value] = (
                    self.coverage.venues.get(event.venue.value, 0) + 1
                )
                self._handle_event(
                    session,
                    event=event,
                    raw=raw,
                    decoded_at=decoded_at,
                    outcome=outcome,
                )
        return outcome

    # -- per event ----------------------------------------------------------------------
    def _handle_event(
        self,
        session: Session,
        *,
        event: SwapEvent,
        raw: RawChainEvent,
        decoded_at: datetime,
        outcome: ProcessOutcome,
    ) -> None:
        pool_liquidity = pool_quote_liquidity(raw.payload, event.pool, event.quote_mint)
        context = SignalContext(
            source_pre_base_balance_raw=pre_balance_of(raw.payload, event.wallet, event.base_mint),
            pool_quote_liquidity_raw=pool_liquidity,
            received_at_utc=raw.received_at_utc,
            decoded_at_utc=decoded_at,
        )
        decision = self._policy.evaluate(
            session,
            event,
            selection=self._selections.active,
            mode=self._mode,
            context=context,
        )
        intent = decision.intent
        decided_at = self._clock.now()
        log_decision(intent, decision.detail)

        if not self._persist_intent(session, intent):
            # Another pass already decided this event. Idempotency, enforced by the index.
            return
        outcome.intents.append(intent)
        self.coverage.decisions[intent.reason_code.value if intent.reason_code else "EXECUTE"] = (
            self.coverage.decisions.get(
                intent.reason_code.value if intent.reason_code else "EXECUTE", 0
            )
            + 1
        )

        execution: ExecutionResult | None = None
        if intent.decision is Decision.EXECUTE and self._broker is not None:
            execution = self._broker.execute(
                session,
                intent,
                ExecutionContext(
                    base_decimals=event.base_decimals,
                    quote_decimals=event.quote_decimals,
                    reference_price=event.price_in_quote(),
                    pool_quote_liquidity_raw=pool_liquidity,
                    block_time_utc=event.block_time_utc,
                    received_at_utc=raw.received_at_utc,
                    decoded_at_utc=decoded_at,
                    decided_at_utc=decided_at,
                ),
            )
            outcome.results.append(execution)
            self._results_buffer.append(execution)

        observation = self._latency_observation(
            event=event,
            raw=raw,
            decoded_at=decoded_at,
            decided_at=decided_at,
            intent=intent,
            execution=execution,
        )
        outcome.latencies.append(observation)
        self._latency_buffer.append(observation)

    def _latency_observation(
        self,
        *,
        event: SwapEvent,
        raw: RawChainEvent,
        decoded_at: datetime,
        decided_at: datetime,
        intent: CopyIntent,
        execution: ExecutionResult | None,
    ) -> LatencyObservation:
        quoted_at = execution.quote.requested_at_utc if execution and execution.quote else None
        return LatencyObservation(
            schema_version=SCHEMA_VERSIONS["latency_observation"],
            event_id=event.event_id,
            signature=event.signature,
            wallet=event.wallet,
            slot=event.slot,
            block_time_utc=event.block_time_utc,
            received_at_utc=raw.received_at_utc,
            decoded_at_utc=decoded_at,
            decided_at_utc=decided_at,
            quoted_at_utc=quoted_at,
            submitted_at_utc=None,
            latencies_ms=LatenciesMs(
                block_to_receive=millis_between(event.block_time_utc, raw.received_at_utc),
                receive_to_decode=millis_between(raw.received_at_utc, decoded_at),
                decode_to_decision=millis_between(decoded_at, decided_at),
                decision_to_quote=millis_between(decided_at, quoted_at) if quoted_at else None,
                quote_to_submit=None,
                submit_to_confirm=None,
            ),
            source=raw.source,
            source_provider=self._config.decoder.source_provider,
            mode=self._mode,
            decision=intent.decision,
            reason_code=intent.reason_code.value if intent.reason_code else None,
        )

    # -- persistence --------------------------------------------------------------------
    def _persist_raw(self, session: Session, raw: RawChainEvent) -> bool:
        """Store the raw event first. Returns False when it was already processed."""
        existing = (
            session.query(RawChainEventRow)
            .filter(RawChainEventRow.signature == raw.signature)
            .one_or_none()
        )
        if existing is not None:
            return False
        session.add(
            RawChainEventRow(
                signature=raw.signature,
                slot=raw.slot,
                source_kind=raw.source.value,
                wallet_hint=raw.wallet_hint,
                received_at_utc=raw.received_at_utc,
                processed_at_utc=self._clock.now(),
                decode_failed=False,
                payload=raw.payload,
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return False
        return True

    def _persist_event(self, session: Session, event: SwapEvent, raw: RawChainEvent) -> bool:
        if session.get(StoredSwapEvent, event.event_id) is not None:
            return False
        session.add(
            StoredSwapEvent(
                event_id=event.event_id,
                signature=event.signature,
                slot=event.slot,
                block_time_utc=event.block_time_utc,
                wallet=event.wallet,
                base_mint=event.base_mint,
                quote_mint=event.quote_mint,
                side=event.side.value,
                base_amount_raw=str(event.base_amount_raw),
                quote_amount_raw=str(event.quote_amount_raw),
                venue=event.venue.value,
                instruction_path=event.instruction_path,
                net_swap_index=event.net_swap_index,
                decode_confidence=event.decode_confidence.value,
                success=event.success,
                received_at_utc=raw.received_at_utc,
                payload=event.to_wire(),
            )
        )
        session.flush()
        return True

    def _persist_intent(self, session: Session, intent: CopyIntent) -> bool:
        if session.get(StoredCopyIntent, intent.intent_id) is not None:
            return False
        session.add(
            StoredCopyIntent(
                intent_id=intent.intent_id,
                source_event_id=intent.source_event_id,
                source_wallet=intent.source_wallet,
                decision=intent.decision.value,
                reason_code=intent.reason_code.value if intent.reason_code else None,
                side=intent.side.value,
                base_mint=intent.base_mint,
                quote_mint=intent.quote_mint,
                mode=intent.mode.value,
                selection_id=intent.selection_id,
                policy_version=intent.policy_version,
                created_at_utc=intent.created_at_utc,
                expires_at_utc=intent.expires_at_utc,
                settled=intent.decision is Decision.NO_TRADE,
                payload=intent.to_wire(),
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            log_event(
                logger,
                30,
                "intent rejected by the uniqueness index; another pass already decided this event",
                event_id=intent.source_event_id[:16],
            )
            return False
        return True

    # -- counters -----------------------------------------------------------------------
    def _count_decode(self, result: Any) -> None:
        for program_id in result.unknown_program_ids:
            self.coverage.unknown_programs[program_id] = (
                self.coverage.unknown_programs.get(program_id, 0) + 1
            )
            self.coverage.unknown_program_wallets.setdefault(program_id, set()).update(
                result.quarantined_wallets or result.involved_wallets
            )
        for reason in result.quarantine_reasons:
            key = reason.split(":", 1)[0]
            self.coverage.quarantine_reasons[key] = self.coverage.quarantine_reasons.get(key, 0) + 1
        self.coverage.quarantined += len(result.quarantined_wallets)
        self.coverage.wash_same_tx += result.wash_same_tx

        if not result.success:
            return
        if result.quarantined_wallets:
            self.coverage.transactions_unknown += 1
            return
        if result.events:
            if all(e.decode_confidence is DecodeConfidence.COMPLETE for e in result.events):
                self.coverage.transactions_decoded_complete += 1
            elif any(e.venue is Venue.UNKNOWN for e in result.events):
                self.coverage.transactions_unknown += 1
            else:
                self.coverage.transactions_partial += 1

    # -- checkpoints --------------------------------------------------------------------
    def save_checkpoint(self, key: str, value: dict[str, Any]) -> None:
        with unit_of_work(self._sessions) as session:
            row = session.get(ServiceCheckpoint, key)
            now = self._clock.now()
            if row is None:
                session.add(ServiceCheckpoint(key=key, value=value, updated_at_utc=now))
            else:
                row.value = value
                row.updated_at_utc = now

    def load_checkpoint(self, key: str) -> dict[str, Any] | None:
        with unit_of_work(self._sessions) as session:
            row = session.get(ServiceCheckpoint, key)
            return dict(row.value) if row is not None else None


def transaction_failed(transaction: dict[str, Any] | None) -> bool:
    if not transaction:
        return False
    return (transaction.get("meta") or {}).get("err") is not None


def pool_quote_liquidity(
    transaction: dict[str, Any] | None, pool: str | None, quote_mint: str
) -> int | None:
    """How much of the quote asset the pool held after the trade.

    The transaction already carries it, so this costs no extra RPC call. For a SOL-quoted
    pool it is the pool account's lamport balance; for a token-quoted pool it is the pool's
    token balance for that mint. Returns None when the pool cannot be identified, and the
    liquidity gate then has nothing to judge rather than a guess to act on.
    """
    if not transaction or not pool:
        return None
    meta = transaction.get("meta") or {}

    if quote_mint == WSOL_MINT:
        message = transaction.get("transaction", {}).get("message", {})
        keys: list[str] = []
        for entry in message.get("accountKeys") or []:
            keys.append(entry if isinstance(entry, str) else str(entry.get("pubkey")))
        post = meta.get("postBalances")
        if isinstance(post, list) and pool in keys:
            index = keys.index(pool)
            if index < len(post):
                return int(post[index])
        # A pool that holds wrapped SOL in a token account rather than as lamports.
        for entry in meta.get("postTokenBalances") or []:
            if entry.get("owner") == pool and entry.get("mint") == WSOL_MINT:
                amount = (entry.get("uiTokenAmount") or {}).get("amount")
                if amount is not None:
                    return int(amount)
        return None

    for entry in meta.get("postTokenBalances") or []:
        if entry.get("owner") == pool and entry.get("mint") == quote_mint:
            amount = (entry.get("uiTokenAmount") or {}).get("amount")
            if amount is not None:
                return int(amount)
    return None


def pre_balance_of(transaction: dict[str, Any] | None, wallet: str, mint: str) -> int | None:
    """What ``wallet`` held of ``mint`` before this transaction.

    This is what turns "sold 1.2M tokens" into "sold 30 % of the position", which is the
    only form a copier can apply to a differently sized holding. Not part of the SwapEvent
    contract, so it is read here from the raw payload the decision was made on.
    """
    if not transaction:
        return None
    for entry in (transaction.get("meta") or {}).get("preTokenBalances") or []:
        if entry.get("owner") == wallet and entry.get("mint") == mint:
            amount = (entry.get("uiTokenAmount") or {}).get("amount")
            if amount is not None:
                return int(amount)
    return None


def watched_from(events: Iterable[SwapEvent]) -> Sequence[str]:
    return tuple({event.wallet for event in events})


__all__ = [
    "PIPELINE_VERSION",
    "CoverageCounters",
    "EventSource",
    "Pipeline",
    "ProcessOutcome",
    "pool_quote_liquidity",
    "pre_balance_of",
    "transaction_failed",
]
