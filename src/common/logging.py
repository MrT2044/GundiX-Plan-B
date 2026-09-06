"""Structured logging with secret redaction.

02_PLAN_B section 6: "Logs und Fehler enthalten keine Secrets". Redaction happens at the
formatter, so it also covers exception payloads and third-party log records that we do
not control.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

REDACTED = "***redacted***"

#: Field names whose values never appear in a log line, at any nesting depth.
SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|apikey|authorization|auth[_-]?token|access[_-]?token|bearer|"
    r"private[_-]?key|secret|keypair|passphrase|password|mnemonic|seed[_-]?phrase|"
    r"signer|x-api-key)",
    re.IGNORECASE,
)

#: ``?api-key=...`` / ``&token=...`` inside URLs.
_URL_SECRET_RE = re.compile(
    r"([?&](?:api[-_]?key|apikey|token|access[-_]?token|key)=)[^&\s\"']+",
    re.IGNORECASE,
)

correlation_id: ContextVar[str | None] = ContextVar("gundix_correlation_id", default=None)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively strip secrets out of a log payload."""
    if _depth > 8:
        return "<max depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and SECRET_KEY_PATTERN.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _URL_SECRET_RE.sub(r"\1" + REDACTED, value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        cid = correlation_id.get()
        if cid:
            payload["correlation_id"] = cid
        extra = getattr(record, "gundix", None)
        if isinstance(extra, dict):
            payload["data"] = redact(extra)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({"level": "ERROR", "message": "log serialisation failed"})


def configure_logging(level: int = logging.INFO, *, stream: Any = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, /, **fields: Any) -> None:
    """Emit one structured record. ``fields`` is redacted before it reaches the log."""
    logger.log(level, message, extra={"gundix": fields})
