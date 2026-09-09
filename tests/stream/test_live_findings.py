"""Two things a live run against mainnet taught us, kept as tests so they stay true.

Both were found on 2026-09-09 by running the RPC-polling source in OBSERVE mode against
the public endpoint for four minutes and looking at what came back. Neither was visible
from recorded fixtures, because both are properties of *streams*, not of transactions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gundix_contracts.enums import EventSource

from src.stream.decoder import wallets_that_traded
from src.stream.pipeline import block_time_of
from src.stream.sources import RawChainEvent
from tests.factories import MINT_X, WALLET_A, WALLET_B, make_signature
from tests.integration import tx_builder


# --------------------------------------------------------------------------------------
# Finding 1: the fee payer is frequently not the trader
# --------------------------------------------------------------------------------------
def test_the_trader_is_identified_by_balance_change_not_by_paying_the_fee() -> None:
    """A relayer sponsors the fee; the trader is whoever's balance moved.

    Live measurement: across 40 consecutive transactions of one high-frequency address, it
    was the fee payer in 40 and the trader in 0. The same 40 transactions contained 14
    wallets that had actually traded.
    """
    # WALLET_B trades; the payload's fee payer is WALLET_B too in this builder, so we
    # rewrite the fee payer to a sponsor that touches no token balance.
    payload = tx_builder.pumpfun_buy(
        signature=make_signature(31),
        wallet=WALLET_B,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()),
    )
    keys = payload["transaction"]["message"]["accountKeys"]
    keys.insert(0, {"pubkey": WALLET_A, "signer": True, "writable": True, "source": "transaction"})
    payload["meta"]["preBalances"].insert(0, 10_000_000_000)
    payload["meta"]["postBalances"].insert(0, 10_000_000_000 - tx_builder.DEFAULT_FEE)
    for bucket in ("preTokenBalances", "postTokenBalances"):
        for entry in payload["meta"][bucket]:
            entry["accountIndex"] += 1

    traders = wallets_that_traded(payload)

    assert WALLET_B in traders, "the wallet whose token balance moved is the trader"
    assert WALLET_A not in traders, "the fee payer sponsored the trade, it did not make it"


def test_a_transaction_where_nobody_local_traded_yields_no_trader() -> None:
    """Honest emptiness: the function never falls back to naming the fee payer."""
    payload = tx_builder.pumpfun_buy(
        signature=make_signature(32),
        wallet=WALLET_B,
        mint=MINT_X,
        sol_in=1,
        tokens_out=1,
        slot=1,
        block_time=int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()),
    )
    # Freeze every balance: nothing moved for anyone.
    payload["meta"]["postTokenBalances"] = payload["meta"]["preTokenBalances"]

    assert wallets_that_traded(payload) == ()


def test_the_trader_list_has_no_duplicates() -> None:
    payload = tx_builder.pumpfun_buy(
        signature=make_signature(33),
        wallet=WALLET_B,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()),
    )
    traders = wallets_that_traded(payload)
    assert len(traders) == len(set(traders))


# --------------------------------------------------------------------------------------
# Finding 2: feed lag is a measurement, and it can grow without bound
# --------------------------------------------------------------------------------------
def test_block_time_is_read_from_the_payload() -> None:
    moment = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    payload = tx_builder.pumpfun_buy(
        signature=make_signature(34),
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=1_000,
        tokens_out=1_000,
        slot=1,
        block_time=int(moment.timestamp()),
    )
    assert block_time_of(payload) == moment
    assert block_time_of(None) is None
    assert block_time_of({"blockTime": None}) is None


def test_feed_lag_is_measured_not_assumed(scenario_pipeline) -> None:
    """A poller falling behind must be visible as a number, not as a vague sense of delay.

    Against the public RPC the median lag was 51 s in the first minute and 183 s afterwards
    - it grew, because the endpoint could not be polled fast enough to keep up with a
    high-frequency wallet. Without this measurement that failure is invisible.
    """
    app, clock = scenario_pipeline
    block_time = clock.now() - timedelta(seconds=90)
    payload = tx_builder.pumpfun_buy(
        signature=make_signature(35),
        wallet=WALLET_A,
        mint=MINT_X,
        sol_in=50_000_000,
        tokens_out=1_000_000_000,
        slot=1,
        block_time=int(block_time.timestamp()),
    )

    app.pipeline.process(
        RawChainEvent(
            signature=payload["transaction"]["signatures"][0],
            slot=1,
            received_at_utc=clock.now(),
            source=EventSource.LIVE_STREAM,
            payload=payload,
            wallet_hint=WALLET_A,
        )
    )

    assert app.pipeline.last_feed_lag_seconds is not None
    assert 89 <= app.pipeline.last_feed_lag_seconds <= 91
    assert app.pipeline.max_feed_lag_seconds >= app.pipeline.last_feed_lag_seconds


def test_the_stale_feed_breaker_trips_on_a_lagging_feed(scenario_pipeline, session) -> None:
    """The breaker existed but nothing fed it; the live run is what exposed that."""
    app, clock = scenario_pipeline

    quiet = app.risk.evaluate_circuit_breakers(
        session,
        now=clock.now(),
        feed_age_seconds=5.0,
        decoder_coverage=None,
        observed_slippage_bps=None,
    )
    assert not any("stale_feed" in item for item in quiet)

    tripped = app.risk.evaluate_circuit_breakers(
        session,
        now=clock.now(),
        feed_age_seconds=183.0,
        decoder_coverage=None,
        observed_slippage_bps=None,
    )
    assert any("stale_feed" in item for item in tripped)
