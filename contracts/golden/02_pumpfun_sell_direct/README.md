# 02_pumpfun_sell_direct

Pump.fun bonding-curve SELL of a Token-2022 mint. Same as case 01 in the other direction; the venue fee is inside the wallet's net SOL flow.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:41Z.

- signature: `s6752qE4eY9xCQRofKnNmygktexDFRwYBvbHp3ujSuwmzCSRcpxrUZTNQDWUEne4rZEMn4Fy4RfUgnZG5hWehKw`
- slot: 444874712
- observed wallet: `3xWFCqaiV3LdqZbHcVa9J6CMFa9YJV5W8cJL1DyUdi7z`
- transaction succeeded: True

## Expectation

0 SwapEvent(s), 1 quarantined wallet(s),
0 netted-to-zero pair(s).

Generated with the contractual decoder settings
(`UnknownProgramPolicy.QUARANTINE_TRANSACTION`). `observed_at_utc`, `source` and
`source_provider` are excluded from the comparison: they differ between Plan A's backfill
and Plan B's live stream by design.

## Quarantine reasons

- `unknown_program_in_path`
