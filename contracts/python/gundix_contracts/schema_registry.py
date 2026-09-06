"""JSON Schema is the source of truth; this module is how Python reaches it.

Nothing in GundiX parses a document that crossed a process boundary without running it
through :func:`validate_document` first (S0 section 10.2: "Keine Interpretation von JSON
ohne Schema-Validierung").
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

#: contract name -> schema file name
SCHEMA_FILES: dict[str, str] = {
    "common": "common.schema.json",
    "swap_event": "swap_event.schema.json",
    "trader_selection": "trader_selection.schema.json",
    "copy_intent": "copy_intent.schema.json",
    "execution_result": "execution_result.schema.json",
    "artifact_manifest": "artifact_manifest.schema.json",
    "latency_observation": "latency_observation.schema.json",
    "decoder_coverage": "decoder_coverage.schema.json",
}


class SchemaValidationError(ValueError):
    """A document does not satisfy its contract."""

    def __init__(self, contract: str, errors: list[str]) -> None:
        self.contract = contract
        self.errors = errors
        super().__init__(f"{contract} failed schema validation: {'; '.join(errors)}")


@cache
def load_schema(contract: str) -> dict[str, Any]:
    try:
        file_name = SCHEMA_FILES[contract]
    except KeyError:
        raise KeyError(f"unknown contract {contract!r}; known: {sorted(SCHEMA_FILES)}") from None
    schema: dict[str, Any] = json.loads((SCHEMA_DIR / file_name).read_text(encoding="utf-8"))
    return schema


@cache
def _registry() -> Registry:
    registry: Registry = Registry()
    for contract, file_name in SCHEMA_FILES.items():
        schema = load_schema(contract)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        # Registered under the relative file name (how the schemas $ref each other) and
        # under the absolute $id.
        registry = registry.with_resource(uri=file_name, resource=resource)
        if "$id" in schema:
            registry = registry.with_resource(uri=schema["$id"], resource=resource)
    return registry


@cache
def get_validator(contract: str) -> Draft202012Validator:
    return Draft202012Validator(
        load_schema(contract), registry=_registry(), format_checker=FormatChecker()
    )


def validate_document(contract: str, payload: Any) -> None:
    """Raise :class:`SchemaValidationError` if ``payload`` violates the contract."""
    errors = sorted(
        get_validator(contract).iter_errors(payload), key=lambda e: list(e.absolute_path)
    )
    if errors:
        rendered = [
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:10]
        ]
        raise SchemaValidationError(contract, rendered)


def is_valid(contract: str, payload: Any) -> bool:
    try:
        validate_document(contract, payload)
    except SchemaValidationError:
        return False
    return True


def schema_enum(contract: str, pointer: str) -> list[str]:
    """Read an ``enum`` out of a schema by JSON pointer, for the enum-parity tests."""
    node: Any = load_schema(contract)
    for part in pointer.strip("/").split("/"):
        if not part:
            continue
        node = node[part]
    if not isinstance(node, dict) or "enum" not in node:
        raise KeyError(f"{contract}{pointer} does not hold an enum")
    return [value for value in node["enum"] if value is not None]
