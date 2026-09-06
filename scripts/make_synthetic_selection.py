"""Create a synthetic TraderSelection so Plan B can run before Plan A delivers a real one.

The wallets are taken from the recorded golden transactions, so a replay of those fixtures
actually reaches the policy instead of being filtered out at the first gate.

**This artifact is not a research result.** Its scores are zero, its windows are
placeholders and its wallets were chosen because they appear in a recorded transaction, not
because anything suggests they are worth copying. It exists to exercise the pipeline. When
Plan A delivers a real selection, only this file is replaced - no code changes.

    python scripts/make_synthetic_selection.py [--approve] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "contracts" / "python")]

from gundix_contracts.artifacts import write_json_artifact  # noqa: E402
from gundix_contracts.enums import ArtifactType, WalletStatus  # noqa: E402
from gundix_contracts.ids import config_hash  # noqa: E402
from gundix_contracts.models import (  # noqa: E402
    SCHEMA_VERSIONS,
    CopyPnlByLatency,
    ManualApproval,
    SelectedWallet,
    TimeWindow,
    TraderSelection,
)

RAW_DIR = REPO_ROOT / "contracts" / "golden_raw"
DEFAULT_OUT = REPO_ROOT / "artifacts" / "selections" / "synthetic_dev.json"


def wallets_from_fixtures(limit: int = 8) -> list[str]:
    wallets: list[str] = []
    for path in sorted(RAW_DIR.glob("*.json")):
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for wallet in fixture.get("wallets") or []:
            if wallet and wallet not in wallets:
                wallets.append(str(wallet))
        if len(wallets) >= limit:
            break
    return wallets[:limit]


def build(wallets: list[str], *, approved: bool, now: datetime) -> TraderSelection:
    zero = CopyPnlByLatency.model_validate(
        {"1s": "0", "3s": "0", "5s": "0", "15s": "0", "60s": "0"}
    )
    weight = (Decimal(1) / Decimal(max(1, len(wallets)))).quantize(Decimal("0.0001"))
    entries = tuple(
        SelectedWallet(
            wallet=wallet,
            status=WalletStatus.ACTIVE,
            rank=index + 1,
            score=Decimal(0),
            weight=weight,
            max_weight=weight,
            reason_codes=("synthetic_fixture_wallet",),
            risks=("NOT_A_RESEARCH_RESULT",),
            coverage_ratio=Decimal(0),
            metrics_ref=None,
            copy_pnl_by_latency=zero,
        )
        for index, wallet in enumerate(wallets)
    )
    window = TimeWindow(
        label="A",
        start_utc=now - timedelta(days=90),
        end_utc=now - timedelta(days=45),
        start_slot=0,
        end_slot=0,
    )
    evaluation = TimeWindow(
        label="B",
        start_utc=now - timedelta(days=45),
        end_utc=now,
        start_slot=0,
        end_slot=0,
    )
    return TraderSelection(
        schema_version=SCHEMA_VERSIONS["trader_selection"],
        selection_id="synthetic_dev",
        created_at_utc=now,
        data_snapshot_id="synthetic-no-snapshot",
        code_commit="0" * 40,
        params_hash=config_hash({"synthetic": True}),
        score_version="0.0.0",
        selection_window=window,
        evaluation_window=evaluation,
        wallets=entries,
        applied_filters=(),
        manual_approval=ManualApproval(
            approved=approved,
            approved_by="synthetic-fixture",
            approved_at_utc=now,
            note=(
                "SYNTHETIC. Wallets come from recorded golden transactions, not from any "
                "analysis. Never use this for anything but exercising the pipeline."
            ),
        ),
        expires_at_utc=now + timedelta(days=30),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--approve",
        action="store_true",
        help="mark it approved so the paper path can run end to end; off by default",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    wallets = wallets_from_fixtures()
    if not wallets:
        print("no wallets found in contracts/golden_raw; run scripts/record_golden.py first")
        return 1

    now = datetime.now(UTC)
    selection = build(wallets, approved=args.approve, now=now)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    manifest = write_json_artifact(
        path=args.out,
        document=selection.to_wire(),
        artifact_type=ArtifactType.TRADER_SELECTION,
        plan="B",
        component="scripts.make_synthetic_selection",
        component_version="1.0.0",
        config_hash=config_hash({"synthetic": True, "wallets": wallets}),
        repo_root=REPO_ROOT,
        contracts_root=REPO_ROOT / "contracts",
        data_snapshot_id="synthetic-no-snapshot",
    )
    print(f"wrote {args.out} ({len(wallets)} wallets, approved={args.approve})")
    print(f"manifest sha256 {manifest.content_sha256}")
    print(
        "NOTE: produced by Plan B as a stand-in. Plan A owns real selections; replacing this "
        "file is the whole integration step."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
