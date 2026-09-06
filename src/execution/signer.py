"""Transaction signing. Import-guarded.

S0 8.3 requires that the signer module is **not even importable** in PAPER and SHADOW.
That is enforced here, at import time, rather than by a runtime check somewhere inside a
function: a check inside a function only fires if that function is called, whereas an
import guard fires the moment anything reaches for this module at all.

The key itself lives outside the repository. Nothing in this file reads, prints, logs or
serialises key material, and no test fixture contains one.
"""

from __future__ import annotations

import os

from gundix_contracts.enums import OperatingMode

_MODE = (os.environ.get("GUNDIX_MODE") or "").strip().upper()

if OperatingMode.LIVE.value != _MODE:
    raise ImportError(
        "src.execution.signer must not be imported outside LIVE. "
        f"GUNDIX_MODE is {_MODE or '<unset>'}. Paper and shadow execution are technically "
        "separated from signing (S0 8.3); if you reached this line in PAPER or SHADOW, "
        "something is wired wrong and must be fixed rather than worked around."
    )


class SignerNotImplementedError(RuntimeError):
    """Live signing is not built in this milestone."""


class Signer:
    """Placeholder for the isolated signer.

    Deliberately not implemented. Building a signer before the Go/No-Go criteria of
    00_GESAMTPLAN section 11 are met would create a component whose only remaining barrier
    to moving real money is a configuration value.

    When it is built it must: live in a separate process or at minimum a separate module
    boundary with no imports from research or paper code; load the keypair from a path
    outside the repository; never place key material in an exception, a log record, a
    metric label or an artifact; and refuse to sign a transaction whose fee payer is not
    the expected live wallet.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise SignerNotImplementedError(
            "no signer is implemented. Live execution stays blocked until the Go/No-Go "
            "criteria in 00_GESAMTPLAN section 11 are demonstrably met."
        )


__all__ = ["Signer", "SignerNotImplementedError"]
