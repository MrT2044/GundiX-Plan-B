"""Reading and writing the artifacts that cross the Plan A / Plan B boundary (S0 section 7).

Rules implemented here, all of them from the contract:

* every artifact has a ``<file_name>.manifest.json`` sidecar,
* the reader verifies ``content_sha256`` **before** using the payload; a mismatch is
  fail-closed,
* a file found without a manifest counts as incomplete,
* writing is atomic: payload to ``<name>.tmp``, ``fsync``, ``os.replace``, and only then
  the manifest,
* artifacts are immutable - a correction is a new file with a new timestamp, never an
  overwrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from gundix_contracts.enums import ArtifactType
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    ArtifactManifest,
    ContractVersionError,
    ObservationWindow,
    Producer,
    check_schema_version,
)
from gundix_contracts.schema_registry import validate_document
from gundix_contracts.types import utc_now

MANIFEST_SUFFIX = ".manifest.json"

#: contract name -> schema file stem, used to map a manifest back to its validator
_CONTRACT_BY_SCHEMA_FILE = {
    "swap_event.schema.json": "swap_event",
    "trader_selection.schema.json": "trader_selection",
    "copy_intent.schema.json": "copy_intent",
    "execution_result.schema.json": "execution_result",
    "latency_observation.schema.json": "latency_observation",
    "decoder_coverage.schema.json": "decoder_coverage",
}

#: artifact type -> the contract its records validate against
CONTRACT_BY_ARTIFACT_TYPE: dict[ArtifactType, str] = {
    ArtifactType.TRADER_SELECTION: "trader_selection",
    ArtifactType.SWAP_EVENTS: "swap_event",
    ArtifactType.COPY_INTENTS: "copy_intent",
    ArtifactType.PAPER_FILLS: "execution_result",
    ArtifactType.LATENCY: "latency_observation",
    ArtifactType.DECODER_COVERAGE: "decoder_coverage",
}


class ArtifactError(RuntimeError):
    """The artifact cannot be trusted. Never downgraded to a warning."""


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    manifest: ArtifactManifest
    path: Path
    records: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()


def compact_timestamp(moment: datetime | None = None) -> str:
    """``2026-06-01T12-00-00Z`` - S0 7.1. Windows does not allow ``:`` in file names."""
    return (moment or utc_now()).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(repo_root: Path) -> str:
    """Real 40-char commit hash, or the explicit marker ``UNCOMMITTED``.

    A dirty working tree also yields ``UNCOMMITTED``: claiming a commit for code that is
    not in that commit would make the artifact unreproducible while looking reproducible,
    which is worse than admitting it.
    """
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head.returncode != 0:
            return "UNCOMMITTED"
        dirty = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            return "UNCOMMITTED"
        return head.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "UNCOMMITTED"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def manifest_path_for(payload_path: Path) -> Path:
    return payload_path.with_name(payload_path.name + MANIFEST_SUFFIX)


def _build_manifest(
    *,
    path: Path,
    body: bytes,
    artifact_type: ArtifactType,
    record_contract: str,
    record_count: int,
    plan: Literal["A", "B"],
    component: str,
    component_version: str,
    config_hash: str,
    repo_root: Path,
    contracts_root: Path,
    data_snapshot_id: str | None,
    observation_window: ObservationWindow | None,
) -> ArtifactManifest:
    return ArtifactManifest(
        schema_version=SCHEMA_VERSIONS["artifact_manifest"],
        artifact_type=artifact_type,
        file_name=path.name,
        file_bytes=len(body),
        content_sha256=hashlib.sha256(body).hexdigest(),
        record_schema_version=SCHEMA_VERSIONS[record_contract],
        record_count=record_count,
        producer=Producer(plan=plan, component=component, version=component_version),
        git_commit=git_commit(repo_root),
        contracts_commit=git_commit(contracts_root),
        config_hash=config_hash,
        created_at_utc=utc_now(),
        data_snapshot_id=data_snapshot_id,
        observation_window=observation_window,
    )


def write_jsonl_artifact(
    *,
    path: Path,
    records: Iterable[dict[str, Any]],
    artifact_type: ArtifactType,
    plan: Literal["A", "B"],
    component: str,
    component_version: str,
    config_hash: str,
    repo_root: Path,
    contracts_root: Path,
    data_snapshot_id: str | None = None,
    observation_window: ObservationWindow | None = None,
    validate: bool = True,
) -> ArtifactManifest:
    """Write a JSONL artifact plus its manifest atomically."""
    contract = CONTRACT_BY_ARTIFACT_TYPE[artifact_type]
    lines: list[bytes] = []
    for record in records:
        if validate:
            validate_document(contract, record)
        lines.append(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
    body = b"\n".join(lines) + (b"\n" if lines else b"")
    _atomic_write(path, body)

    manifest = _build_manifest(
        path=path,
        body=body,
        artifact_type=artifact_type,
        record_contract=contract,
        record_count=len(lines),
        plan=plan,
        component=component,
        component_version=component_version,
        config_hash=config_hash,
        repo_root=repo_root,
        contracts_root=contracts_root,
        data_snapshot_id=data_snapshot_id,
        observation_window=observation_window,
    )
    _write_manifest(path, manifest)
    return manifest


def write_json_artifact(
    *,
    path: Path,
    document: dict[str, Any],
    artifact_type: ArtifactType,
    plan: Literal["A", "B"],
    component: str,
    component_version: str,
    config_hash: str,
    repo_root: Path,
    contracts_root: Path,
    data_snapshot_id: str | None = None,
    observation_window: ObservationWindow | None = None,
) -> ArtifactManifest:
    """Write a single-document artifact (e.g. a TraderSelection) plus its manifest."""
    contract = CONTRACT_BY_ARTIFACT_TYPE[artifact_type]
    validate_document(contract, document)
    body = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    _atomic_write(path, body)

    manifest = _build_manifest(
        path=path,
        body=body,
        artifact_type=artifact_type,
        record_contract=contract,
        record_count=1,
        plan=plan,
        component=component,
        component_version=component_version,
        config_hash=config_hash,
        repo_root=repo_root,
        contracts_root=contracts_root,
        data_snapshot_id=data_snapshot_id,
        observation_window=observation_window,
    )
    _write_manifest(path, manifest)
    return manifest


def _write_manifest(payload_path: Path, manifest: ArtifactManifest) -> None:
    payload = manifest.to_wire()
    validate_document("artifact_manifest", payload)
    _atomic_write(
        manifest_path_for(payload_path),
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    )


def read_manifest(payload_path: Path) -> ArtifactManifest:
    """Load the manifest and verify it against the payload on disk."""
    manifest_file = manifest_path_for(payload_path)
    if not manifest_file.exists():
        raise ArtifactError(
            f"no manifest for {payload_path.name}; a file without a manifest counts as incomplete"
        )
    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"manifest {manifest_file.name} is not valid JSON: {exc}") from exc
    validate_document("artifact_manifest", raw)
    manifest = ArtifactManifest.model_validate(raw)

    if manifest.file_name != payload_path.name:
        raise ArtifactError(
            f"manifest names {manifest.file_name!r} but was loaded for {payload_path.name!r}"
        )
    if not payload_path.exists():
        raise ArtifactError(f"manifest exists but payload {payload_path.name} does not")
    actual_bytes = payload_path.stat().st_size
    if actual_bytes != manifest.file_bytes:
        raise ArtifactError(
            f"{payload_path.name}: manifest says {manifest.file_bytes} bytes, file has {actual_bytes}"
        )
    actual_hash = sha256_file(payload_path)
    if actual_hash != manifest.content_sha256:
        raise ArtifactError(
            f"checksum mismatch for {payload_path.name}: manifest says "
            f"{manifest.content_sha256}, file is {actual_hash}"
        )
    return manifest


def iter_jsonl_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ArtifactError(f"{path.name}:{lineno} is not valid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ArtifactError(f"{path.name}:{lineno} is not a JSON object")
            yield record


def _check_kind_and_version(
    manifest: ArtifactManifest, expected_type: ArtifactType | None
) -> tuple[str, tuple[str, ...]]:
    if expected_type is not None and manifest.artifact_type is not expected_type:
        raise ArtifactError(
            f"expected artifact type {expected_type.value}, manifest says {manifest.artifact_type.value}"
        )
    contract = CONTRACT_BY_ARTIFACT_TYPE[manifest.artifact_type]
    try:
        warnings = check_schema_version(contract, manifest.record_schema_version)
    except ContractVersionError as exc:
        raise ArtifactError(str(exc)) from exc
    return contract, tuple(warnings)


def load_jsonl_artifact(path: Path, *, expected_type: ArtifactType | None = None) -> LoadedArtifact:
    """Verify manifest and checksum, then validate every record against its schema."""
    manifest = read_manifest(path)
    contract, warnings = _check_kind_and_version(manifest, expected_type)
    records: list[dict[str, Any]] = []
    for record in iter_jsonl_records(path):
        validate_document(contract, record)
        records.append(record)
    if len(records) != manifest.record_count:
        raise ArtifactError(
            f"{path.name}: manifest claims {manifest.record_count} records, found {len(records)}"
        )
    return LoadedArtifact(manifest=manifest, path=path, records=tuple(records), warnings=warnings)


def load_json_artifact(path: Path, *, expected_type: ArtifactType | None = None) -> LoadedArtifact:
    """Verify manifest and checksum, then validate the single document."""
    manifest = read_manifest(path)
    contract, warnings = _check_kind_and_version(manifest, expected_type)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path.name} is not valid JSON: {exc}") from exc
    validate_document(contract, document)
    return LoadedArtifact(manifest=manifest, path=path, records=(document,), warnings=warnings)
