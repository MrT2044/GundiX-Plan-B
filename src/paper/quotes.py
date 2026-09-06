"""Quote providers (02_PLAN_B, `QuoteProvider`).

A quote is an offer, not a fill. Everything here therefore records what was asked, what
came back and when it expires - and never treats a quote as a promise.

Two implementations:

* :class:`SimulatedQuoteProvider` - deterministic constant-product pricing around a
  reference price. Used in PAPER, where the point is to exercise the decision path, not to
  predict a price. It is honest about being a model: its output carries the pool size it
  assumed, so a later calibration against real quotes is possible.
* :class:`JupiterQuoteProvider` - a real HTTP quote. **The endpoint and its response shape
  must be verified against the current official API before this is used for anything that
  informs a decision** (02_PLAN_B B8). Until an integration test has run against the live
  service, treat its output as unverified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import httpx
from gundix_contracts.mints import LAMPORTS_PER_SOL
from gundix_contracts.models import QuoteSnapshot

from src.common.clock import Clock
from src.common.config import QuoteConfig
from src.common.logging import get_logger, log_event

logger = get_logger(__name__)


class QuoteError(RuntimeError):
    """The provider could not produce a usable quote."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    in_mint: str
    out_mint: str
    in_amount_raw: int
    in_decimals: int
    out_decimals: int
    slippage_bps: int
    #: Price of one whole *out* token in whole *in* tokens, as observed on the source trade.
    #: The simulated provider prices around it; a real provider ignores it.
    reference_price: Decimal | None = None
    #: Quote-side pool liquidity in raw units, when the caller knows it.
    pool_quote_liquidity_raw: int | None = None


@runtime_checkable
class QuoteProvider(Protocol):
    name: str

    def quote(self, request: QuoteRequest) -> QuoteSnapshot:
        """Return a quote or raise :class:`QuoteError`. Never returns a guess."""


class SimulatedQuoteProvider:
    """Constant-product pricing around a reference price. Deterministic, no network.

    The model: a pool holding ``L`` quote units and ``L / price`` base units. Swapping
    ``dx`` in yields ``dy = y * dx / (x + dx)``, which reproduces the two properties that
    actually matter for a copy trader - larger orders get worse prices, and the effect grows
    non-linearly as the order approaches pool size.

    This is a model, not a prediction. It exists so the decision path can be exercised end
    to end before real quotes are available, and so paper fills can later be compared
    against shadow quotes to see how wrong the model was.
    """

    name = "simulated"
    model_version = "1.0.0"

    def __init__(self, config: QuoteConfig, *, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    def quote(self, request: QuoteRequest) -> QuoteSnapshot:
        if request.in_amount_raw <= 0:
            raise QuoteError("quote requested for a non-positive amount", code="PROVIDER_ERROR")
        if request.reference_price is None or request.reference_price <= 0:
            raise QuoteError(
                "the simulated provider needs a reference price from the source trade",
                code="NO_ROUTE",
            )

        pool_quote = request.pool_quote_liquidity_raw or int(
            self._config.simulated_pool_quote_liquidity_sol * LAMPORTS_PER_SOL
        )
        if pool_quote <= 0:
            raise QuoteError("no pool liquidity to price against", code="INSUFFICIENT_LIQUIDITY")

        in_amount = Decimal(request.in_amount_raw)
        price = request.reference_price  # out per in, in whole-token terms

        scale = Decimal(10) ** (request.out_decimals - request.in_decimals)
        ideal_out = in_amount * price * scale

        # Constant product around the assumed pool. `x` is the reserve of the input asset.
        x = Decimal(pool_quote)
        y = x * price * scale
        actual_out = (y * in_amount) / (x + in_amount)

        if ideal_out <= 0 or actual_out <= 0:
            raise QuoteError("model produced a non-positive output", code="INSUFFICIENT_LIQUIDITY")

        fee = Decimal(self._config.simulated_fee_bps) / Decimal(10_000)
        actual_out *= Decimal(1) - fee

        impact_bps = int(
            ((ideal_out - actual_out) / ideal_out * Decimal(10_000)).to_integral_value()
        )
        out_raw = int(actual_out.to_integral_value(rounding="ROUND_DOWN"))
        if out_raw <= 0:
            raise QuoteError("output rounds to zero at this size", code="INSUFFICIENT_LIQUIDITY")

        now = self._clock.now()
        return QuoteSnapshot(
            provider=self.name,
            requested_at_utc=now,
            expires_at_utc=now + timedelta(seconds=self._config.quote_ttl_seconds),
            in_amount_raw=request.in_amount_raw,
            out_amount_raw=out_raw,
            price_impact_bps=max(0, impact_bps),
            route_hops=1,
            platform_fee_raw=int(
                (Decimal(request.in_amount_raw) * fee).to_integral_value(rounding="ROUND_DOWN")
            ),
        )


class JupiterQuoteProvider:
    """Real quotes over HTTP.

    NOT VERIFIED against the live API in this build. 02_PLAN_B B8 requires the current
    official API, its cost structure, authentication and terms of use to be checked before
    an execution path commits to a provider. Until an integration test has run against the
    live service, this class must not inform a decision that matters.
    """

    name = "jupiter"
    default_base_url = "https://quote-api.jup.ag/v6"

    def __init__(
        self,
        config: QuoteConfig,
        *,
        clock: Clock,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._base_url = (config.base_url or self.default_base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=config.http_timeout_seconds)
        self._verified = False

    @property
    def is_verified(self) -> bool:
        """True only after an integration test confirmed the live response shape."""
        return self._verified

    def quote(self, request: QuoteRequest) -> QuoteSnapshot:
        params = {
            "inputMint": request.in_mint,
            "outputMint": request.out_mint,
            "amount": str(request.in_amount_raw),
            "slippageBps": str(request.slippage_bps),
        }
        try:
            response = self._client.get(f"{self._base_url}/quote", params=params)
        except httpx.TimeoutException as exc:
            raise QuoteError(f"quote request timed out: {exc}", code="PROVIDER_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise QuoteError(f"quote request failed: {exc}", code="PROVIDER_ERROR") from exc

        if response.status_code == 404:
            raise QuoteError("no route for this pair", code="NO_ROUTE")
        if response.status_code >= 400:
            raise QuoteError(
                f"quote provider returned HTTP {response.status_code}", code="PROVIDER_ERROR"
            )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise QuoteError("quote response was not JSON", code="PROVIDER_ERROR") from exc

        return self._parse(payload, request)

    def _parse(self, payload: dict[str, Any], request: QuoteRequest) -> QuoteSnapshot:
        out_amount = payload.get("outAmount")
        in_amount = payload.get("inAmount")
        if out_amount is None or in_amount is None:
            raise QuoteError(
                "quote response is missing inAmount/outAmount; the API shape must be "
                "re-verified before this provider is used",
                code="PROVIDER_ERROR",
            )
        route = payload.get("routePlan") or []
        impact_raw = payload.get("priceImpactPct") or "0"
        try:
            impact_bps = int(
                (Decimal(str(impact_raw)).copy_abs() * Decimal(10_000)).to_integral_value()
            )
        except (ValueError, ArithmeticError):
            impact_bps = 0

        now = self._clock.now()
        log_event(
            logger,
            10,
            "quote received",
            provider=self.name,
            in_mint=request.in_mint,
            out_mint=request.out_mint,
            in_amount_raw=str(request.in_amount_raw),
            out_amount_raw=str(out_amount),
            hops=len(route) or 1,
        )
        return QuoteSnapshot(
            provider=self.name,
            requested_at_utc=now,
            expires_at_utc=now + timedelta(seconds=self._config.quote_ttl_seconds),
            in_amount_raw=int(in_amount),
            out_amount_raw=int(out_amount),
            price_impact_bps=max(0, impact_bps),
            route_hops=max(1, len(route)),
            platform_fee_raw=int((payload.get("platformFee") or {}).get("amount") or 0),
        )

    def close(self) -> None:
        self._client.close()


def build_quote_provider(config: QuoteConfig, *, clock: Clock) -> QuoteProvider:
    if config.provider == "simulated":
        return SimulatedQuoteProvider(config, clock=clock)
    if config.provider == "jupiter":
        return JupiterQuoteProvider(config, clock=clock)
    raise ValueError(f"unknown quote provider {config.provider!r}")


def min_out_amount(quote: QuoteSnapshot, slippage_bps: int) -> int:
    """The floor committed to before execution. Truncated down, never rounded up."""
    return int(
        (
            Decimal(quote.out_amount_raw)
            * (Decimal(10_000) - Decimal(slippage_bps))
            / Decimal(10_000)
        ).to_integral_value(rounding="ROUND_DOWN")
    )


def quote_is_stale(quote: QuoteSnapshot, now: datetime) -> bool:
    return now >= quote.expires_at_utc


__all__ = [
    "JupiterQuoteProvider",
    "QuoteError",
    "QuoteProvider",
    "QuoteRequest",
    "SimulatedQuoteProvider",
    "build_quote_provider",
    "min_out_amount",
    "quote_is_stale",
]
