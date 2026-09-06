"""Identity and artifact handover.

``event_id`` is the hinge of the whole integration: if Plan A's backfill and Plan B's live
stream computed it differently, deduplication across restarts would be impossible and I2
would be unprovable. These tests pin the exact construction rather than merely checking
that it is stable.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from gundix_contracts.artifacts import (
    ArtifactError,
    compact_timestamp,
    load_json_artifact,
    load_jsonl_artifact,
    manifest_path_for,
    write_json_artifact,
    write_jsonl_artifact,
)
from gundix_contracts.enums import ArtifactType, EventSource, Finality
from gundix_contracts.ids import US, compute_event_id, compute_intent_id, sha256_hex

from tests.factories import T0, WALLET_A, make_selection, make_signature, make_swap_event

SIGNATURE = make_signature(1)


def test_event_id_matches_the_documented_construction() -> None:
    """S0 4.1.1, spelled out so a refactor cannot quietly change it."""
    expected = sha256_hex(US.join(["solana", SIGNATURE, WALLET_A, "3", "0"]))
    assert compute_event_id("solana", SIGNATURE, WALLET_A, "3", 0) == expected


def test_event_id_ignores_everything_a_backfill_and_a_stream_disagree_about() -> None:
    """Same transaction, different observer: the id must be identical."""
    backfill = make_swap_event(signature_seed=5)
    live = make_swap_event(signature_seed=5).model_copy(
        update={
            "source": EventSource.LIVE_STREAM,
            "source_provider": "helius",
            "observed_at_utc": T0 + timedelta(hours=9),
            "finality": Finality.FINALIZED,
            "transaction_index": 42,
        }
    )
    assert backfill.event_id == live.event_id


def test_event_id_separates_fields_unforgeably() -> None:
    """The unit separator cannot appear in base58 or an instruction path, so no pair of
    different inputs can produce the same joined payload."""
    a = compute_event_id("solana", SIGNATURE, WALLET_A, "3", 10)
    b = compute_event_id("solana", SIGNATURE, WALLET_A, "31", 0)
    assert a != b


def test_event_id_distinguishes_net_swaps_in_one_transaction() -> None:
    first = compute_event_id("solana", SIGNATURE, WALLET_A, "3", 0)
    second = compute_event_id("solana", SIGNATURE, WALLET_A, "3", 1)
    assert first != second


def test_intent_id_is_the_idempotency_key() -> None:
    event_id = compute_event_id("solana", SIGNATURE, WALLET_A, "3", 0)
    first = compute_intent_id(event_id, "sel_1", "1.0.0")
    assert first == compute_intent_id(event_id, "sel_1", "1.0.0")
    assert first != compute_intent_id(event_id, "sel_2", "1.0.0")
    assert first != compute_intent_id(event_id, "sel_1", "1.1.0")


def test_compact_timestamp_has_no_colons() -> None:
    """S0 7.1: Windows does not allow ':' in file names."""
    stamp = compact_timestamp(T0)
    assert stamp == "2026-09-01T12-00-00Z"
    assert ":" not in stamp


# --------------------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------------------
def _write_selection(tmp_path: Path) -> Path:
    path = tmp_path / "sel_test.json"
    write_json_artifact(
        path=path,
        document=make_selection().to_wire(),
        artifact_type=ArtifactType.TRADER_SELECTION,
        plan="A",
        component="test",
        component_version="1.0.0",
        config_hash=sha256_hex("config"),
        repo_root=tmp_path,
        contracts_root=tmp_path,
    )
    return path


def test_roundtrip_of_a_selection(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    loaded = load_json_artifact(path, expected_type=ArtifactType.TRADER_SELECTION)
    assert loaded.records[0]["selection_id"] == "test_selection"
    assert loaded.manifest.record_count == 1
    assert loaded.manifest.producer.plan == "A"


def test_a_same_length_tamper_is_caught_by_the_checksum(tmp_path: Path) -> None:
    """Edited in place without changing the size, so only the hash can catch it."""
    path = _write_selection(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    digest = document["params_hash"]
    document["params_hash"] = ("b" if digest[0] != "b" else "c") + digest[1:]
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )
    assert path.stat().st_size, "precondition: the file still exists"

    with pytest.raises(ArtifactError, match="checksum mismatch"):
        load_json_artifact(path)


def test_a_resized_tamper_is_caught_by_the_byte_count(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["wallets"][0]["weight"] = "0.9"
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )

    with pytest.raises(ArtifactError, match="bytes"):
        load_json_artifact(path)


def test_a_truncated_payload_is_refused(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    content = path.read_text(encoding="utf-8")
    path.write_text(content[: len(content) // 2], encoding="utf-8", newline=chr(10))

    with pytest.raises(ArtifactError):
        load_json_artifact(path)


def test_a_file_without_a_manifest_counts_as_incomplete(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    manifest_path_for(path).unlink()

    with pytest.raises(ArtifactError, match="no manifest"):
        load_json_artifact(path)


def test_an_unknown_major_version_is_refused(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    manifest_file = manifest_path_for(path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["record_schema_version"] = "2.0.0"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )

    with pytest.raises(ArtifactError, match="unsupported schema_version"):
        load_json_artifact(path)


def test_a_manifest_naming_another_file_is_refused(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    manifest_file = manifest_path_for(path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["file_name"] = "something_else.json"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )

    with pytest.raises(ArtifactError, match="manifest names"):
        load_json_artifact(path)


def test_wrong_artifact_type_is_refused(tmp_path: Path) -> None:
    path = _write_selection(tmp_path)
    with pytest.raises(ArtifactError, match="expected artifact type"):
        load_json_artifact(path, expected_type=ArtifactType.PAPER_FILLS)


def test_jsonl_record_count_must_match_the_manifest(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    records = [make_swap_event(signature_seed=i).to_wire() for i in (1, 2, 3)]
    write_jsonl_artifact(
        path=path,
        records=records,
        artifact_type=ArtifactType.SWAP_EVENTS,
        plan="B",
        component="test",
        component_version="1.0.0",
        config_hash=sha256_hex("config"),
        repo_root=tmp_path,
        contracts_root=tmp_path,
    )
    loaded = load_jsonl_artifact(path, expected_type=ArtifactType.SWAP_EVENTS)
    assert len(loaded.records) == 3
    assert loaded.manifest.record_count == 3


def test_an_empty_jsonl_artifact_is_valid(tmp_path: Path) -> None:
    """A run that saw nothing must still be able to report that it saw nothing."""
    path = tmp_path / "empty.jsonl"
    write_jsonl_artifact(
        path=path,
        records=[],
        artifact_type=ArtifactType.LATENCY,
        plan="B",
        component="test",
        component_version="1.0.0",
        config_hash=sha256_hex("config"),
        repo_root=tmp_path,
        contracts_root=tmp_path,
    )
    loaded = load_jsonl_artifact(path)
    assert loaded.records == ()
    assert loaded.manifest.record_count == 0


def test_writing_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    _write_selection(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))


def test_provenance_ignores_the_run_s_own_output(tmp_path: Path, monkeypatch) -> None:
    """A run writing its artifacts must not thereby declare itself unreproducible.

    Dirtiness is judged on the inputs - source, schemas, configuration - not on the outputs
    the run just produced.
    """
    import subprocess

    from gundix_contracts import artifacts as artifacts_module

    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        # Only untracked output, exactly what a finished run leaves behind.
        return subprocess.CompletedProcess(
            command, 0, stdout="?? artifacts/\n?? data/gundix.sqlite\n", stderr=""
        )

    monkeypatch.setattr(artifacts_module.subprocess, "run", fake_run)
    assert artifacts_module.git_commit(tmp_path) == "a" * 40

    def dirty_source(command, **_kwargs):
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(
            command, 0, stdout=" M src/stream/decoder.py\n?? artifacts/\n", stderr=""
        )

    monkeypatch.setattr(artifacts_module.subprocess, "run", dirty_source)
    assert artifacts_module.git_commit(tmp_path) == "UNCOMMITTED"
