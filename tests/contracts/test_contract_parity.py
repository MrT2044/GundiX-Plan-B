"""The Pydantic models and the JSON Schemas must not drift apart.

S0 section 4: "Quelle der Wahrheit ist das JSON Schema. Die Pydantic-Modelle werden dagegen
getestet - ein Test schlaegt fehl, sobald Modell und Schema divergieren."

Three checks, because they fail for different reasons:

1. every model's serialised form validates against its schema,
2. the field sets are identical in both directions - a field only in the model would never
   reach Plan A, a field only in the schema would never be produced,
3. every enum has the same members in both places.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from gundix_contracts.enums import (
    ArtifactType,
    Decision,
    DecodeConfidence,
    DedupPolicy,
    EventSource,
    ExecutionErrorCode,
    ExecutionStatus,
    Finality,
    NoTradeReason,
    OperatingMode,
    RouterLabel,
    Side,
    Venue,
    WalletStatus,
)
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    ContractVersionError,
    check_schema_version,
)
from gundix_contracts.schema_registry import (
    SCHEMA_FILES,
    load_schema,
    schema_enum,
    validate_document,
)

from tests.factories import T0, make_selection, make_swap_event


def _model_fields(model_cls) -> set[str]:
    return set(model_cls.model_fields)


def _schema_properties(contract: str) -> set[str]:
    return set(load_schema(contract)["properties"])


def test_swap_event_validates_against_its_schema() -> None:
    validate_document("swap_event", make_swap_event().to_wire())


def test_trader_selection_validates_against_its_schema() -> None:
    validate_document("trader_selection", make_selection().to_wire())


@pytest.mark.parametrize(
    ("contract", "model_path"),
    [
        ("swap_event", "SwapEvent"),
        ("trader_selection", "TraderSelection"),
        ("copy_intent", "CopyIntent"),
        ("execution_result", "ExecutionResult"),
        ("artifact_manifest", "ArtifactManifest"),
        ("latency_observation", "LatencyObservation"),
        ("decoder_coverage", "DecoderCoverage"),
    ],
)
def test_model_and_schema_have_identical_field_sets(contract: str, model_path: str) -> None:
    import gundix_contracts.models as models

    model_cls = getattr(models, model_path)
    model = _model_fields(model_cls)
    schema = _schema_properties(contract)
    assert model == schema, (
        f"{contract}: only in model {sorted(model - schema)}, only in schema {sorted(schema - model)}"
    )


@pytest.mark.parametrize(
    ("enum_cls", "contract", "pointer"),
    [
        (Venue, "common", "/$defs/venue"),
        (RouterLabel, "common", "/$defs/router_label"),
        (Side, "common", "/$defs/side"),
        (Finality, "common", "/$defs/finality"),
        (OperatingMode, "common", "/$defs/operating_mode"),
        (DecodeConfidence, "common", "/$defs/decode_confidence"),
        (EventSource, "common", "/$defs/event_source"),
        (ArtifactType, "artifact_manifest", "/properties/artifact_type"),
        (ExecutionStatus, "execution_result", "/properties/status"),
        (WalletStatus, "trader_selection", "/$defs/selected_wallet/properties/status"),
        (Decision, "copy_intent", "/properties/decision"),
        (DedupPolicy, "copy_intent", "/properties/dedup_policy"),
    ],
)
def test_enums_match_schema(enum_cls, contract: str, pointer: str) -> None:
    assert {member.value for member in enum_cls} == set(schema_enum(contract, pointer))


def test_no_trade_reason_enum_matches_schema() -> None:
    schema = load_schema("copy_intent")["properties"]["reason_code"]["oneOf"][0]["enum"]
    assert {member.value for member in NoTradeReason} == set(schema)


def test_execution_error_code_enum_matches_schema() -> None:
    schema = load_schema("execution_result")["properties"]["error_code"]["oneOf"][0]["enum"]
    assert {member.value for member in ExecutionErrorCode} == set(schema)


def test_every_schema_forbids_unknown_fields() -> None:
    """S0 section 4: an unknown field is an error, not a tolerance."""
    for contract in SCHEMA_FILES:
        schema = load_schema(contract)
        if "properties" not in schema:
            continue  # common.schema.json only holds $defs
        assert schema.get("additionalProperties") is False, f"{contract} tolerates unknown fields"


def test_unknown_major_version_is_rejected() -> None:
    with pytest.raises(ContractVersionError):
        check_schema_version("swap_event", "2.0.0")


def test_newer_minor_version_is_accepted_with_a_warning() -> None:
    warnings = check_schema_version("swap_event", "1.9.0")
    assert warnings and "minor" in warnings[0]


def test_current_versions_produce_no_warning() -> None:
    for contract, version in SCHEMA_VERSIONS.items():
        if contract == "artifact_manifest":
            continue
        assert check_schema_version(contract, version) == []


def test_raw_amounts_serialise_as_strings_not_numbers() -> None:
    """JSON numbers lose precision above 2^53; a u64 token amount reaches that in practice."""
    event = make_swap_event(base_amount_raw=18_000_000_000_000_000_000)
    wire = event.to_wire()
    assert isinstance(wire["base_amount_raw"], str)
    assert wire["base_amount_raw"] == "18000000000000000000"


def test_floats_are_rejected_for_money_like_values() -> None:
    from gundix_contracts.models import SelectedWallet

    with pytest.raises(ValueError, match="floats are forbidden"):
        SelectedWallet.model_validate(
            {
                "wallet": "3xWFCqaiV3LdqZbHcVa9J6CMFa9YJV5W8cJL1DyUdi7z",
                "status": "ACTIVE",
                "rank": 1,
                "score": 1.5,
                "weight": "0.5",
                "max_weight": "1.0",
                "reason_codes": [],
                "risks": [],
                "coverage_ratio": "0.9",
                "metrics_ref": None,
                "copy_pnl_by_latency": {"1s": "0", "3s": "0", "5s": "0", "15s": "0", "60s": "0"},
            }
        )


def test_naive_datetimes_are_rejected() -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="naive datetimes"):
        make_swap_event(block_time=datetime(2026, 9, 1, 12, 0, 0))  # noqa: DTZ001


def test_block_time_serialises_to_second_precision() -> None:
    """The chain gives seconds; inventing milliseconds would be inventing a measurement."""
    event = make_swap_event(block_time=T0 + timedelta(microseconds=123456))
    assert event.to_wire()["block_time_utc"] == "2026-09-01T12:00:00Z"


def test_observation_time_serialises_to_millisecond_precision() -> None:
    event = make_swap_event(observed_at=T0 + timedelta(microseconds=123456))
    assert event.to_wire()["observed_at_utc"] == "2026-09-01T12:00:00.123Z"
