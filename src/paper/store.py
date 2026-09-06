"""Persistent state (02_PLAN_B B5).

Invariants the schema itself enforces, so a bug cannot quietly violate them:

* one event id -> at most one intent  (unique index on ``copy_intents.source_event_id``
  restricted to EXECUTE decisions),
* one intent -> at most one economically effective fill (partial unique index),
* one signature -> at most one raw event row (unique index),
* exposure rows are derived only from effective results.

Raw amounts are stored as TEXT. A u64 token amount does not fit into SQLite's signed
64-bit INTEGER, and silently wrapping a balance is not an acceptable failure mode.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.common.db import Base, UtcDateTime


class RawChainEvent(Base):
    """Persisted before anything is done with it (02_PLAN_B design principle 2)."""

    __tablename__ = "raw_chain_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(String(96), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    wallet_hint: Mapped[str | None] = mapped_column(String(64))
    received_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    processed_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime)
    decode_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("signature", name="uq_raw_chain_events_signature"),
        Index("ix_raw_chain_events_slot", "slot"),
        Index("ix_raw_chain_events_unprocessed", "processed_at_utc"),
    )


class StoredSwapEvent(Base):
    """The canonical SwapEvent, stored by its deterministic id."""

    __tablename__ = "swap_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signature: Mapped[str] = mapped_column(String(96), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    block_time_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    wallet: Mapped[str] = mapped_column(String(64), nullable=False)
    base_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    base_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False)
    quote_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    instruction_path: Mapped[str] = mapped_column(String(32), nullable=False)
    net_swap_index: Mapped[int] = mapped_column(Integer, nullable=False)
    decode_confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    received_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_swap_events_wallet_time", "wallet", "block_time_utc"),
        Index("ix_swap_events_signature", "signature"),
        Index("ix_swap_events_mint", "base_mint"),
    )


class StoredCopyIntent(Base):
    """Every decision, TRADE and NO_TRADE alike, so discards stay countable."""

    __tablename__ = "copy_intents"

    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_wallet: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(48))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    base_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_mint: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    selection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime)
    settled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        # S0 4.5 invariant: one source_event_id backs at most one EXECUTE intent.
        # NO_TRADE rows are excluded so a re-evaluation under a new selection is still
        # recordable - Plan A needs the full rejection history.
        Index(
            "uq_copy_intents_event_execute",
            "source_event_id",
            unique=True,
            sqlite_where=(decision == "EXECUTE"),
        ),
        Index("ix_copy_intents_event", "source_event_id"),
        Index("ix_copy_intents_open", "settled", "decision"),
        Index("ix_copy_intents_reason", "reason_code"),
    )


class StoredQuote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    in_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False)
    out_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False)
    price_impact_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (Index("ix_quotes_intent", "intent_id"),)


class ExecutionAttempt(Base):
    """Written *before* an execution is attempted, so a crash mid-flight is visible."""

    __tablename__ = "execution_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    broker: Mapped[str] = mapped_column(String(8), nullable=False)
    started_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at_utc: Mapped[datetime | None] = mapped_column(UtcDateTime)
    outcome: Mapped[str | None] = mapped_column(String(24))

    __table_args__ = (
        UniqueConstraint("intent_id", "attempt", name="uq_execution_attempts_intent_attempt"),
        Index("ix_execution_attempts_open", "finished_at_utc"),
    )


class StoredExecutionResult(Base):
    __tablename__ = "execution_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    broker: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    economically_effective: Mapped[bool] = mapped_column(Boolean, nullable=False)
    signature: Mapped[str | None] = mapped_column(String(96))
    finished_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("intent_id", "attempt", name="uq_execution_results_intent_attempt"),
        # The core money invariant: an intent can never have two effective fills.
        Index(
            "uq_execution_results_effective",
            "intent_id",
            unique=True,
            sqlite_where=(economically_effective == True),  # noqa: E712
        ),
        Index("ix_execution_results_status", "status"),
    )


class SourceWalletPosition(Base):
    """GundiX's own copy position, attributed to the wallet whose trade caused it."""

    __tablename__ = "source_wallet_positions"

    source_wallet: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    quote_cost_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    quote_proceeds_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    opened_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (Index("ix_positions_mint", "base_mint"),)


class TokenExposure(Base):
    """Aggregate across all source wallets, because limits are per token, not per wallet."""

    __tablename__ = "token_exposure"

    base_mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_amount_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    quote_cost_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DailyRiskLedger(Base):
    """Survives restarts, so a daily limit cannot be reset by bouncing the process."""

    __tablename__ = "daily_risk_ledger"

    day: Mapped[date] = mapped_column(String(10), primary_key=True)
    quote_deployed_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    realized_pnl_quote_raw: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ActiveSelection(Base):
    """Singleton row recording which selection is live right now."""

    __tablename__ = "active_selection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    selection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    score_version: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    activated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    wallet_count: Mapped[int] = mapped_column(Integer, nullable=False)


class ServiceCheckpoint(Base):
    """Cursors and counters that must survive a restart."""

    __tablename__ = "service_checkpoints"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


ALL_TABLES = (
    RawChainEvent,
    StoredSwapEvent,
    StoredCopyIntent,
    StoredQuote,
    ExecutionAttempt,
    StoredExecutionResult,
    SourceWalletPosition,
    TokenExposure,
    DailyRiskLedger,
    ActiveSelection,
    ServiceCheckpoint,
)
