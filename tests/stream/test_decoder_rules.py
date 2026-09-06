"""Aggregation rules of S0 section 5, against real recorded transactions.

These are the rules that decide whether Plan A and Plan B agree (I2). They are checked
against real mainnet payloads rather than hand-built ones, because the interesting failures
- a bonding curve moving lamports with no instruction, a bot program wrapping the venue
call - do not occur in payloads written by the person writing the test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from gundix_contracts.enums import DecodeConfidence, EventSource, Venue
from gundix_contracts.schema_registry import validate_document

from src.stream.decoder import (
    QuarantineReason,
    SwapDecoder,
    UnknownProgramPolicy,
    sol_sphere_flow,
    token_balance_deltas,
)
from src.stream.decoder import account_keys as decoder_account_keys
from src.stream.decoder import token_accounts as decoder_token_accounts
from src.stream.programs import (
    INFRASTRUCTURE_PROGRAMS,
    PUMP_FEE_PROGRAM,
    SWAP_PROGRAMS,
    VENUE_PROGRAMS,
    is_known_program,
)
from tests.conftest import T0, golden_names, load_golden

CONTRACTUAL = SwapDecoder(
    source=EventSource.GOLDEN_FIXTURE,
    source_provider="fixture",
    unknown_program_policy=UnknownProgramPolicy.QUARANTINE_TRANSACTION,
)
RECONCILING = SwapDecoder(
    source=EventSource.GOLDEN_FIXTURE,
    source_provider="fixture",
    unknown_program_policy=UnknownProgramPolicy.RECONCILE_BALANCES,
)


def decode(name: str, decoder: SwapDecoder = CONTRACTUAL):
    fixture = load_golden(name)
    return decoder.decode(
        fixture["transaction"], wallets=fixture["wallets"], observed_at_utc=T0
    ), fixture


# -- rule 8: a failed transaction is never an executed trade ------------------------------
@pytest.mark.parametrize("name", [n for n in golden_names() if n.endswith("_failed")])
def test_a_failed_transaction_produces_no_event(name: str) -> None:
    result, _ = decode(name)
    assert result.success is False
    assert result.events == ()
    assert result.quarantine_reasons == ()


# -- rule 9: an unknown program in the path quarantines the whole transaction ---------------
def test_rule_9_quarantines_the_whole_transaction() -> None:
    """The contractual behaviour: no partial event, ever."""
    result, _ = decode("pumpfun_ok0")
    assert result.events == ()
    assert result.quarantined_wallets
    assert any(
        reason.startswith(QuarantineReason.UNKNOWN_PROGRAM_IN_PATH)
        for reason in result.quarantine_reasons
    )


def test_rule_9_is_what_blocks_these_transactions_and_nothing_else() -> None:
    """Evidence for CCR-001, kept as a test so the claim stays true or fails loudly.

    Under the contractual rule these recorded transactions yield no usable events at all.
    Under the proposed rule the same payloads decode, and the amounts are the ones verified
    against Pump.fun's own program event in test_decoder_program_events.py.
    """
    contractual_usable = 0
    reconciling_usable = 0
    for name in golden_names():
        strict, _ = decode(name, CONTRACTUAL)
        loose, _ = decode(name, RECONCILING)
        contractual_usable += sum(1 for e in strict.events if e.is_economically_usable)
        reconciling_usable += sum(1 for e in loose.events if e.is_economically_usable)

    assert contractual_usable == 0
    assert reconciling_usable > 0


def test_the_pump_fee_program_is_recognised_infrastructure() -> None:
    """It only routes a fee already inside the wallet's net SOL flow, so it changes no amount."""
    assert PUMP_FEE_PROGRAM in INFRASTRUCTURE_PROGRAMS
    assert is_known_program(PUMP_FEE_PROGRAM)


# -- amounts and confidence ------------------------------------------------------------------
def test_decoded_events_validate_against_the_schema() -> None:
    for name in golden_names():
        result, _ = decode(name, RECONCILING)
        for event in result.events:
            validate_document("swap_event", event.to_wire())


def test_raw_amounts_are_never_negative() -> None:
    for name in golden_names():
        result, _ = decode(name, RECONCILING)
        for event in result.events:
            assert event.base_amount_raw >= 0
            assert event.quote_amount_raw >= 0


def test_an_unknown_venue_can_never_be_complete() -> None:
    for name in golden_names():
        result, _ = decode(name, RECONCILING)
        for event in result.events:
            if event.venue is Venue.UNKNOWN:
                assert event.decode_confidence is not DecodeConfidence.COMPLETE


def test_transaction_wide_fees_sit_on_net_swap_index_zero() -> None:
    """S0 4.1.8: the sum over all events of a transaction is exactly the real fee."""
    for name in golden_names():
        result, _ = decode(name, RECONCILING)
        if not result.events:
            continue
        by_signature: dict[str, list[Any]] = {}
        for event in result.events:
            by_signature.setdefault(event.signature, []).append(event)
        for events in by_signature.values():
            total = sum(
                e.fees.network_fee_lamports + e.fees.priority_fee_lamports + e.fees.tip_lamports
                for e in events
            )
            assert total == result.fee_lamports
            for event in events:
                if event.net_swap_index > 0:
                    assert event.fees.fee_attribution.value == "NONE"


# -- determinism ---------------------------------------------------------------------------------
def test_decoding_is_deterministic() -> None:
    """Same input, same event ids. Otherwise deduplication across restarts is impossible."""
    for name in golden_names():
        first, _ = decode(name, RECONCILING)
        second, _ = decode(name, RECONCILING)
        assert [e.event_id for e in first.events] == [e.event_id for e in second.events]
        assert [e.base_amount_raw for e in first.events] == [
            e.base_amount_raw for e in second.events
        ]


def test_wallet_order_does_not_change_the_result() -> None:
    """S0 9.2: the same data in a different order must produce the same result."""
    fixture = load_golden("pumpfun_ok0")
    wallets = fixture["wallets"] + ["3xWFCqaiV3LdqZbHcVa9J6CMFa9YJV5W8cJL1DyUdi7z"]
    forward = RECONCILING.decode(fixture["transaction"], wallets=wallets, observed_at_utc=T0)
    backward = RECONCILING.decode(
        fixture["transaction"], wallets=list(reversed(wallets)), observed_at_utc=T0
    )
    assert {e.event_id for e in forward.events} == {e.event_id for e in backward.events}


def test_the_observation_time_does_not_change_the_event_id() -> None:
    fixture = load_golden("pumpfun_ok0")
    early = RECONCILING.decode(
        fixture["transaction"], wallets=fixture["wallets"], observed_at_utc=T0
    )
    late = RECONCILING.decode(
        fixture["transaction"],
        wallets=fixture["wallets"],
        observed_at_utc=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert [e.event_id for e in early.events] == [e.event_id for e in late.events]


# -- sphere accounting ------------------------------------------------------------------------------
def test_sphere_accounting_isolates_the_swap_from_fees_and_rent() -> None:
    """The wallet's own lamport delta is not the swap amount; the corrected flow is."""
    fixture = load_golden("pumpfun_ok0")
    transaction = fixture["transaction"]
    wallet = fixture["wallets"][0]
    keys = decoder_account_keys(transaction)
    accounts = decoder_token_accounts(transaction, keys)

    raw_delta = int(transaction["meta"]["postBalances"][0]) - int(
        transaction["meta"]["preBalances"][0]
    )
    flow = sol_sphere_flow(transaction, keys, accounts, wallet)

    assert flow is not None
    assert flow != raw_delta, "the correction terms must actually do something"
    # This wallet sold, so it received SOL.
    assert flow > 0


def test_token_balance_deltas_see_what_the_wallet_actually_gained() -> None:
    fixture = load_golden("pumpfun_ok0")
    deltas = token_balance_deltas(fixture["transaction"], fixture["wallets"][0])
    assert deltas, "a swap must move at least one token balance"
    assert all(value != 0 for value in deltas.values())


# -- registry -----------------------------------------------------------------------------------------
def test_every_venue_program_maps_to_a_contract_enum_value() -> None:
    for program_id, venue in VENUE_PROGRAMS.items():
        assert isinstance(venue, Venue)
        assert venue is not Venue.UNKNOWN, f"{program_id} must map to a real venue"


def test_swap_programs_and_infrastructure_do_not_overlap() -> None:
    assert not (SWAP_PROGRAMS & INFRASTRUCTURE_PROGRAMS)


def test_golden_cases_have_the_expected_layout() -> None:
    """S0 6: every case carries its raw payload, its expectation and an explanation."""
    root = Path(__file__).resolve().parents[2] / "contracts" / "golden"
    cases = sorted(path for path in root.iterdir() if path.is_dir())
    assert cases, "no golden cases were built; run scripts/build_golden_cases.py"
    for case in cases:
        assert (case / "raw_transaction.json").exists(), case.name
        assert (case / "expected_events.json").exists(), case.name
        assert (case / "README.md").exists(), case.name


def test_golden_expectations_still_hold() -> None:
    """The reconciliation test both plans run (S0 6). A drift here breaks I2."""
    root = Path(__file__).resolve().parents[2] / "contracts" / "golden"
    volatile = {"observed_at_utc", "source", "source_provider"}
    for case in sorted(path for path in root.iterdir() if path.is_dir()):
        fixture = json.loads((case / "raw_transaction.json").read_text(encoding="utf-8"))
        expected = json.loads((case / "expected_events.json").read_text(encoding="utf-8"))

        result = CONTRACTUAL.decode(
            fixture["transaction"],
            wallets=expected["wallets"],
            observed_at_utc=datetime(2026, 9, 6, tzinfo=UTC),
        )
        produced = [
            {k: v for k, v in event.to_wire().items() if k not in volatile}
            for event in result.events
        ]
        assert produced == expected["expected_events"], f"{case.name} drifted"
        assert list(result.quarantined_wallets) == expected["expected_quarantined_wallets"]
        assert result.wash_same_tx == expected["expected_wash_same_tx"]
