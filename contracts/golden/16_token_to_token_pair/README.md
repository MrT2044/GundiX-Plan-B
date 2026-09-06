# 16_token_to_token_pair

Raydium LaunchLab swap whose counter-asset is neither SOL nor a stablecoin. Tests the token-to-token tie-break of S0 4.1.10.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:55Z.

- signature: `2zGXL78YvvvuijdH26ZQm91t6Vcv5sVszJi68wo9cMYZktpeaXEQ38efHufA7bk2686tZ716mckjzh4fWFYVJYym`
- slot: 444874723
- observed wallet: `CJYNMqDZcDwDshCSRgBmtDJmgzdvE5AejmvP918w51iL`
- transaction succeeded: True

## Expectation

0 SwapEvent(s), 1 quarantined wallet(s),
0 netted-to-zero pair(s).

Generated with the contractual decoder settings
(`UnknownProgramPolicy.QUARANTINE_TRANSACTION`). `observed_at_utc`, `source` and
`source_provider` are excluded from the comparison: they differ between Plan A's backfill
and Plan B's live stream by design.

## Quarantine reasons

- `unresolved_pair`
