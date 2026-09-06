"""Execution adapter interface (02_PLAN_B B8).

Whether GundiX later routes through Jupiter, a Pump-specific service or builds transactions
directly is an adapter decision, not an architectural one. The interface below is what any
of them has to satisfy.

Explicitly open, and not to be closed by guessing: 02_PLAN_B B8 requires the current
official API, its cost structure, authentication and terms of use to be verified before an
implementation commits to a provider. No provider name is hardcoded anywhere in this build.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from gundix_contracts.models import CopyIntent, QuoteSnapshot


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    """A built but unsigned transaction, plus everything needed to check it locally."""

    payload_base64: str
    expected_fee_payer: str
    min_out_amount_raw: int
    expires_at_slot: int | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    succeeded: bool
    consumed_compute_units: int | None
    error: str | None
    logs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    signature: str | None
    accepted: bool
    error: str | None


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Capabilities a live execution path must provide, in the order they are used."""

    name: str

    def quote(self, intent: CopyIntent) -> QuoteSnapshot: ...

    def build(self, intent: CopyIntent, quote: QuoteSnapshot) -> UnsignedTransaction: ...

    def validate_locally(self, transaction: UnsignedTransaction, intent: CopyIntent) -> None:
        """Raise if the built transaction does not match the intent.

        This is the last point at which a provider bug or a swapped mint is catchable
        without spending money, so it never trusts the builder's own claims.
        """

    def simulate(self, transaction: UnsignedTransaction) -> SimulationOutcome: ...

    def submit(self, signed_transaction: bytes) -> SubmitOutcome: ...

    def confirm(self, signature: str, *, timeout_seconds: float) -> str:
        """Track to a terminal state. A timeout is not a failure - it is an unknown."""

    def reconcile(self, signature: str) -> str:
        """Ask the chain what actually happened to a signature."""


__all__ = [
    "ExecutionAdapter",
    "SimulationOutcome",
    "SubmitOutcome",
    "UnsignedTransaction",
]
