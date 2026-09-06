"""Closed vocabularies. Every member mirrors an enum in ``contracts/schemas/``.

``tests/contracts/test_enums_match_schemas.py`` fails if the two ever drift apart.
"""

from __future__ import annotations

from enum import StrEnum


class OperatingMode(StrEnum):
    """00_GESAMTPLAN section 9 / S0 section 8."""

    RESEARCH = "RESEARCH"
    OBSERVE = "OBSERVE"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"

    @property
    def creates_intents(self) -> bool:
        """RESEARCH and OBSERVE decode and record, but never produce a tradeable intent."""
        return self in {OperatingMode.PAPER, OperatingMode.SHADOW, OperatingMode.LIVE}

    @property
    def may_load_signer(self) -> bool:
        return self is OperatingMode.LIVE

    @property
    def may_broadcast(self) -> bool:
        return self is OperatingMode.LIVE


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Finality(StrEnum):
    PROCESSED = "PROCESSED"
    CONFIRMED = "CONFIRMED"
    FINALIZED = "FINALIZED"

    @property
    def rank(self) -> int:
        return {"PROCESSED": 0, "CONFIRMED": 1, "FINALIZED": 2}[self.value]

    def at_least(self, required: Finality) -> bool:
        return self.rank >= required.rank


class Venue(StrEnum):
    """S0 4.1.5. Extending this enum is a CCR."""

    PUMP_FUN = "PUMP_FUN"
    PUMPSWAP = "PUMPSWAP"
    RAYDIUM_AMM_V4 = "RAYDIUM_AMM_V4"
    RAYDIUM_CPMM = "RAYDIUM_CPMM"
    RAYDIUM_CLMM = "RAYDIUM_CLMM"
    RAYDIUM_LAUNCHLAB = "RAYDIUM_LAUNCHLAB"
    METEORA_DLMM = "METEORA_DLMM"
    METEORA_DAMM = "METEORA_DAMM"
    ORCA_WHIRLPOOL = "ORCA_WHIRLPOOL"
    UNKNOWN = "UNKNOWN"


class RouterLabel(StrEnum):
    JUPITER_V6 = "JUPITER_V6"
    JUPITER_V4 = "JUPITER_V4"
    OKX_DEX = "OKX_DEX"
    OTHER = "OTHER"


class DecodeConfidence(StrEnum):
    """S0 4.1.9. Only COMPLETE is economically usable."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"

    @property
    def is_usable(self) -> bool:
        return self is DecodeConfidence.COMPLETE


class EventSource(StrEnum):
    HISTORICAL_BACKFILL = "HISTORICAL_BACKFILL"
    LIVE_STREAM = "LIVE_STREAM"
    REPLAY = "REPLAY"
    GOLDEN_FIXTURE = "GOLDEN_FIXTURE"

    @property
    def is_live_measurement(self) -> bool:
        """Only live observations may be used for latency statistics."""
        return self is EventSource.LIVE_STREAM


class WalletStatus(StrEnum):
    ACTIVE = "ACTIVE"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SUSPENDED = "SUSPENDED"

    @property
    def may_trade(self) -> bool:
        return self is WalletStatus.ACTIVE


class Decision(StrEnum):
    EXECUTE = "EXECUTE"
    NO_TRADE = "NO_TRADE"


class NoTradeReason(StrEnum):
    """02_PLAN_B section 5. A discarded trade must stay explainable and countable."""

    WALLET_NOT_SELECTED = "wallet_not_selected"
    SELECTION_INVALID = "selection_invalid"
    SELECTION_EXPIRED = "selection_expired"
    DUPLICATE_EVENT = "duplicate_event"
    EVENT_TOO_OLD = "event_too_old"
    EVENT_NOT_FINAL = "event_not_final"
    DECODE_INCOMPLETE = "decode_incomplete"
    UNKNOWN_VENUE = "unknown_venue"
    TRANSFER_NOT_SWAP = "transfer_not_swap"
    UNSUPPORTED_TOKEN = "unsupported_token"
    NO_ROUTE = "no_route"
    QUOTE_STALE = "quote_stale"
    SLIPPAGE_TOO_HIGH = "slippage_too_high"
    LIQUIDITY_TOO_LOW = "liquidity_too_low"
    POSITION_LIMIT = "position_limit"
    DAILY_LIMIT = "daily_limit"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    STATE_MISMATCH = "state_mismatch"
    NO_COPY_POSITION_TO_SELL = "no_copy_position_to_sell"
    MODE_DISALLOWS_EXECUTION = "mode_disallows_execution"
    # Strictly narrower additions beyond the plan's minimum list; see CCR-002.
    TRANSACTION_FAILED = "transaction_failed"
    DEDUP_POLICY_SUPPRESSED = "dedup_policy_suppressed"
    SIZE_BELOW_MINIMUM = "size_below_minimum"


class DedupPolicy(StrEnum):
    """02_PLAN_B B4. Phase 1 starts conservatively with FIRST_SIGNAL_ONLY."""

    FIRST_SIGNAL_ONLY = "FIRST_SIGNAL_ONLY"
    CONSENSUS_REQUIRED = "CONSENSUS_REQUIRED"
    INCREMENT_WITH_CAP = "INCREMENT_WITH_CAP"


class ExecutionStatus(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_economically_effective(self) -> bool:
        """Only these two change a position."""
        return self in {ExecutionStatus.FILLED, ExecutionStatus.PARTIAL}

    @property
    def is_terminal(self) -> bool:
        """UNKNOWN is deliberately not terminal: it is a blocking state (S0 4.6)."""
        return self is not ExecutionStatus.UNKNOWN


class ExecutionErrorCode(StrEnum):
    NO_ROUTE = "NO_ROUTE"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SIMULATED_FAIL = "SIMULATED_FAIL"
    TOKEN_RESTRICTED = "TOKEN_RESTRICTED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    STATE_MISMATCH = "STATE_MISMATCH"


class ArtifactType(StrEnum):
    WALLET_CANDIDATES = "wallet_candidates"
    TRADER_SELECTION = "trader_selection"
    SWAP_EVENTS = "swap_events"
    COPY_INTENTS = "copy_intents"
    PAPER_FILLS = "paper_fills"
    LATENCY = "latency"
    DECODER_COVERAGE = "decoder_coverage"
    REPORT = "report"


class FeeAttribution(StrEnum):
    """S0 4.1.8: transaction-wide fees sit entirely on net_swap_index 0."""

    FULL = "FULL"
    NONE = "NONE"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED_OBSERVATION = "degraded_observation"
    EXECUTION_BLOCKED = "execution_blocked"
    FATAL = "fatal"
