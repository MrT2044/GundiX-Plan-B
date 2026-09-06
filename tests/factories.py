"""Builders for controlled test scenarios.

Real recorded transactions verify the decoder. They are a poor tool for testing the policy
and the broker, where the point is to construct one precise situation - a 30 % sell, a
stale quote, an exhausted daily limit - and see what the code does with it. These builders
produce contract-valid objects with everything but the field under test held fixed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from gundix_contracts.enums import (
    DecodeConfidence,
    EventSource,
    FeeAttribution,
    Finality,
    Side,
    Venue,
    WalletStatus,
)
from gundix_contracts.ids import compute_event_id, config_hash
from gundix_contracts.mints import WSOL_MINT
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    CopyPnlByLatency,
    ManualApproval,
    SelectedWallet,
    SwapEvent,
    SwapFees,
    TimeWindow,
    TraderSelection,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

WALLET_A = "3xWFCqaiV3LdqZbHcVa9J6CMFa9YJV5W8cJL1DyUdi7z"
WALLET_B = "7cTUhpTPbC6F4Q6gQ3jkurQ5Hz5sQ7tXRWTbq7U4n8hj"
MINT_X = "AjToeC9PjBK7NnHG8xFz3bwu3UZLQC37LfyPjUDRpump"
MINT_Y = "FSh39Pks5s4kUxvXTbGoTLTqhnQGTswGriBrTuVXpump"
POOL = "9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"


def make_signature(seed: int = 1) -> str:
    """A syntactically valid 88-character base58 signature."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(alphabet[(seed * (i + 7)) % len(alphabet)] for i in range(88))


def make_swap_event(
    *,
    wallet: str = WALLET_A,
    base_mint: str = MINT_X,
    quote_mint: str = WSOL_MINT,
    side: Side = Side.BUY,
    base_amount_raw: int = 1_000_000_000,
    quote_amount_raw: int = 50_000_000,
    base_decimals: int = 6,
    quote_decimals: int = 9,
    venue: Venue = Venue.PUMP_FUN,
    block_time: datetime = T0,
    observed_at: datetime | None = None,
    net_swap_index: int = 0,
    instruction_path: str = "3",
    success: bool = True,
    decode_confidence: DecodeConfidence = DecodeConfidence.COMPLETE,
    finality: Finality = Finality.CONFIRMED,
    signature_seed: int = 1,
    fee_lamports: int = 5_000,
) -> SwapEvent:
    signature = make_signature(signature_seed)
    return SwapEvent(
        schema_version=SCHEMA_VERSIONS["swap_event"],
        event_id=compute_event_id("solana", signature, wallet, instruction_path, net_swap_index),
        signature=signature,
        slot=300_000_000 + signature_seed,
        block_time_utc=block_time,
        transaction_index=None,
        instruction_path=instruction_path,
        net_swap_index=net_swap_index,
        wallet=wallet,
        base_mint=base_mint,
        quote_mint=quote_mint,
        side=side,
        base_amount_raw=base_amount_raw,
        quote_amount_raw=quote_amount_raw,
        base_decimals=base_decimals,
        quote_decimals=quote_decimals,
        venue=venue,
        pool=POOL,
        router=None,
        success=success,
        source=EventSource.REPLAY,
        source_provider="fixture",
        observed_at_utc=observed_at or block_time + timedelta(seconds=1),
        finality=finality,
        fees=SwapFees(
            network_fee_lamports=fee_lamports if net_swap_index == 0 else 0,
            fee_attribution=FeeAttribution.FULL if net_swap_index == 0 else FeeAttribution.NONE,
        ),
        decode_confidence=decode_confidence,
    )


def make_selection(
    *,
    wallets: tuple[str, ...] = (WALLET_A,),
    approved: bool = True,
    status: WalletStatus = WalletStatus.ACTIVE,
    now: datetime = T0,
    expires_in_days: int = 30,
    selection_id: str = "test_selection",
    weight: Decimal = Decimal("1.0"),
) -> TraderSelection:
    zero = CopyPnlByLatency.model_validate(
        {"1s": "0", "3s": "0", "5s": "0", "15s": "0", "60s": "0"}
    )
    entries = tuple(
        SelectedWallet(
            wallet=wallet,
            status=status,
            rank=index + 1,
            score=Decimal("1.5"),
            weight=weight / Decimal(len(wallets)),
            max_weight=Decimal("1.0"),
            reason_codes=("test",),
            risks=(),
            coverage_ratio=Decimal("0.95"),
            metrics_ref=None,
            copy_pnl_by_latency=zero,
        )
        for index, wallet in enumerate(wallets)
    )
    return TraderSelection(
        schema_version=SCHEMA_VERSIONS["trader_selection"],
        selection_id=selection_id,
        created_at_utc=now,
        data_snapshot_id="test-snapshot",
        code_commit="a" * 40,
        params_hash=config_hash({"test": True}),
        score_version="1.0.0",
        selection_window=TimeWindow(
            label="A",
            start_utc=now - timedelta(days=135),
            end_utc=now - timedelta(days=45),
            start_slot=1,
            end_slot=2,
        ),
        evaluation_window=TimeWindow(
            label="B",
            start_utc=now - timedelta(days=45),
            end_utc=now,
            start_slot=3,
            end_slot=4,
        ),
        wallets=entries,
        applied_filters=(),
        manual_approval=ManualApproval(
            approved=approved,
            approved_by="test-runner",
            approved_at_utc=now,
            note="test fixture",
        ),
        expires_at_utc=now + timedelta(days=expires_in_days),
    )


def write_selection_artifact(path, selection: TraderSelection):
    """Write a selection with its manifest, the way Plan A would deliver it."""
    from gundix_contracts.artifacts import write_json_artifact
    from gundix_contracts.enums import ArtifactType

    return write_json_artifact(
        path=path,
        document=selection.to_wire(),
        artifact_type=ArtifactType.TRADER_SELECTION,
        plan="A",
        component="research.selection",
        component_version="1.0.0",
        config_hash=config_hash({"test": True}),
        repo_root=path.parent,
        contracts_root=path.parent,
    )
