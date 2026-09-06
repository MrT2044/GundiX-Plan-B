# 06_same_mint_buy_and_sell

Buy and sell of the same mint inside one transaction (arbitrage shape). Tests the netting rule of S0 5.3 and the refusal to invent a SOL side that is not visible.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:42Z.

- signature: `3Xc3B8edHphw89m33N7n3LxsVAFpVrCRWuotTNoyhr4KhRjq5rMLiEQmP8ZzaBgH23azeLLkCs6beVVuUdBBgoAE`
- slot: 444874716
- observed wallet: `J33eRAWP1uAKgNkUF6KUCXEaWsne5SSYV7CcPGumCYtP`
- transaction succeeded: True

## Expectation

0 SwapEvent(s), 1 quarantined wallet(s),
0 netted-to-zero pair(s).

Generated with the contractual decoder settings
(`UnknownProgramPolicy.QUARANTINE_TRANSACTION`). `observed_at_utc`, `source` and
`source_provider` are excluded from the comparison: they differ between Plan A's backfill
and Plan B's live stream by design.

## Quarantine reasons

- `no_sol_side_visible`
- `no_sol_side_visible`
