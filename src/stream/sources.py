"""Chain event sources (02_PLAN_B B2).

The domain never talks to a provider directly. It talks to :class:`ChainEventSource`, and
which implementation is behind it - RPC polling, a websocket, a webhook, a recorded file -
changes nothing downstream.

Two implementations exist here:

* :class:`ReplaySource` - recorded transactions, deterministic, no network. Everything
  downstream can be tested with real payloads.
* :class:`RpcPollingSource` - ``getSignaturesForAddress`` per watched wallet against a plain
  HTTP RPC. It works on the public endpoint, which is what is actually available. The target
  population holds positions for hours to days, so robust second-level latency matters more
  than premature sub-second infrastructure (B2).

A websocket source is deliberately **not** implemented here. ``logsSubscribe`` is not usable
on the public endpoint, so the code could only ever be tested against a mock - and a mock
test is not evidence that it works (S0 9.3). It is a small module to add once a provider
endpoint exists; the interface below is what it has to satisfy.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from gundix_contracts.enums import EventSource, Finality

from src.common.clock import Clock
from src.common.config import StreamConfig
from src.common.logging import get_logger, log_event

logger = get_logger(__name__)


class SourceError(RuntimeError):
    """The source could not deliver. Never silently swallowed."""


@dataclass(frozen=True, slots=True)
class RawChainEvent:
    """One transaction as it arrived, before anything was interpreted."""

    signature: str
    slot: int
    received_at_utc: datetime
    source: EventSource
    payload: dict[str, Any] | None
    wallet_hint: str | None = None
    failed_on_chain: bool = False


@dataclass(slots=True)
class SourceHealth:
    connected: bool = True
    last_event_at_utc: datetime | None = None
    last_slot: int = 0
    events_seen: int = 0
    errors: int = 0
    reconnects: int = 0
    gaps_detected: int = 0
    backfilled: int = 0
    detail: str = ""

    def feed_age_seconds(self, now: datetime) -> float | None:
        if self.last_event_at_utc is None:
            return None
        return (now - self.last_event_at_utc).total_seconds()


@runtime_checkable
class ChainEventSource(Protocol):
    name: str
    source_kind: EventSource

    def set_watchlist(self, wallets: Sequence[str]) -> None: ...

    def poll(self) -> Iterator[RawChainEvent]:
        """Yield everything new since the last call. Never blocks indefinitely."""

    def fetch_transaction(self, signature: str) -> dict[str, Any] | None:
        """Fetch one transaction by signature, for gap recovery."""

    def health(self) -> SourceHealth: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------------
class ReplaySource:
    """Recorded transactions from disk. Deterministic and offline.

    Fixture layout: either ``scripts/record_golden.py`` output (a ``transaction`` key plus a
    ``wallets`` list) or a golden case directory containing ``raw_transaction.json``.
    """

    name = "replay"
    source_kind = EventSource.REPLAY

    def __init__(self, path: Path, *, clock: Clock) -> None:
        self._path = Path(path)
        self._clock = clock
        self._health = SourceHealth()
        self._watchlist: tuple[str, ...] = ()
        self._pending: list[RawChainEvent] = []
        self._by_signature: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def set_watchlist(self, wallets: Sequence[str]) -> None:
        self._watchlist = tuple(wallets)

    def _load(self) -> None:
        import json

        if self._loaded:
            return
        files: list[Path] = []
        if self._path.is_dir():
            files = sorted(self._path.glob("*.json")) + sorted(
                self._path.glob("*/raw_transaction.json")
            )
        elif self._path.exists():
            files = [self._path]

        for file in files:
            try:
                fixture = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._health.errors += 1
                log_event(logger, 40, "replay fixture unreadable", file=str(file), error=str(exc))
                continue
            transaction = fixture.get("transaction", fixture)
            meta = transaction.get("meta") or {}
            signatures = transaction.get("transaction", {}).get("signatures") or []
            if not signatures:
                continue
            signature = str(signatures[0])
            self._by_signature[signature] = transaction
            hint_list = fixture.get("wallets") or []
            self._pending.append(
                RawChainEvent(
                    signature=signature,
                    slot=int(transaction.get("slot", 0)),
                    received_at_utc=self._clock.now(),
                    source=self.source_kind,
                    payload=transaction,
                    wallet_hint=str(hint_list[0]) if hint_list else None,
                    failed_on_chain=meta.get("err") is not None,
                )
            )
        self._pending.sort(key=lambda event: (event.slot, event.signature))
        self._loaded = True

    def poll(self) -> Iterator[RawChainEvent]:
        self._load()
        while self._pending:
            event = self._pending.pop(0)
            self._health.events_seen += 1
            self._health.last_slot = max(self._health.last_slot, event.slot)
            self._health.last_event_at_utc = event.received_at_utc
            yield event

    def fetch_transaction(self, signature: str) -> dict[str, Any] | None:
        self._load()
        return self._by_signature.get(signature)

    def health(self) -> SourceHealth:
        return self._health

    def close(self) -> None:
        self._pending.clear()


# --------------------------------------------------------------------------------------
# RPC polling
# --------------------------------------------------------------------------------------
class RpcClient:
    """Minimal JSON-RPC client with a rate limit and bounded retries.

    The public endpoint rate limits aggressively, so the limiter is not optional politeness:
    without it the source spends its time being refused.
    """

    def __init__(
        self,
        url: str,
        *,
        max_requests_per_second: float = 4.0,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._min_interval = 1.0 / max_requests_per_second if max_requests_per_second > 0 else 0.0
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._last_call = 0.0

    def call(self, method: str, params: list[Any], *, attempts: int = 4) -> Any:
        last_error: str = ""
        for attempt in range(attempts):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self._client.post(
                    self._url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            finally:
                self._last_call = time.monotonic()

            if response.status_code == 429:
                last_error = "rate limited"
                time.sleep(min(8.0, 1.0 * (2**attempt)))
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            if response.status_code >= 400:
                raise SourceError(f"{method} failed with HTTP {response.status_code}")
            payload = response.json()
            if "error" in payload:
                raise SourceError(f"{method} failed: {payload['error']}")
            return payload.get("result")
        raise SourceError(f"{method} failed after {attempts} attempts: {last_error}")

    def close(self) -> None:
        self._client.close()


@dataclass(slots=True)
class _WalletCursor:
    """Where we got to for one wallet. Persisted, so a restart does not re-read history."""

    last_signature: str | None = None
    last_slot: int = 0
    backlog: list[str] = field(default_factory=list)


class RpcPollingSource:
    """Polls ``getSignaturesForAddress`` per watched wallet.

    Gap handling is inherent rather than bolted on: the cursor is the last signature we
    fully processed, and every poll asks the RPC for everything *after* it. A process that
    was down for an hour therefore receives the hour it missed, bounded by
    ``max_signature_backlog_per_wallet`` so a very long outage cannot produce an unbounded
    burst - the overflow is reported as a gap rather than silently dropped.
    """

    name = "rpc_polling"
    source_kind = EventSource.LIVE_STREAM

    def __init__(
        self,
        config: StreamConfig,
        *,
        clock: Clock,
        client: RpcClient | None = None,
        cursors: dict[str, _WalletCursor] | None = None,
    ) -> None:
        if not config.rpc_http_url:
            raise SourceError("rpc_polling needs stream.rpc_http_url")
        self._config = config
        self._clock = clock
        self._rpc = client or RpcClient(
            config.rpc_http_url, max_requests_per_second=config.rpc_max_requests_per_second
        )
        self._cursors: dict[str, _WalletCursor] = cursors or {}
        self._watchlist: tuple[str, ...] = ()
        self._health = SourceHealth(connected=True)

    def set_watchlist(self, wallets: Sequence[str]) -> None:
        self._watchlist = tuple(dict.fromkeys(wallets))
        for wallet in self._watchlist:
            self._cursors.setdefault(wallet, _WalletCursor())

    @property
    def cursors(self) -> dict[str, _WalletCursor]:
        return self._cursors

    def restore_cursors(self, state: dict[str, dict[str, Any]]) -> None:
        for wallet, values in state.items():
            self._cursors[wallet] = _WalletCursor(
                last_signature=values.get("last_signature"),
                last_slot=int(values.get("last_slot", 0)),
                backlog=list(values.get("backlog", [])),
            )

    def export_cursors(self) -> dict[str, dict[str, Any]]:
        return {
            wallet: {
                "last_signature": cursor.last_signature,
                "last_slot": cursor.last_slot,
                "backlog": cursor.backlog,
            }
            for wallet, cursor in self._cursors.items()
        }

    def poll(self) -> Iterator[RawChainEvent]:
        commitment = self._config.commitment.value.lower()
        for wallet in self._watchlist:
            cursor = self._cursors.setdefault(wallet, _WalletCursor())
            try:
                entries = self._signatures_since(wallet, cursor, commitment)
            except SourceError as exc:
                self._health.errors += 1
                self._health.detail = str(exc)
                log_event(logger, 40, "signature poll failed", wallet=wallet, error=str(exc))
                continue

            for entry in entries:
                signature = entry["signature"]
                try:
                    transaction = self.fetch_transaction(signature)
                except SourceError as exc:
                    # Keep it in the backlog: an unfetched transaction is a gap, and a gap
                    # that is forgotten is worse than one that is retried.
                    self._health.errors += 1
                    cursor.backlog.append(signature)
                    log_event(
                        logger, 40, "transaction fetch failed", signature=signature, error=str(exc)
                    )
                    continue
                if transaction is None:
                    continue

                now = self._clock.now()
                slot = int(transaction.get("slot", entry.get("slot", 0)))
                cursor.last_signature = signature
                cursor.last_slot = max(cursor.last_slot, slot)
                self._health.events_seen += 1
                self._health.last_slot = max(self._health.last_slot, slot)
                self._health.last_event_at_utc = now
                yield RawChainEvent(
                    signature=signature,
                    slot=slot,
                    received_at_utc=now,
                    source=self.source_kind,
                    payload=transaction,
                    wallet_hint=wallet,
                    failed_on_chain=(transaction.get("meta") or {}).get("err") is not None,
                )

    def _signatures_since(
        self, wallet: str, cursor: _WalletCursor, commitment: str
    ) -> list[dict[str, Any]]:
        """Everything after the cursor, oldest first."""
        limit = min(1000, self._config.max_signature_backlog_per_wallet)
        params: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if cursor.last_signature:
            params["until"] = cursor.last_signature

        result = self._rpc.call("getSignaturesForAddress", [wallet, params]) or []
        if len(result) >= limit and cursor.last_signature:
            # The RPC hit the limit, so there is more between the cursor and now than one
            # page. That is a real gap: report it instead of pretending the page was all.
            self._health.gaps_detected += 1
            log_event(
                logger,
                30,
                "signature backlog exceeded one page; older transactions were not fetched",
                wallet=wallet,
                limit=limit,
                since=cursor.last_signature,
            )
        # The RPC returns newest first; process oldest first so the cursor only ever moves
        # forward and a crash mid-batch re-reads rather than skips.
        return list(reversed(result))

    def fetch_transaction(self, signature: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self._rpc.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": self._config.commitment.value.lower(),
                },
            ],
        )
        return result

    def recover_backlog(self) -> Iterator[RawChainEvent]:
        """Retry signatures that failed to fetch earlier."""
        for wallet, cursor in self._cursors.items():
            pending, cursor.backlog = cursor.backlog, []
            for signature in pending:
                try:
                    transaction = self.fetch_transaction(signature)
                except SourceError:
                    cursor.backlog.append(signature)
                    continue
                if transaction is None:
                    continue
                self._health.backfilled += 1
                yield RawChainEvent(
                    signature=signature,
                    slot=int(transaction.get("slot", 0)),
                    received_at_utc=self._clock.now(),
                    source=EventSource.HISTORICAL_BACKFILL,
                    payload=transaction,
                    wallet_hint=wallet,
                    failed_on_chain=(transaction.get("meta") or {}).get("err") is not None,
                )

    def health(self) -> SourceHealth:
        return self._health

    def close(self) -> None:
        self._rpc.close()


def build_source(config: StreamConfig, *, clock: Clock) -> ChainEventSource:
    if config.source == "replay":
        if config.replay_path is None:
            raise SourceError("stream.replay_path is required for the replay source")
        return ReplaySource(config.replay_path, clock=clock)
    if config.source == "rpc_polling":
        return RpcPollingSource(config, clock=clock)
    if config.source == "websocket":
        raise SourceError(
            "the websocket source is not implemented in this build. logsSubscribe is not "
            "usable on the public endpoint, so it could only be tested against a mock, and a "
            "mock test is not evidence that it works. Use stream.source=rpc_polling until a "
            "provider endpoint is available."
        )
    raise SourceError(f"unknown stream source {config.source!r}")


__all__ = [
    "ChainEventSource",
    "Finality",
    "RawChainEvent",
    "ReplaySource",
    "RpcClient",
    "RpcPollingSource",
    "SourceError",
    "SourceHealth",
    "build_source",
]
