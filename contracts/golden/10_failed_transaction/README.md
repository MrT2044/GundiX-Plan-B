# 10_failed_transaction

On-chain failure. Nothing executed, so no SwapEvent may be produced at all.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:41Z.

- signature: `54UX1tmrSe6471jvu5p6q1DDJo1bffWuDSFcDG87gMj49bW5kLP1jR1QGv7hKbf6eFn8T6EfoE4hKseozeP2afzD`
- slot: 444874712
- observed wallet: `FHpcNSe6tb2n15bAdq4BkeYWGyZKFD7yLYrH92ng7wCT`
- transaction succeeded: False

## Expectation

0 SwapEvent(s), 0 quarantined wallet(s),
0 netted-to-zero pair(s).

Generated with the contractual decoder settings
(`UnknownProgramPolicy.QUARANTINE_TRANSACTION`). `observed_at_utc`, `source` and
`source_provider` are excluded from the comparison: they differ between Plan A's backfill
and Plan B's live stream by design.
