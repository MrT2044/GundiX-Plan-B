"""Independent verification of the decoder against Pump.fun's own on-chain event.

This is the strongest check we have short of a second implementation: the Pump.fun program
emits an Anchor ``TradeEvent`` into the transaction log containing the mint, the SOL amount
and the token amount it computed itself. That number comes from the program, not from our
parsing, so agreeing with it is real evidence rather than the decoder confirming itself.

Expected relationship (verified on recorded mainnet transactions):

* token amount - identical to the lamport,
* SOL amount   - the decoder reports the wallet's **net** SOL flow, which is the program's
  pre-fee curve amount minus the venue fee on a sell and plus the venue fee on a buy.

The test asserts the direction and a tolerance band rather than a hardcoded fee rate, so a
protocol fee change shows up as a failure to investigate, not as a silent drift.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

import base58
import pytest
from gundix_contracts.enums import Side

from src.stream.decoder import SwapDecoder, UnknownProgramPolicy
from src.stream.programs import VENUE_PROGRAMS
from tests.conftest import T0, golden_names, load_golden

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

#: Pump.fun's venue fee, as observed on chain. Used only to size the tolerance band.
MAX_VENUE_FEE_RATE = 0.03


@dataclass(frozen=True)
class TradeEvent:
    mint: str
    sol_amount: int
    token_amount: int
    is_buy: bool
    user: str


def parse_trade_events(transaction: dict[str, Any]) -> list[TradeEvent]:
    """Decode Anchor ``Program data:`` log lines into Pump.fun trade events.

    Layout: 8 byte discriminator, 32 byte mint, u64 sol_amount, u64 token_amount,
    bool is_buy, 32 byte user, ...
    """
    events: list[TradeEvent] = []
    for line in transaction.get("meta", {}).get("logMessages") or []:
        if not line.startswith("Program data:"):
            continue
        try:
            blob = base64.b64decode(line.split("Program data:", 1)[1].strip())
        except (ValueError, TypeError):
            continue
        if len(blob) < 8 + 32 + 8 + 8 + 1 + 32:
            continue
        sol_amount, token_amount = struct.unpack_from("<QQ", blob, 40)
        events.append(
            TradeEvent(
                mint=base58.b58encode(blob[8:40]).decode(),
                sol_amount=sol_amount,
                token_amount=token_amount,
                is_buy=bool(blob[56]),
                user=base58.b58encode(blob[57:89]).decode(),
            )
        )
    return events


def _pumpfun_fixtures() -> list[str]:
    names: list[str] = []
    for name in golden_names():
        fixture = load_golden(name)
        transaction = fixture["transaction"]
        if (transaction.get("meta") or {}).get("err") is not None:
            continue
        if not fixture.get("wallets"):
            continue
        programs = {
            str(ix.get("programId"))
            for group in (transaction.get("meta") or {}).get("innerInstructions") or []
            for ix in group.get("instructions") or []
        }
        programs |= {
            str(ix.get("programId"))
            for ix in transaction.get("transaction", {}).get("message", {}).get("instructions")
            or []
        }
        if PUMPFUN_PROGRAM not in programs:
            continue
        wallet = fixture["wallets"][0]
        if any(event.user == wallet for event in parse_trade_events(transaction)):
            names.append(name)
    return names


PUMPFUN_FIXTURES = _pumpfun_fixtures()


def test_pumpfun_fixtures_exist() -> None:
    """Guard against the check silently becoming a no-op if fixtures are re-recorded."""
    assert PUMPFUN_FIXTURES, (
        "no recorded Pump.fun transaction with a trade event for its fee payer; "
        "re-run scripts/record_golden.py"
    )


@pytest.mark.parametrize("name", PUMPFUN_FIXTURES)
def test_decoder_matches_pumpfun_program_event(name: str) -> None:
    fixture = load_golden(name)
    transaction = fixture["transaction"]
    wallet = fixture["wallets"][0]

    # CCR-001 policy: these real transactions are wrapped in third-party bot programs, so
    # the contractual rule 9 would quarantine them and this verification could not run at
    # all. The check is about arithmetic, so it uses the reconciling policy explicitly.
    decoder = SwapDecoder(unknown_program_policy=UnknownProgramPolicy.RECONCILE_BALANCES)
    result = decoder.decode(transaction, wallets=[wallet], observed_at_utc=T0)
    program_events = [e for e in parse_trade_events(transaction) if e.user == wallet]
    assert program_events, "fixture selection guarantees at least one matching trade event"

    decoded = [e for e in result.events if e.base_mint == program_events[0].mint]
    assert decoded, f"decoder produced no event for mint {program_events[0].mint}"
    event = decoded[0]
    expected = program_events[0]

    # The venue the transaction actually used must be recognised.
    assert VENUE_PROGRAMS[PUMPFUN_PROGRAM] == event.venue

    # Token side: exact, to the smallest unit.
    assert event.base_amount_raw == expected.token_amount

    # Direction must agree with the program's own view.
    assert (event.side is Side.BUY) == expected.is_buy

    # SOL side: the wallet's net flow. A buy pays the curve amount plus the venue fee,
    # a sell receives the curve amount minus it.
    if expected.is_buy:
        assert event.quote_amount_raw >= expected.sol_amount
    else:
        assert event.quote_amount_raw <= expected.sol_amount
    deviation = abs(event.quote_amount_raw - expected.sol_amount) / expected.sol_amount
    assert deviation <= MAX_VENUE_FEE_RATE, (
        f"{name}: decoded {event.quote_amount_raw} lamports vs program {expected.sol_amount}; "
        f"deviation {deviation:.4f} is larger than a plausible venue fee"
    )

    assert event.decode_confidence.is_usable
