"""Observation artifacts for Plan A (S0 section 7.1).

Plan B delivers three files per run:

* ``latency_<ts>.jsonl``          - measured pipeline latencies (A10 replaces the
  provisional backtest assumptions with these),
* ``paper_fills_<ts>.jsonl``      - ExecutionResults,
* ``decoder_coverage_<ts>.json``  - what the decoder could and could not read.

Each is written atomically with a manifest, and each is immutable: a correction is a new
file with a new timestamp, never an overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gundix_contracts.artifacts import (
    compact_timestamp,
    write_json_artifact,
    write_jsonl_artifact,
)
from gundix_contracts.enums import ArtifactType
from gundix_contracts.models import (
    SCHEMA_VERSIONS,
    CoverageWindow,
    DecoderCoverage,
    ExecutionResult,
    LatencyObservation,
    ObservationWindow,
    QuarantineReasonCount,
    UnknownProgramCount,
    VenueCount,
)

from src.common.config import RuntimeConfig
from src.common.logging import get_logger, log_event
from src.stream.pipeline import PIPELINE_VERSION, CoverageCounters

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = REPO_ROOT / "contracts"


@dataclass(frozen=True, slots=True)
class ExportResult:
    latency_path: Path | None
    paper_fills_path: Path | None
    coverage_path: Path | None
    records: dict[str, int]


class ObservationExporter:
    """Writes what Plan B observed, in the shape Plan A reads."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._directory = Path(config.export.directory)
        self._config_hash = config.hash()

    def export(
        self,
        *,
        latencies: list[LatencyObservation],
        results: list[ExecutionResult],
        coverage: CoverageCounters,
        window_start: datetime,
        window_end: datetime,
        timestamp: datetime | None = None,
    ) -> ExportResult:
        stamp = compact_timestamp(timestamp or window_end)
        self._directory.mkdir(parents=True, exist_ok=True)
        window = ObservationWindow(start_utc=window_start, end_utc=window_end)

        latency_path: Path | None = None
        if latencies:
            latency_path = self._directory / f"latency_{stamp}.jsonl"
            write_jsonl_artifact(
                path=latency_path,
                records=[observation.to_wire() for observation in latencies],
                artifact_type=ArtifactType.LATENCY,
                plan="B",
                component="stream.pipeline",
                component_version=PIPELINE_VERSION,
                config_hash=self._config_hash,
                repo_root=REPO_ROOT,
                contracts_root=CONTRACTS_ROOT,
                observation_window=window,
            )

        fills_path: Path | None = None
        if results:
            fills_path = self._directory / f"paper_fills_{stamp}.jsonl"
            write_jsonl_artifact(
                path=fills_path,
                records=[result.to_wire() for result in results],
                artifact_type=ArtifactType.PAPER_FILLS,
                plan="B",
                component="paper.broker",
                component_version=PIPELINE_VERSION,
                config_hash=self._config_hash,
                repo_root=REPO_ROOT,
                contracts_root=CONTRACTS_ROOT,
                observation_window=window,
            )

        coverage_path = self._directory / f"decoder_coverage_{stamp}.json"
        document = self._coverage_document(coverage, window_start, window_end)
        write_json_artifact(
            path=coverage_path,
            document=document.to_wire(),
            artifact_type=ArtifactType.DECODER_COVERAGE,
            plan="B",
            component="stream.decoder",
            component_version=PIPELINE_VERSION,
            config_hash=self._config_hash,
            repo_root=REPO_ROOT,
            contracts_root=CONTRACTS_ROOT,
            observation_window=window,
        )

        result = ExportResult(
            latency_path=latency_path,
            paper_fills_path=fills_path,
            coverage_path=coverage_path,
            records={
                "latency": len(latencies),
                "paper_fills": len(results),
                "coverage": 1,
            },
        )
        log_event(
            logger,
            20,
            "observation artifacts written",
            directory=str(self._directory),
            latency=len(latencies),
            paper_fills=len(results),
            coverage_ratio=str(coverage.coverage_ratio),
        )
        return result

    def _coverage_document(
        self, coverage: CoverageCounters, start: datetime, end: datetime
    ) -> DecoderCoverage:
        return DecoderCoverage(
            schema_version=SCHEMA_VERSIONS["decoder_coverage"],
            window=CoverageWindow(start_utc=start, end_utc=end),
            transactions_seen=coverage.transactions_seen,
            transactions_failed_onchain=coverage.transactions_failed_onchain,
            transactions_decoded_complete=coverage.transactions_decoded_complete,
            transactions_partial=coverage.transactions_partial,
            transactions_unknown=coverage.transactions_unknown,
            transactions_no_swap=coverage.transactions_no_swap,
            coverage_ratio=coverage.coverage_ratio,
            events_emitted=coverage.events_emitted,
            quarantined=coverage.quarantined,
            unknown_programs=tuple(
                UnknownProgramCount(
                    program_id=program_id,
                    occurrences=count,
                    affected_wallets=len(coverage.unknown_program_wallets.get(program_id, set())),
                )
                for program_id, count in sorted(coverage.unknown_programs.items())
            ),
            venues_seen=tuple(
                VenueCount(venue=venue, events=count)
                for venue, count in sorted(coverage.venues.items())
            ),
            quarantine_reasons=tuple(
                QuarantineReasonCount(reason=reason, count=count)
                for reason, count in sorted(coverage.quarantine_reasons.items())
            ),
        )


__all__ = ["ExportResult", "ObservationExporter"]
