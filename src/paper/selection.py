"""Selection import and hot reload (02_PLAN_B B1, S0 4.4).

The selection is the only thing that tells Plan B which wallets to copy. Plan B never
decides that itself. Everything here therefore optimises for one property: **a bad
selection must not be able to become the active one.**

Loading is atomic in the sense that matters operationally: the new selection is fully
validated into an immutable snapshot before anything is swapped, and if any step fails the
previously active selection stays exactly as it was. A reload never leaves the process with
half a watchlist.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gundix_contracts.artifacts import ArtifactError, load_json_artifact
from gundix_contracts.enums import ArtifactType, OperatingMode, WalletStatus
from gundix_contracts.models import SelectedWallet, TraderSelection
from gundix_contracts.schema_registry import SchemaValidationError

from src.common.clock import Clock
from src.common.logging import get_logger, log_event

logger = get_logger(__name__)


class SelectionRejectedError(RuntimeError):
    """The selection is not usable. It never becomes active."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail}")


@dataclass(frozen=True, slots=True)
class ActiveSelection:
    """An immutable, validated snapshot plus the index the hot path needs."""

    selection: TraderSelection
    content_sha256: str
    source_path: Path
    loaded_at_utc: datetime
    by_wallet: dict[str, SelectedWallet]

    @property
    def selection_id(self) -> str:
        return self.selection.selection_id

    @property
    def approved(self) -> bool:
        return self.selection.manual_approval.approved

    def is_expired_at(self, now: datetime) -> bool:
        return now >= self.selection.expires_at_utc

    def wallet(self, address: str) -> SelectedWallet | None:
        return self.by_wallet.get(address)

    def is_tradeable_wallet(self, address: str) -> bool:
        entry = self.by_wallet.get(address)
        return entry is not None and entry.status.may_trade

    @property
    def active_wallet_count(self) -> int:
        return sum(1 for entry in self.by_wallet.values() if entry.status.may_trade)


def validate_selection(
    selection: TraderSelection, *, now: datetime, require_approval: bool
) -> None:
    """The eight rejection rules of S0 4.4, applied fail-closed.

    Rules 1 (major version), 5 (duplicate wallet), 6 (invalid base58) and 7 (weight bounds)
    are already enforced by the schema and the model, and are re-checked here only where
    the model cannot see the whole picture.
    """
    if require_approval and not selection.manual_approval.approved:
        raise SelectionRejectedError(
            "rule_2_not_approved",
            f"selection {selection.selection_id} carries manual_approval.approved=false",
        )
    if now >= selection.expires_at_utc:
        raise SelectionRejectedError(
            "rule_3_expired",
            f"selection {selection.selection_id} expired at {selection.expires_at_utc.isoformat()}",
        )
    if not selection.code_commit or not selection.params_hash:
        raise SelectionRejectedError(
            "rule_8_missing_provenance",
            "code_commit and params_hash must both be present",
        )
    # Rule 7's second half: the model checks weight <= max_weight per wallet and the sum
    # over all wallets; nothing left to do here beyond making the intent explicit.


class SelectionRepository:
    """Holds the active selection and swaps it atomically.

    Thread-safe by construction: the active snapshot is a single immutable object behind a
    lock, so a reader either sees the old selection or the new one, never a mixture.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock,
        require_approval: bool = True,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._require_approval = require_approval
        self._lock = threading.Lock()
        self._active: ActiveSelection | None = None
        self._last_error: str | None = None
        self._reload_count = 0
        self._rejected_count = 0

    # -- reading -----------------------------------------------------------------------
    @property
    def active(self) -> ActiveSelection | None:
        with self._lock:
            return self._active

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def stats(self) -> dict[str, int | str | None]:
        with self._lock:
            return {
                "reloads": self._reload_count,
                "rejected": self._rejected_count,
                "active_selection_id": self._active.selection_id if self._active else None,
                "active_wallets": self._active.active_wallet_count if self._active else 0,
                "last_error": self._last_error,
            }

    # -- loading -----------------------------------------------------------------------
    def load(self) -> ActiveSelection:
        """Load and activate. Raises without touching the active selection on any failure."""
        candidate = self._read_and_validate()
        with self._lock:
            previous = self._active
            self._active = candidate
            self._last_error = None
            self._reload_count += 1
        log_event(
            logger,
            20,
            "selection activated",
            selection_id=candidate.selection_id,
            wallets=len(candidate.by_wallet),
            active_wallets=candidate.active_wallet_count,
            approved=candidate.approved,
            expires_at_utc=candidate.selection.expires_at_utc.isoformat(),
            content_sha256=candidate.content_sha256,
            replaced=previous.selection_id if previous else None,
        )
        return candidate

    def reload_if_changed(self) -> bool:
        """Reload when the file changed. Keeps the last valid selection on any error.

        Returns True when a new selection became active.
        """
        try:
            candidate = self._read_and_validate()
        except (SelectionRejectedError, ArtifactError, SchemaValidationError, OSError) as exc:
            with self._lock:
                self._last_error = str(exc)
                self._rejected_count += 1
                kept = self._active.selection_id if self._active else None
            log_event(
                logger,
                40,
                "selection reload rejected, keeping the last valid selection",
                error=str(exc),
                kept_selection_id=kept,
                path=str(self._path),
            )
            return False

        with self._lock:
            unchanged = (
                self._active is not None and self._active.content_sha256 == candidate.content_sha256
            )
            if unchanged:
                self._last_error = None
                return False
            previous = self._active
            self._active = candidate
            self._last_error = None
            self._reload_count += 1

        log_event(
            logger,
            20,
            "selection hot-reloaded",
            selection_id=candidate.selection_id,
            previous_selection_id=previous.selection_id if previous else None,
            active_wallets=candidate.active_wallet_count,
        )
        return True

    def _read_and_validate(self) -> ActiveSelection:
        loaded = load_json_artifact(self._path, expected_type=ArtifactType.TRADER_SELECTION)
        selection = TraderSelection.model_validate(loaded.records[0])
        now = self._clock.now()
        validate_selection(selection, now=now, require_approval=self._require_approval)
        for warning in loaded.warnings:
            log_event(logger, 30, "selection schema warning", warning=warning)
        return ActiveSelection(
            selection=selection,
            content_sha256=loaded.manifest.content_sha256,
            source_path=self._path,
            loaded_at_utc=now,
            by_wallet={entry.wallet: entry for entry in selection.wallets},
        )


def selection_blocks_execution(
    active: ActiveSelection | None, *, now: datetime, mode: OperatingMode
) -> str | None:
    """Reason why no intent may be produced right now, or ``None`` if execution is allowed.

    An empty but valid selection is deliberately not an error: it allows observation and
    simply produces no intents (S0 4.4).
    """
    if not mode.creates_intents:
        return "mode_disallows_execution"
    if active is None:
        return "selection_invalid"
    if not active.approved:
        return "selection_invalid"
    if active.is_expired_at(now):
        return "selection_expired"
    return None


__all__ = [
    "ActiveSelection",
    "SelectionRejectedError",
    "SelectionRepository",
    "WalletStatus",
    "selection_blocks_execution",
    "validate_selection",
]
