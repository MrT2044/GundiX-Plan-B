# 04_jupiter_multi_venue_route

Jupiter v6 route whose legs cross more than one venue. The S0 venue enum has no value for this, so the contractual expectation is quarantine - see CCR-003.

## Source

Real Solana mainnet transaction, recorded from the public RPC on
2026-09-06T19:41:56Z.

- signature: `4ix6B7y9Lfi7kPhxm2arQztdux5R9gyrrnSUsc9Bcbzw6abtrM9ocboMUznF5ny9i8YnafLZmuT8YcantnJzSStd`
- slot: 444874758
- observed wallet: `Fqczgf9KfSVXtMtXccq6SE1yBfWeUXefJ3ithStptTUa`
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
