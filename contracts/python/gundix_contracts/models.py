"""Pydantic models for the shared GundiX contracts.

JSON Schema under ``contracts/schemas/`` is the source of truth. These models are tested
against it, so the two cannot drift apart silently.

Rules for every model here:

* ``extra="forbid"`` - an unknown field is an error, not a tolerance (S0 section 4).
* ``frozen=True`` - a recorded event is history and is never mutated in place.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gundix_contracts.enums import (
    ArtifactType,
    Decision,
    DecodeConfidence,
    DedupPolicy,
    EventSource,
    ExecutionErrorCode,
    ExecutionStatus,
    FeeAttribution,
    Finality,
    NoTradeReason,
    OperatingMode,
    RouterLabel,
    Side,
    Venue,
    WalletStatus,
)
from gundix_contracts.types import (
    Bps,
    Decimals,
    GitSha,
    InstructionPath,
    RawAmount,
    SchemaVersion,
    SelectionId,
    SemVer,
    Sha256Hex,
    Signature,
    SignedDecimal,
    SlippageBps,
    Slot,
    SolanaAddress,
    UnsignedDecimal,
    UtcMillis,
    UtcSecond,
)

#: Current version of each contract. A reader rejects an unknown MAJOR fail-closed and
#: accepts (but logs) an unknown MINOR - S0 section 11.2.
SCHEMA_VERSIONS: dict[str, str] = {
    "swap_event": "1.0.0",
    "trader_selection": "1.0.0",
    "copy_intent": "1.0.0",
    "execution_result": "1.0.0",
    "artifact_manifest": "1.0.0",
    "latency_observation": "1.0.0",
    "decoder_coverage": "1.0.0",
}


class ContractVersionError(ValueError):
    """A document's MAJOR version is not supported by this build."""


def check_schema_version(contract: str, version: str) -> list[str]:
    """Fail-closed MAJOR gate. Returns warnings for an unknown MINOR."""
    expected = SCHEMA_VERSIONS[contract]
    got_parts = version.split(".")
    want_parts = expected.split(".")
    if got_parts[0] != want_parts[0]:
        raise ContractVersionError(
            f"{contract}: unsupported schema_version {version!r}; this build speaks {expected!r}. "
            "Refusing to guess a migration."
        )
    warnings: list[str] = []
    if int(got_parts[1]) > int(want_parts[1]):
        warnings.append(
            f"{contract}: document minor version {version} is newer than this build's {expected}; "
            "unknown optional fields are ignored"
        )
    return warnings


class GundixModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_wire(self) -> dict[str, Any]:
        """Serialise into the exact JSON shape the schemas describe."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------------------
# SwapEvent
# --------------------------------------------------------------------------------------
class RouterInfo(GundixModel):
    program_id: SolanaAddress
    label: RouterLabel
    hops: int = Field(ge=1)


class SwapFees(GundixModel):
    """S0 4.1.8.

    Transaction-wide fees sit entirely on the event with ``net_swap_index == 0``; every
    further event of the same wallet in the same transaction carries zeros and
    ``fee_attribution = NONE``. The sum over all events is therefore exactly the real
    transaction fee - no double counting, no loss.
    """

    network_fee_lamports: RawAmount = 0
    priority_fee_lamports: RawAmount = 0
    tip_lamports: RawAmount = 0
    venue_fee_raw: RawAmount = 0
    venue_fee_mint: SolanaAddress | None = None
    transfer_fee_base_raw: RawAmount = 0
    transfer_fee_quote_raw: RawAmount = 0
    fee_attribution: FeeAttribution = FeeAttribution.NONE

    @model_validator(mode="after")
    def _attribution_consistent(self) -> Self:
        transaction_wide = (
            self.network_fee_lamports + self.priority_fee_lamports + self.tip_lamports
        )
        if self.fee_attribution is FeeAttribution.NONE and transaction_wide != 0:
            raise ValueError(
                "fee_attribution=NONE requires zero transaction-wide fees, otherwise the sum "
                "over all events of a transaction would exceed the real fee"
            )
        if self.venue_fee_raw > 0 and self.venue_fee_mint is None:
            raise ValueError("a venue fee needs the mint it was charged in")
        return self


class SwapEvent(GundixModel):
    """The canonical representation of a detected swap. There is exactly this one."""

    schema_version: SchemaVersion
    chain: Literal["solana"] = "solana"
    event_id: Sha256Hex
    signature: Signature
    slot: Slot
    block_time_utc: UtcSecond
    transaction_index: int | None = Field(default=None, ge=0)
    instruction_path: InstructionPath
    net_swap_index: int = Field(ge=0)
    wallet: SolanaAddress
    base_mint: SolanaAddress
    quote_mint: SolanaAddress
    side: Side
    base_amount_raw: RawAmount
    quote_amount_raw: RawAmount
    base_decimals: Decimals
    quote_decimals: Decimals
    venue: Venue
    pool: SolanaAddress | None
    router: RouterInfo | None
    success: bool
    source: EventSource
    source_provider: str = Field(min_length=1)
    observed_at_utc: UtcMillis
    finality: Finality
    fees: SwapFees
    decode_confidence: DecodeConfidence

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        check_schema_version("swap_event", self.schema_version)
        if self.base_mint == self.quote_mint:
            raise ValueError("base_mint and quote_mint must differ")
        if self.venue is Venue.UNKNOWN and self.decode_confidence is DecodeConfidence.COMPLETE:
            raise ValueError("an unknown venue cannot yield decode_confidence=COMPLETE (S0 4.1.5)")
        if self.net_swap_index > 0 and self.fees.fee_attribution is FeeAttribution.FULL:
            raise ValueError(
                "transaction-wide fees are attributed to net_swap_index 0 only (S0 4.1.8)"
            )
        return self

    @property
    def is_economically_usable(self) -> bool:
        """S0 4.1.7 and 4.1.9: successful *and* completely decoded. Necessary, never sufficient."""
        return self.success and self.decode_confidence.is_usable

    def price_in_quote(self) -> Decimal:
        """Executed price of one whole base token, in whole quote tokens."""
        if self.base_amount_raw == 0:
            raise ValueError("cannot derive a price from a zero base amount")
        base = Decimal(self.base_amount_raw) / (Decimal(10) ** self.base_decimals)
        quote = Decimal(self.quote_amount_raw) / (Decimal(10) ** self.quote_decimals)
        return quote / base


# --------------------------------------------------------------------------------------
# TraderSelection
# --------------------------------------------------------------------------------------
class TimeWindow(GundixModel):
    label: str = Field(min_length=1)
    start_utc: UtcSecond
    end_utc: UtcSecond
    start_slot: Slot
    end_slot: Slot

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc must be after start_utc")
        if self.end_slot < self.start_slot:
            raise ValueError("end_slot must not precede start_slot")
        return self


class ManualApproval(GundixModel):
    approved: bool
    approved_by: str = Field(min_length=1)
    approved_at_utc: UtcMillis
    note: str | None = None


class AppliedFilter(GundixModel):
    filter_id: str = Field(min_length=1)
    threshold: str | None
    threshold_version: SemVer
    wallets_removed: int = Field(ge=0)


class CopyPnlByLatency(GundixModel):
    """Plan A's copy PnL per latency scenario. Documentation for Plan B, never a control input."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    s1: SignedDecimal = Field(alias="1s")
    s3: SignedDecimal = Field(alias="3s")
    s5: SignedDecimal = Field(alias="5s")
    s15: SignedDecimal = Field(alias="15s")
    s60: SignedDecimal = Field(alias="60s")

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class SelectedWallet(GundixModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    wallet: SolanaAddress
    status: WalletStatus
    rank: int = Field(ge=1)
    score: SignedDecimal
    weight: UnsignedDecimal
    max_weight: UnsignedDecimal
    reason_codes: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    coverage_ratio: UnsignedDecimal
    metrics_ref: str | None = None
    copy_pnl_by_latency: CopyPnlByLatency

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.weight <= 0 or self.weight > 1:
            raise ValueError("weight must be in (0, 1]")
        if self.max_weight <= 0 or self.max_weight > 1:
            raise ValueError("max_weight must be in (0, 1]")
        if self.weight > self.max_weight:
            raise ValueError("weight must not exceed max_weight (S0 4.4 rejection rule 7)")
        if not (Decimal(0) <= self.coverage_ratio <= Decimal(1)):
            raise ValueError("coverage_ratio must be in [0, 1]")
        return self

    def to_wire(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", by_alias=True)
        payload["copy_pnl_by_latency"] = self.copy_pnl_by_latency.to_wire()
        return payload


class TraderSelection(GundixModel):
    schema_version: SchemaVersion
    selection_id: SelectionId
    created_at_utc: UtcMillis
    data_snapshot_id: str = Field(min_length=1)
    code_commit: GitSha
    params_hash: Sha256Hex
    score_version: SemVer
    selection_window: TimeWindow
    evaluation_window: TimeWindow
    wallets: tuple[SelectedWallet, ...]
    applied_filters: tuple[AppliedFilter, ...] = ()
    manual_approval: ManualApproval
    expires_at_utc: UtcMillis

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        check_schema_version("trader_selection", self.schema_version)
        seen: set[str] = set()
        total_weight = Decimal(0)
        for entry in self.wallets:
            if entry.wallet in seen:
                raise ValueError(f"duplicate wallet in selection: {entry.wallet}")
            seen.add(entry.wallet)
            total_weight += entry.weight
        if total_weight > 1:
            raise ValueError(
                f"sum of weights is {total_weight}, which exceeds 1 (S0 4.4 rejection rule 7)"
            )
        return self

    @property
    def active_wallets(self) -> tuple[SelectedWallet, ...]:
        return tuple(w for w in self.wallets if w.status.may_trade)

    def to_wire(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["wallets"] = [wallet.to_wire() for wallet in self.wallets]
        return payload


# --------------------------------------------------------------------------------------
# CopyIntent
# --------------------------------------------------------------------------------------
class CopyIntent(GundixModel):
    schema_version: SchemaVersion
    intent_id: Sha256Hex
    source_event_id: Sha256Hex
    source_wallet: SolanaAddress
    created_at_utc: UtcMillis
    base_mint: SolanaAddress
    quote_mint: SolanaAddress
    side: Side
    target_size_quote_raw: RawAmount
    target_fraction_bps: int | None = Field(default=None, ge=1, le=10_000)
    target_base_raw: RawAmount | None = None
    max_slippage_bps: SlippageBps
    expires_at_utc: UtcMillis
    mode: OperatingMode
    decision: Decision
    reason_code: NoTradeReason | None
    reason_detail: str | None = None
    selection_id: str = Field(min_length=1)
    policy_version: SemVer
    dedup_policy: DedupPolicy

    @model_validator(mode="after")
    def _decision_shape(self) -> Self:
        check_schema_version("copy_intent", self.schema_version)
        if self.decision is Decision.NO_TRADE:
            if self.reason_code is None:
                raise ValueError("a NO_TRADE decision must name a machine readable reason")
            return self
        if self.reason_code is not None:
            raise ValueError("an EXECUTE decision must not carry a reason_code")
        if self.side is Side.BUY:
            if self.target_size_quote_raw <= 0:
                raise ValueError("a BUY intent must spend a positive quote amount")
            if self.target_fraction_bps is not None or self.target_base_raw is not None:
                raise ValueError("a BUY intent is sized in quote units only")
        else:
            if self.target_fraction_bps is None or self.target_base_raw is None:
                raise ValueError(
                    "a SELL intent must carry both the fraction the source wallet sold and the "
                    "resolved base amount it maps to"
                )
            if self.target_base_raw <= 0:
                raise ValueError("a SELL intent must sell a positive base amount")
        return self

    @property
    def is_executable(self) -> bool:
        return self.decision is Decision.EXECUTE


# --------------------------------------------------------------------------------------
# ExecutionResult
# --------------------------------------------------------------------------------------
class QuoteSnapshot(GundixModel):
    provider: str = Field(min_length=1)
    requested_at_utc: UtcMillis
    expires_at_utc: UtcMillis
    in_amount_raw: RawAmount
    out_amount_raw: RawAmount
    price_impact_bps: Bps
    route_hops: int = Field(ge=1)
    platform_fee_raw: RawAmount = 0

    def is_stale_at(self, now: Any) -> bool:
        return bool(now >= self.expires_at_utc)


class ExecutionFees(GundixModel):
    network_fee_lamports: RawAmount = 0
    priority_fee_lamports: RawAmount = 0
    tip_lamports: RawAmount = 0
    venue_fee_raw: RawAmount = 0


class LatenciesMs(GundixModel):
    """Stage names are exactly those of Plan B B2; Plan A reads them by name in A10."""

    block_to_receive: int | None = None
    receive_to_decode: int | None = None
    decode_to_decision: int | None = None
    decision_to_quote: int | None = None
    quote_to_submit: int | None = None
    submit_to_confirm: int | None = None


class ExecutionResult(GundixModel):
    schema_version: SchemaVersion
    result_id: Sha256Hex
    intent_id: Sha256Hex
    mode: OperatingMode
    status: ExecutionStatus
    quote: QuoteSnapshot | None
    expected_price: UnsignedDecimal
    realized_price: UnsignedDecimal
    filled_base_raw: RawAmount
    filled_quote_raw: RawAmount
    fees: ExecutionFees
    latencies_ms: LatenciesMs
    signature: Signature | None
    error_code: ExecutionErrorCode | None
    error_detail: str | None = None
    created_at_utc: UtcMillis
    finalized_at_utc: UtcMillis

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        check_schema_version("execution_result", self.schema_version)
        if self.status.is_economically_effective:
            if self.filled_base_raw == 0 or self.filled_quote_raw == 0:
                raise ValueError(
                    f"status {self.status.value} requires a non-zero fill on both sides"
                )
            if self.error_code is not None:
                raise ValueError("a fill must not carry an error_code")
        else:
            if self.filled_base_raw != 0 or self.filled_quote_raw != 0:
                raise ValueError(
                    f"status {self.status.value} is not a fill, so both filled amounts must be 0"
                )
            if self.status is not ExecutionStatus.UNKNOWN and self.error_code is None:
                raise ValueError("a non-fill must name a machine readable error_code")
        if self.mode in {OperatingMode.PAPER, OperatingMode.SHADOW} and self.signature is not None:
            raise ValueError("paper and shadow results must never carry a transaction signature")
        if self.finalized_at_utc < self.created_at_utc:
            raise ValueError("finalized_at_utc must not precede created_at_utc")
        return self

    @property
    def is_economically_effective(self) -> bool:
        return self.status.is_economically_effective


# --------------------------------------------------------------------------------------
# Observation artifacts owned by Plan B
# --------------------------------------------------------------------------------------
class LatencyObservation(GundixModel):
    schema_version: SchemaVersion
    event_id: Sha256Hex
    signature: Signature
    wallet: SolanaAddress
    slot: Slot
    block_time_utc: UtcSecond
    received_at_utc: UtcMillis
    decoded_at_utc: UtcMillis
    decided_at_utc: UtcMillis
    quoted_at_utc: UtcMillis | None = None
    submitted_at_utc: UtcMillis | None = None
    latencies_ms: LatenciesMs
    source: EventSource
    source_provider: str = Field(min_length=1)
    mode: OperatingMode
    decision: Decision
    reason_code: str | None = None

    @model_validator(mode="after")
    def _version(self) -> Self:
        check_schema_version("latency_observation", self.schema_version)
        return self


class UnknownProgramCount(GundixModel):
    program_id: SolanaAddress
    occurrences: int = Field(ge=1)
    affected_wallets: int = Field(ge=0)


class VenueCount(GundixModel):
    venue: Venue
    events: int = Field(ge=0)


class QuarantineReasonCount(GundixModel):
    reason: str = Field(min_length=1)
    count: int = Field(ge=1)


class CoverageWindow(GundixModel):
    start_utc: UtcMillis
    end_utc: UtcMillis


class DecoderCoverage(GundixModel):
    schema_version: SchemaVersion
    window: CoverageWindow
    transactions_seen: int = Field(ge=0)
    transactions_failed_onchain: int = Field(ge=0)
    transactions_decoded_complete: int = Field(ge=0)
    transactions_partial: int = Field(ge=0)
    transactions_unknown: int = Field(ge=0)
    transactions_no_swap: int = Field(ge=0)
    coverage_ratio: UnsignedDecimal
    events_emitted: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    unknown_programs: tuple[UnknownProgramCount, ...] = ()
    venues_seen: tuple[VenueCount, ...] = ()
    quarantine_reasons: tuple[QuarantineReasonCount, ...] = ()

    @model_validator(mode="after")
    def _version(self) -> Self:
        check_schema_version("decoder_coverage", self.schema_version)
        return self


# --------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------
class Producer(GundixModel):
    plan: Literal["A", "B"]
    component: str = Field(min_length=1)
    version: SemVer


class ObservationWindow(GundixModel):
    start_utc: UtcMillis
    end_utc: UtcMillis


class ArtifactManifest(GundixModel):
    schema_version: SchemaVersion
    artifact_type: ArtifactType
    file_name: str = Field(min_length=1)
    file_bytes: int = Field(ge=0)
    content_sha256: Sha256Hex
    record_schema_version: SchemaVersion
    record_count: int = Field(ge=0)
    producer: Producer
    git_commit: str
    contracts_commit: str
    config_hash: Sha256Hex
    created_at_utc: UtcMillis
    data_snapshot_id: str | None = None
    observation_window: ObservationWindow | None = None

    @model_validator(mode="after")
    def _no_traversal(self) -> Self:
        if "/" in self.file_name or "\\" in self.file_name or ".." in self.file_name:
            raise ValueError("file_name must be a plain file name next to the manifest")
        return self
