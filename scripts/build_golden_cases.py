"""Turn recorded raw transactions into golden cases in the S0 section 6 layout.

    contracts/golden/<case_id>/
        raw_transaction.json   unchanged provider response, no credentials
        expected_events.json   the SwapEvents the contract says must come out
        README.md              what the case checks and why

``expected_events.json`` is generated with the **contractual** decoder settings, never with
a proposed CCR variant, so both plans reconcile against the same definition (I2).

Two fields cannot be part of a stable expectation and are stripped: ``observed_at_utc``
(wall-clock time of the observer) and ``source``/``source_provider`` (differ between
Plan A's backfill and Plan B's stream by design). Everything else must match exactly.

Run:  python scripts/build_golden_cases.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "contracts" / "python")]

from gundix_contracts.enums import EventSource, Finality  # noqa: E402

from src.stream.decoder import SwapDecoder, UnknownProgramPolicy  # noqa: E402

RAW_DIR = REPO_ROOT / "contracts" / "golden_raw"
GOLDEN_DIR = REPO_ROOT / "contracts" / "golden"

#: Fields that legitimately differ between producers and are therefore not compared.
VOLATILE_FIELDS = ("observed_at_utc", "source", "source_provider")

#: Fixed observation instant so the generated files are byte-stable across runs.
FIXED_OBSERVED_AT = datetime(2026, 9, 6, 0, 0, 0, tzinfo=UTC)

#: recorded fixture -> (case_id, description). Case ids follow the S0 6.1 numbering where
#: the recorded transaction actually is that case.
CASE_MAP: dict[str, tuple[str, str]] = {
    "raydium_amm_v4_ok0": (
        "01_pumpfun_buy_direct",
        "Pump.fun bonding-curve BUY. The SOL side exists only as a balance change, so this "
        "case pins the sphere accounting. Cross-checked against the program's own TradeEvent.",
    ),
    "pumpfun_ok0": (
        "02_pumpfun_sell_direct",
        "Pump.fun bonding-curve SELL of a Token-2022 mint. Same as case 01 in the other "
        "direction; the venue fee is inside the wallet's net SOL flow.",
    ),
    "jupiter_v6_ok0": (
        "04_jupiter_multi_venue_route",
        "Jupiter v6 route whose legs cross more than one venue. The S0 venue enum has no "
        "value for this, so the contractual expectation is quarantine - see CCR-003.",
    ),
    "jupiter_v6_ok1": (
        "05_jupiter_route_unknown_helper",
        "Jupiter v6 route containing a helper program that is not in the registry. Under "
        "S0 rule 9 the whole transaction is quarantined - see CCR-001.",
    ),
    "pumpswap_ok0": (
        "06_same_mint_buy_and_sell",
        "Buy and sell of the same mint inside one transaction (arbitrage shape). Tests the "
        "netting rule of S0 5.3 and the refusal to invent a SOL side that is not visible.",
    ),
    "pumpfun_failed": (
        "10_failed_transaction",
        "On-chain failure. Nothing executed, so no SwapEvent may be produced at all.",
    ),
    "raydium_launchlab_ok1": (
        "16_token_to_token_pair",
        "Raydium LaunchLab swap whose counter-asset is neither SOL nor a stablecoin. Tests "
        "the token-to-token tie-break of S0 4.1.10.",
    ),
}


def _strip_volatile(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in VOLATILE_FIELDS}


def build_case(raw_name: str, case_id: str, description: str) -> Path:
    fixture = json.loads((RAW_DIR / f"{raw_name}.json").read_text(encoding="utf-8"))
    transaction = fixture["transaction"]
    wallets = fixture["wallets"]

    decoder = SwapDecoder(
        source=EventSource.GOLDEN_FIXTURE,
        source_provider="fixture",
        unknown_program_policy=UnknownProgramPolicy.QUARANTINE_TRANSACTION,
    )
    result = decoder.decode(
        transaction,
        wallets=wallets,
        observed_at_utc=FIXED_OBSERVED_AT,
        finality=Finality.CONFIRMED,
    )

    case_dir = GOLDEN_DIR / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "raw_transaction.json").write_text(
        json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8"
    )

    expected = {
        "case_id": case_id,
        "wallets": wallets,
        "expected_events": [_strip_volatile(event.to_wire()) for event in result.events],
        "expected_quarantined_wallets": list(result.quarantined_wallets),
        "expected_wash_same_tx": result.wash_same_tx,
    }
    (case_dir / "expected_events.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True), encoding="utf-8"
    )

    quarantine_note = ""
    if result.quarantine_reasons:
        reasons = "\n".join(f"- `{reason.split(':')[0]}`" for reason in result.quarantine_reasons)
        quarantine_note = f"\n## Quarantine reasons\n\n{reasons}\n"

    (case_dir / "README.md").write_text(
        f"""# {case_id}

{description}

## Source

Real Solana mainnet transaction, recorded from the public RPC on
{fixture.get("recorded_at_utc", "unknown date")}.

- signature: `{result.signature}`
- slot: {result.slot}
- observed wallet: `{wallets[0] if wallets else "-"}`
- transaction succeeded: {result.success}

## Expectation

{len(result.events)} SwapEvent(s), {len(result.quarantined_wallets)} quarantined wallet(s),
{result.wash_same_tx} netted-to-zero pair(s).

Generated with the contractual decoder settings
(`UnknownProgramPolicy.QUARANTINE_TRANSACTION`). `observed_at_utc`, `source` and
`source_provider` are excluded from the comparison: they differ between Plan A's backfill
and Plan B's live stream by design.
{quarantine_note}""",
        encoding="utf-8",
    )
    return case_dir


def main() -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    built = 0
    for raw_name, (case_id, description) in CASE_MAP.items():
        if not (RAW_DIR / f"{raw_name}.json").exists():
            print(f"  missing raw fixture {raw_name}, skipped")
            continue
        path = build_case(raw_name, case_id, description)
        payload = json.loads((path / "expected_events.json").read_text(encoding="utf-8"))
        print(
            f"  {case_id}: {len(payload['expected_events'])} events, "
            f"{len(payload['expected_quarantined_wallets'])} quarantined"
        )
        built += 1
    print(f"\n{built} golden cases in {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
