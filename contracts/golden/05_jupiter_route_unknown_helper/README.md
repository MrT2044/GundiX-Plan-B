# 05_jupiter_route_unknown_helper

Jupiter v6 route containing a helper program that is not in the registry. Under S0 rule 9 the whole transaction is quarantined - see CCR-001.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:56Z.

- signature: `NR9qSPg16McFVVoYLrLriGP7S3Ei8nXfhnYsMXfWE13JwQfcj7USFSU8qPuxnYCYpm3vqCjUqJP8TpJ1peHVoLA`
- slot: 444874758
- observed wallet: `6VAD25T6dSQTXhNeXVWHXq7AKnFrQrGc5gsBE5sNQBT5`
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
