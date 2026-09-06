"""Shared scalar types. Materialises 03_S0_INTEGRATIONSVERTRAG section 3.

The wire format and the Python format differ on purpose:

* raw amounts are ``str`` on the wire (JSON numbers lose precision above 2^53) and
  ``int`` in Python, because that is what you compute with;
* prices and ratios are ``str`` on the wire and ``Decimal`` in Python - never ``float``;
* timestamps are ISO-8601 UTC strings on the wire and aware ``datetime`` in Python, with
  two distinct precisions: block times to the second (that is all the chain gives us) and
  everything our own system observes to the millisecond.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer, WithJsonSchema

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")
_RAW_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_INSTRUCTION_PATH_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_SELECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")


# --------------------------------------------------------------------------------------
# amounts and decimals
# --------------------------------------------------------------------------------------
def _parse_raw_amount(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("a raw amount must not be a bool")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        if not _RAW_AMOUNT_RE.fullmatch(value):
            raise ValueError(f"raw amount must be a non-negative integer string, got {value!r}")
        parsed = int(value)
    else:
        raise ValueError(f"raw amount must be str or int, got {type(value).__name__}")
    if parsed < 0:
        raise ValueError("raw amounts are never negative; direction comes from `side`")
    return parsed


RawAmount = Annotated[
    int,
    BeforeValidator(_parse_raw_amount),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": "^(0|[1-9][0-9]*)$"}, mode="serialization"),
]


def _parse_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("a decimal must not be a bool")
    if isinstance(value, float):
        raise ValueError("binary floats are forbidden for money-like values; pass a string")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        if not _DECIMAL_RE.fullmatch(value):
            raise ValueError(f"not an exact decimal string: {value!r}")
        return Decimal(value)
    raise ValueError(f"decimal must be str/int/Decimal, got {type(value).__name__}")


def format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _require_non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("value must not be negative")
    return value


SignedDecimal = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal),
    PlainSerializer(format_decimal, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string"}, mode="serialization"),
]

UnsignedDecimal = Annotated[
    Decimal,
    BeforeValidator(_parse_decimal),
    AfterValidator(_require_non_negative),
    PlainSerializer(format_decimal, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string"}, mode="serialization"),
]


# --------------------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------------------
def format_utc_second(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_utc_millis(value: datetime) -> str:
    moment = _aware(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are forbidden; every timestamp is UTC-aware")
    return value.astimezone(UTC)


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        if not value.endswith("Z"):
            raise ValueError(f"timestamp must end with 'Z' (UTC): {value!r}")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise ValueError(f"timestamp must be str or datetime, got {type(value).__name__}")


def _truncate_to_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _truncate_to_millis(value: datetime) -> datetime:
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


UtcSecond = Annotated[
    datetime,
    BeforeValidator(_parse_utc),
    AfterValidator(_truncate_to_second),
    PlainSerializer(format_utc_second, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
"""Block time. Truncated to the second because the chain does not provide more."""

UtcMillis = Annotated[
    datetime,
    BeforeValidator(_parse_utc),
    AfterValidator(_truncate_to_millis),
    PlainSerializer(format_utc_millis, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
"""Anything our own system observed or decided."""


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------------------
def _base58(min_len: int, max_len: int, label: str) -> AfterValidator:
    def _check(value: str) -> str:
        if not BASE58_RE.fullmatch(value):
            raise ValueError(f"{label} is not valid base58: {value!r}")
        if not (min_len <= len(value) <= max_len):
            raise ValueError(f"{label} must be {min_len}-{max_len} characters, got {len(value)}")
        return value

    return AfterValidator(_check)


def _pattern(regex: re.Pattern[str], label: str) -> AfterValidator:
    def _check(value: str) -> str:
        if not regex.fullmatch(value):
            raise ValueError(f"{label} does not match {regex.pattern}: {value!r}")
        return value

    return AfterValidator(_check)


SolanaAddress = Annotated[str, _base58(32, 44, "solana address")]
Signature = Annotated[str, _base58(86, 88, "signature")]
SchemaVersion = Annotated[str, _pattern(_SEMVER_RE, "schema_version")]
SemVer = Annotated[str, _pattern(_SEMVER_RE, "semantic version")]
Sha256Hex = Annotated[str, _pattern(_SHA256_RE, "sha256 hex digest")]
GitSha = Annotated[str, _pattern(_GIT_SHA_RE, "git commit sha")]
InstructionPath = Annotated[str, _pattern(_INSTRUCTION_PATH_RE, "instruction_path")]
SelectionId = Annotated[str, _pattern(_SELECTION_ID_RE, "selection_id")]
Decimals = Annotated[int, Field(ge=0, le=18)]
Slot = Annotated[int, Field(ge=0)]
Bps = Annotated[int, Field(ge=0, le=100_000)]
SlippageBps = Annotated[int, Field(ge=1, le=10_000)]


def instruction_path_key(path: str) -> tuple[int, ...]:
    """Segment-wise numeric sort key (``'3.1' < '3.10' < '4'``)."""
    return tuple(int(part) for part in path.split("."))
