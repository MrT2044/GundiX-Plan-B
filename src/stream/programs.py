"""On-chain program registry.

02_PLAN_B B3: work by actual on-chain programs, not by frontends. A wallet trading through
Photon, BullX or the Pump.fun website ends up in the same handful of programs.

Every id below was verified against Solana mainnet on 2026-09-06 via ``getAccountInfo``
(executable account owned by a BPF loader). The Pump.fun ids additionally match the
official pump-public-docs IDL as recorded in the local xSniper engine
(``engine/crates/pumpfun/src/constants.rs``, verified there on 2026-08-28).

Being listed here is not the same as being supported: a program must map to a value of the
``venue`` enum in S0 4.1.5 before its swaps can be economically usable.
"""

from __future__ import annotations

from gundix_contracts.enums import RouterLabel, Venue

SYSTEM_PROGRAM = "11111111111111111111111111111111"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO_PROGRAM_V1 = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"
MEMO_PROGRAM_V2 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
PUMP_FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

#: Programs that move value or account state without being a swap venue. Seeing one of
#: these does not reduce decoder coverage.
INFRASTRUCTURE_PROGRAMS: frozenset[str] = frozenset(
    {
        SYSTEM_PROGRAM,
        SPL_TOKEN_PROGRAM,
        TOKEN_2022_PROGRAM,
        ASSOCIATED_TOKEN_PROGRAM,
        MEMO_PROGRAM_V1,
        MEMO_PROGRAM_V2,
        COMPUTE_BUDGET_PROGRAM,
        # Pump.fun's fee program. It only routes the protocol/creator fee that is already
        # part of the wallet's net SOL flow, so it changes no amount we compute.
        PUMP_FEE_PROGRAM,
    }
)

#: Programs that map onto a value of the S0 4.1.5 venue enum.
VENUE_PROGRAMS: dict[str, Venue] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": Venue.PUMP_FUN,
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": Venue.PUMPSWAP,
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": Venue.RAYDIUM_AMM_V4,
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": Venue.RAYDIUM_CPMM,
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": Venue.RAYDIUM_CLMM,
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": Venue.RAYDIUM_LAUNCHLAB,
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": Venue.METEORA_DLMM,
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": Venue.METEORA_DAMM,
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": Venue.ORCA_WHIRLPOOL,
}

#: Real swap venues that exist on chain but have no value in the S0 venue enum yet.
#: They are recognised (so they do not count as unknown programs) but their swaps get
#: ``venue = UNKNOWN`` and are therefore never economically usable. Extending the enum is
#: a CCR - see CCR-003.
VENUE_PROGRAMS_WITHOUT_ENUM_VALUE: dict[str, str] = {
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "METEORA_DBC",
    "2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c": "LIFINITY_V2",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "PHOENIX",
    "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb": "OPENBOOK_V2",
}

ROUTER_PROGRAMS: dict[str, RouterLabel] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": RouterLabel.JUPITER_V6,
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": RouterLabel.JUPITER_V4,
}

#: Any program that carries out or wraps a swap. Used to find the swap instructions.
SWAP_PROGRAMS: frozenset[str] = frozenset(
    set(VENUE_PROGRAMS) | set(VENUE_PROGRAMS_WITHOUT_ENUM_VALUE) | set(ROUTER_PROGRAMS)
)


def is_known_program(program_id: str) -> bool:
    """False means: we cannot say what this program did to the wallet's balances."""
    return program_id in INFRASTRUCTURE_PROGRAMS or program_id in SWAP_PROGRAMS


def is_token_program(program_id: str) -> bool:
    return program_id in {SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM}


def venue_for(program_id: str) -> Venue | None:
    """The enum venue, or ``Venue.UNKNOWN`` for a recognised venue without an enum value."""
    if program_id in VENUE_PROGRAMS:
        return VENUE_PROGRAMS[program_id]
    if program_id in VENUE_PROGRAMS_WITHOUT_ENUM_VALUE:
        return Venue.UNKNOWN
    return None
