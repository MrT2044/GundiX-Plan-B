# 01_pumpfun_buy_direct

Pump.fun bonding-curve BUY. The SOL side exists only as a balance change, so this case pins the sphere accounting. Cross-checked against the program's own TradeEvent.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:44Z.

- signature: `31LBS3HWmFqnptqMRAe9BZXpLQLiYk9S976Br5uds8YQ2oAFnSwB71GCWD2rgSTJD7j6Wvier9GyfsMnYfGc5g78`
- slot: 444874720
- observed wallet: `7cTUhpTPbC6F4Q6gQ3jkurQ5Hz5sQ7tXRWTbq7U4n8hj`
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
