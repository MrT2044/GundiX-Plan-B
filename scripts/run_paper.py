"""Run Plan B.

    GUNDIX_MODE=PAPER python scripts/run_paper.py --config config/runtime.example.yaml

The mode comes from ``GUNDIX_MODE`` and from nowhere else. There is no flag to raise it,
and a missing value stops the process rather than picking one (S0 8.2).

With ``stream.source: replay`` this runs entirely offline against the recorded
transactions, which is what the end-to-end paper test does. With ``rpc_polling`` it polls a
real RPC for the wallets in the active selection.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "contracts" / "python")]

from src.common.app import build_application  # noqa: E402
from src.common.config import ConfigError, load_config  # noqa: E402
from src.common.db import migrations_current, unit_of_work  # noqa: E402
from src.common.logging import configure_logging, get_logger, log_event  # noqa: E402
from src.common.mode import ModeError, resolve_mode  # noqa: E402

logger = get_logger("gundix.run")
_stop = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _stop
    _stop = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "config" / "runtime.example.yaml"
    )
    parser.add_argument("--once", action="store_true", help="drain the source once and exit")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    parser.add_argument("--max-iterations", type=int, default=0, help="0 = run until stopped")
    parser.add_argument("--create-tables", action="store_true", help="bootstrap an empty database")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(logging.DEBUG if args.verbose else logging.INFO)

    try:
        mode = resolve_mode()
    except ModeError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    app = build_application(config, mode, create_tables=args.create_tables)
    started = datetime.now(UTC)

    # B9 preflight: a database that is behind the migrations fails later, deeper and less
    # legibly than it fails here.
    ok, detail = migrations_current(app.engine, REPO_ROOT / "migrations")
    if not ok and not args.create_tables:
        print(
            f"refusing to start: database schema is not current ({detail}). "
            "Run: python -m alembic -c alembic.ini upgrade head",
            file=sys.stderr,
        )
        app.close()
        return 5

    try:
        app.selections.load()
    except Exception as exc:
        log_event(
            logger, 40, "no usable selection", error=str(exc), path=str(config.selection.path)
        )
        print(f"refusing to start: selection unusable: {exc}", file=sys.stderr)
        app.close()
        return 3

    with unit_of_work(app.session_factory) as session:
        report = app.reconciler.run(session)
    if report.blocks_execution:
        print("reconciliation found blocking discrepancies:\n" + report.render(), file=sys.stderr)
        app.close()
        return 4

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    iterations = 0
    processed = 0
    try:
        while not _stop:
            app.selections.reload_if_changed()
            outcomes = app.pipeline.run_once()
            processed += len(outcomes)
            iterations += 1
            if args.once or (args.max_iterations and iterations >= args.max_iterations):
                break
            if not outcomes:
                time.sleep(args.interval)
    finally:
        result = app.exporter.export(
            latencies=app.pipeline.latency_observations,
            results=app.pipeline.execution_results,
            coverage=app.pipeline.coverage,
            window_start=started,
            window_end=datetime.now(UTC),
        )
        coverage = app.pipeline.coverage
        print(
            f"\nprocessed {processed} transactions in {iterations} iteration(s)\n"
            f"  events emitted       : {coverage.events_emitted}\n"
            f"  quarantined          : {coverage.quarantined}\n"
            f"  coverage ratio       : {coverage.coverage_ratio}\n"
            f"  decisions            : {dict(sorted(coverage.decisions.items()))}\n"
            f"  latency records      : {result.records['latency']}\n"
            f"  paper fills          : {result.records['paper_fills']}\n"
            f"  coverage artifact    : {result.coverage_path}"
        )
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
