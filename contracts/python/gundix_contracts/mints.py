"""Quote-mint vocabulary. Normative source: S0 section 4.1.10."""

from __future__ import annotations

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000

#: Ascending rank. The token with the LOWEST rank present in a pair becomes quote_mint.
QUOTE_MINT_PRIORITY: tuple[str, ...] = (WSOL_MINT, USDC_MINT, USDT_MINT)

KNOWN_DECIMALS: dict[str, int] = {WSOL_MINT: 9, USDC_MINT: 6, USDT_MINT: 6}


def choose_quote_mint(mint_a: str, mint_b: str) -> str:
    """Decide the quote side of a pair deterministically (S0 4.1.10).

    Both plans call this. The tie-break for token-to-token pairs - the lexicographically
    smaller base58 address wins - is part of the contract, not an implementation detail.
    """
    if mint_a == mint_b:
        raise ValueError("a swap pair cannot have the same mint on both sides")
    rank_a = QUOTE_MINT_PRIORITY.index(mint_a) if mint_a in QUOTE_MINT_PRIORITY else None
    rank_b = QUOTE_MINT_PRIORITY.index(mint_b) if mint_b in QUOTE_MINT_PRIORITY else None
    if rank_a is not None and rank_b is not None:
        return mint_a if rank_a < rank_b else mint_b
    if rank_a is not None:
        return mint_a
    if rank_b is not None:
        return mint_b
    return min(mint_a, mint_b)


def is_token_to_token(base_mint: str, quote_mint: str) -> bool:
    """True when neither side is one of the recognised quote assets."""
    return base_mint not in QUOTE_MINT_PRIORITY and quote_mint not in QUOTE_MINT_PRIORITY
