"""Operating-mode resolution. S0 section 8.2.

The rules are deliberately blunt, because this is the one place where a convenience
default would eventually move real money:

* the mode comes from ``GUNDIX_MODE`` and from nowhere else,
* **Plan B aborts** when it is missing or invalid - unlike Plan A, which may fall back to
  RESEARCH. Plan B is the side that can execute, so "I did not understand what you asked
  for" must never resolve into "I ran anyway",
* ``LIVE`` cannot be set from a configuration file. It additionally requires
  ``GUNDIX_LIVE_CONFIRM`` to match the id of the selection that is actually loaded,
* there is no code path that *raises* a mode. An error can only lower it or stop the
  process.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from gundix_contracts.enums import OperatingMode

MODE_ENV_VAR = "GUNDIX_MODE"
LIVE_CONFIRM_ENV_VAR = "GUNDIX_LIVE_CONFIRM"
LIVE_CONFIG_ENV_VAR = "GUNDIX_LIVE_CONFIG"


class ModeError(RuntimeError):
    """The mode cannot be established safely, so the process must not start."""


def resolve_mode(env: Mapping[str, str] | None = None) -> OperatingMode:
    """Resolve Plan B's operating mode, or refuse to start."""
    source = os.environ if env is None else env
    raw = (source.get(MODE_ENV_VAR) or "").strip()

    if not raw:
        raise ModeError(
            f"{MODE_ENV_VAR} is not set. Plan B does not guess an operating mode: set it to "
            f"one of {', '.join(m.value for m in OperatingMode)}."
        )
    try:
        mode = OperatingMode(raw.upper())
    except ValueError:
        raise ModeError(
            f"{MODE_ENV_VAR}={raw!r} is not a valid mode. "
            f"Valid: {', '.join(m.value for m in OperatingMode)}. Refusing to guess."
        ) from None

    if mode is OperatingMode.LIVE and not (source.get(LIVE_CONFIG_ENV_VAR) or "").strip():
        raise ModeError(
            f"{MODE_ENV_VAR}=LIVE additionally requires {LIVE_CONFIG_ENV_VAR} to point at a "
            "separate live configuration. Refusing to start in LIVE."
        )
    return mode


def assert_live_confirmation(
    mode: OperatingMode, selection_id: str, env: Mapping[str, str] | None = None
) -> None:
    """LIVE requires ``GUNDIX_LIVE_CONFIRM`` to name the selection that is actually loaded.

    Naming the selection is the point: it means somebody looked at *this* watchlist and
    approved *this* one, not "live trading" in the abstract.
    """
    if mode is not OperatingMode.LIVE:
        return
    source = os.environ if env is None else env
    confirmed = (source.get(LIVE_CONFIRM_ENV_VAR) or "").strip()
    if not confirmed:
        raise ModeError(f"LIVE requires {LIVE_CONFIRM_ENV_VAR}=<selection_id>. Refusing to start.")
    if confirmed != selection_id:
        raise ModeError(
            f"{LIVE_CONFIRM_ENV_VAR}={confirmed!r} does not match the loaded selection "
            f"{selection_id!r}. Refusing to start."
        )


def assert_mode_allows_signer(mode: OperatingMode) -> None:
    if not mode.may_load_signer:
        raise ModeError(f"mode {mode.value} must never load a signing key")
