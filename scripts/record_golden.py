"""Record real mainnet transactions as golden fixtures.

Run with a public or private RPC endpoint:

    python scripts/record_golden.py --limit 40

Every fixture is a real, unmodified ``getTransaction`` response plus a small header
describing where it came from. Fixtures are what makes integration point I2 possible:
Plan A's historical parser and Plan B's live decoder must produce identical SwapEvents
for exactly these payloads.

Nothing here is used at runtime.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "contracts" / "golden" / "transactions"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

PROBE_PROGRAMS: dict[str, str] = {
    "pumpfun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pumpswap": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "raydium_amm_v4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "raydium_launchlab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    "jupiter_v6": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}


class Rpc:
    def __init__(self, url: str, *, min_interval: float = 0.3) -> None:
        self.url = url
        self.min_interval = min_interval
        self._client = httpx.Client(timeout=30)
        self._last = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(5):
            response = self._client.post(
                self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            self._last = time.monotonic()
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            payload = response.json()
            if "error" in payload:
                raise RuntimeError(f"{method} failed: {payload['error']}")
            return payload["result"]
        raise RuntimeError(f"{method} kept hitting the rate limit")

    def close(self) -> None:
        self._client.close()


def fetch_transaction(rpc: Rpc, signature: str) -> dict[str, Any] | None:
    return rpc.call(
        "getTransaction",
        [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed",
            },
        ],
    )


def fee_payer(transaction: dict[str, Any]) -> str | None:
    keys = transaction.get("transaction", {}).get("message", {}).get("accountKeys") or []
    if not keys:
        return None
    first = keys[0]
    return first if isinstance(first, str) else str(first.get("pubkey"))


def write_fixture(
    *,
    case: str,
    description: str,
    wallets: list[str],
    transaction: dict[str, Any],
    rpc_url: str,
) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{case}.json"
    payload = {
        "case": case,
        "description": description,
        "wallets": wallets,
        "recorded_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rpc_url": rpc_url,
        "encoding": "jsonParsed",
        "transaction": transaction,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=DEFAULT_RPC)
    parser.add_argument("--limit", type=int, default=6, help="signatures probed per program")
    parser.add_argument(
        "--signature", action="append", default=[], help="record a specific signature"
    )
    parser.add_argument("--case-prefix", default="")
    args = parser.parse_args()

    rpc = Rpc(args.rpc)
    written: list[Path] = []
    try:
        for signature in args.signature:
            transaction = fetch_transaction(rpc, signature)
            if transaction is None:
                print(f"  {signature}: not found")
                continue
            wallet = fee_payer(transaction)
            path = write_fixture(
                case=f"{args.case_prefix}{signature[:16]}",
                description="manually requested signature",
                wallets=[wallet] if wallet else [],
                transaction=transaction,
                rpc_url=args.rpc,
            )
            written.append(path)
            print(f"  wrote {path.name}")

        for label, program_id in PROBE_PROGRAMS.items():
            print(f"probing {label} ...")
            try:
                signatures = rpc.call(
                    "getSignaturesForAddress",
                    [program_id, {"limit": args.limit, "commitment": "confirmed"}],
                )
            except RuntimeError as exc:
                print(f"  skipped: {exc}")
                continue

            ok_count = 0
            fail_count = 0
            for entry in signatures:
                signature = entry["signature"]
                failed = entry.get("err") is not None
                # Keep a couple of successful cases and exactly one failure per venue.
                if failed and fail_count >= 1:
                    continue
                if not failed and ok_count >= 2:
                    continue
                try:
                    transaction = fetch_transaction(rpc, signature)
                except RuntimeError as exc:
                    print(f"  {signature[:12]}: {exc}")
                    continue
                if transaction is None:
                    continue
                wallet = fee_payer(transaction)
                suffix = "failed" if failed else f"ok{ok_count}"
                path = write_fixture(
                    case=f"{label}_{suffix}",
                    description=f"recent {label} transaction ({'failed' if failed else 'successful'})",
                    wallets=[wallet] if wallet else [],
                    transaction=transaction,
                    rpc_url=args.rpc,
                )
                written.append(path)
                print(f"  wrote {path.name} ({signature[:16]}...)")
                if failed:
                    fail_count += 1
                else:
                    ok_count += 1
    finally:
        rpc.close()

    print(f"\n{len(written)} fixtures in {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
