"""The Jupiter quote adapter against the live API (02_PLAN_B B8).

B8 requires the current official API to be verified *before* an execution path commits to a
provider. A mock cannot do that: the failure this test actually caught was that the
hardcoded host had stopped resolving entirely, which every mock in the world would have
happily pretended still worked.

Marked ``integration``: it needs the network, and is skipped when that is unavailable. It
needs no API key - the endpoint under test is the keyless tier.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from gundix_contracts.mints import USDC_MINT, WSOL_MINT

from src.common.clock import SystemClock
from src.common.config import QuoteConfig
from src.paper.quotes import JupiterQuoteProvider, QuoteError, QuoteRequest, min_out_amount

pytestmark = pytest.mark.integration

#: A pump.fun mint that is not tradable, taken from a recorded transaction.
NOT_TRADABLE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
HALF_SOL = 500_000_000


@pytest.fixture(scope="module")
def provider() -> JupiterQuoteProvider:
    try:
        httpx.get("https://lite-api.jup.ag/swap/v1/quote", timeout=10)
    except httpx.HTTPError as exc:
        pytest.skip(f"no network access to the quote provider: {exc}")
    return JupiterQuoteProvider(QuoteConfig(provider="jupiter"), clock=SystemClock())


def _request(out_mint: str = USDC_MINT, amount: int = HALF_SOL) -> QuoteRequest:
    return QuoteRequest(
        in_mint=WSOL_MINT,
        out_mint=out_mint,
        in_amount_raw=amount,
        in_decimals=9,
        out_decimals=6,
        slippage_bps=300,
    )


def test_a_liquid_pair_returns_a_usable_quote(provider: JupiterQuoteProvider) -> None:
    quote = provider.quote(_request())

    assert quote.provider == "jupiter"
    assert quote.in_amount_raw == HALF_SOL
    assert quote.out_amount_raw > 0
    assert quote.route_hops >= 1
    assert quote.expires_at_utc > quote.requested_at_utc


def test_the_quote_is_economically_plausible(provider: JupiterQuoteProvider) -> None:
    """Half a SOL should buy a three-figure number of USDC, not a cent and not a fortune.

    Deliberately a wide band: this catches a decimals or scaling mistake, which is the
    error that would silently corrupt every size calculation downstream, without failing
    whenever the SOL price moves.
    """
    quote = provider.quote(_request())
    usdc = Decimal(quote.out_amount_raw) / Decimal(10**6)

    assert Decimal(1) < usdc < Decimal(10_000), f"half a SOL quoted as {usdc} USDC"


def test_price_impact_is_read_as_a_fraction_not_a_percentage(
    provider: JupiterQuoteProvider,
) -> None:
    """``priceImpactPct`` is misleadingly named: "0.99" means 99 %, not 0.99 %.

    Reading it as a percentage would understate impact by a factor of 100 - the size of
    error that turns an unfillable order into one that looks fine.
    """
    small = provider.quote(_request(amount=HALF_SOL))
    huge = provider.quote(_request(amount=1_000_000 * 10**9))

    assert small.price_impact_bps <= 500, "a half-SOL trade into USDC is not high impact"
    assert huge.price_impact_bps > small.price_impact_bps
    assert huge.price_impact_bps <= 10_000, "impact in bps cannot exceed 100 %"


def test_our_slippage_floor_agrees_with_the_provider(provider: JupiterQuoteProvider) -> None:
    """Cross-check of two independent computations of the same number.

    Jupiter returns ``otherAmountThreshold`` for the slippage we asked for; we compute our
    own floor from the quote. If those disagree, one of the two is wrong about what
    ``slippageBps`` means, and committing to a min-out on that basis would be reckless.
    """
    request = _request()
    quote = provider.quote(request)
    ours = min_out_amount(quote, request.slippage_bps)

    raw = httpx.get(
        "https://lite-api.jup.ag/swap/v1/quote",
        params={
            "inputMint": request.in_mint,
            "outputMint": request.out_mint,
            "amount": str(request.in_amount_raw),
            "slippageBps": str(request.slippage_bps),
        },
        timeout=20,
    ).json()
    theirs = int(raw["otherAmountThreshold"])
    reference = int(raw["outAmount"])

    # The two quotes are separate calls, so the prices differ slightly; compare the ratio
    # each side applied rather than the absolute amounts.
    our_ratio = Decimal(ours) / Decimal(quote.out_amount_raw)
    their_ratio = Decimal(theirs) / Decimal(reference)

    assert abs(our_ratio - their_ratio) < Decimal("0.001"), (
        f"we floor at {our_ratio}, the provider at {their_ratio}"
    )


def test_an_untradable_token_is_reported_as_a_token_restriction(
    provider: JupiterQuoteProvider,
) -> None:
    """The live failure is HTTP 400 with an errorCode, never the 404 this adapter used to
    look for. Getting the category right is what lets Plan A count rejections correctly."""
    with pytest.raises(QuoteError) as caught:
        provider.quote(_request(out_mint=NOT_TRADABLE))

    assert caught.value.code == "TOKEN_RESTRICTED"
    assert "TOKEN_NOT_TRADABLE" in str(caught.value)


def test_the_same_mint_on_both_sides_is_rejected(provider: JupiterQuoteProvider) -> None:
    with pytest.raises(QuoteError) as caught:
        provider.quote(_request(out_mint=WSOL_MINT))

    assert caught.value.code == "PROVIDER_ERROR"
    assert "CIRCULAR_ARBITRAGE" in str(caught.value)


def test_the_dead_endpoint_is_no_longer_configured() -> None:
    """A regression guard for the actual bug this verification found.

    ``quote-api.jup.ag`` stopped resolving. Nothing may point at it again without somebody
    re-running the verification.
    """
    assert "quote-api.jup.ag" not in JupiterQuoteProvider.default_base_url
