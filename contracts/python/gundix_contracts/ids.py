"""Deterministic identifiers. Reference implementation for both plans.

Nobody reimplements these. If Plan A and Plan B computed ``event_id`` differently, the
whole reconciliation step I2 would be meaningless and deduplication across restarts would
be impossible.

Normative source: 03_S0_INTEGRATIONSVERTRAG section 4.1.1.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

US = "\x1f"
"""ASCII Unit Separator. Chosen because it cannot occur in a base58 address or in an
instruction path, so no field value can forge a separator."""

INTENT_ID_NAMESPACE = "gundix.intent.v1"
RESULT_ID_NAMESPACE = "gundix.result.v1"


def sha256_hex(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def compute_event_id(
    chain: str,
    signature: str,
    wallet: str,
    instruction_path: str,
    net_swap_index: int,
) -> str:
    """Identity of one net swap of one wallet inside one transaction (S0 4.1.1).

    ``transaction_index``, ``observed_at_utc``, ``source`` and ``finality`` are excluded on
    purpose: the same transaction must get the same id in a historical backfill and in the
    live stream.
    """
    if net_swap_index < 0:
        raise ValueError("net_swap_index must not be negative")
    payload = US.join([chain, signature, wallet, instruction_path, str(net_swap_index)])
    return sha256_hex(payload)


def compute_intent_id(source_event_id: str, selection_id: str, policy_version: str) -> str:
    """Idempotency key of a decision.

    Replaying the same event under the same selection and policy yields the same id, so the
    unique index on ``copy_intents`` makes a double trade structurally impossible rather
    than merely unlikely.
    """
    payload = US.join([INTENT_ID_NAMESPACE, source_event_id, selection_id, policy_version])
    return sha256_hex(payload)


def compute_result_id(intent_id: str, attempt: int) -> str:
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return sha256_hex(US.join([RESULT_ID_NAMESPACE, intent_id, str(attempt)]))


def canonical_json(payload: Any) -> bytes:
    """Byte-stable JSON used for every hash over structured data."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def config_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()
