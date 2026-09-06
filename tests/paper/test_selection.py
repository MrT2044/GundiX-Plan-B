"""Selection import and hot reload (B1, S0 4.4).

The selection is the only thing that tells Plan B which wallets to copy, so the interesting
cases are all the ways a bad one must fail to become active.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from gundix_contracts.artifacts import ArtifactError, manifest_path_for
from gundix_contracts.enums import OperatingMode, WalletStatus
from gundix_contracts.schema_registry import SchemaValidationError

from src.common.clock import FrozenClock
from src.paper.selection import (
    SelectionRejectedError,
    SelectionRepository,
    selection_blocks_execution,
)
from tests.factories import (
    T0,
    WALLET_A,
    WALLET_B,
    make_selection,
    write_selection_artifact,
)


def _repo(tmp_path: Path, selection, *, require_approval: bool = True, now=T0):
    path = tmp_path / "sel.json"
    write_selection_artifact(path, selection)
    return SelectionRepository(
        path, clock=FrozenClock(now), require_approval=require_approval
    ), path


def test_a_valid_selection_activates(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, make_selection(wallets=(WALLET_A, WALLET_B)))
    active = repo.load()

    assert active.selection_id == "test_selection"
    assert active.active_wallet_count == 2
    assert active.is_tradeable_wallet(WALLET_A)
    assert repo.active is active


def test_an_empty_selection_is_valid_but_trades_nothing(tmp_path: Path) -> None:
    """00_GESAMTPLAN: an empty selection is a permissible result."""
    repo, _ = _repo(tmp_path, make_selection(wallets=()))
    active = repo.load()

    assert active.active_wallet_count == 0
    assert selection_blocks_execution(active, now=T0, mode=OperatingMode.PAPER) is None
    assert not active.is_tradeable_wallet(WALLET_A)


# -- the eight rejection rules of S0 4.4 -------------------------------------------------
def test_rule_2_an_unapproved_selection_is_refused(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, make_selection(approved=False))
    with pytest.raises(SelectionRejectedError, match="rule_2_not_approved"):
        repo.load()


def test_rule_3_an_expired_selection_is_refused(tmp_path: Path) -> None:
    selection = make_selection(expires_in_days=1)
    repo, _ = _repo(tmp_path, selection, now=T0 + timedelta(days=2))
    with pytest.raises(SelectionRejectedError, match="rule_3_expired"):
        repo.load()


def test_rule_4_a_checksum_mismatch_is_refused(tmp_path: Path) -> None:
    repo, path = _repo(tmp_path, make_selection())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["params_hash"] = "f" * 64
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )
    with pytest.raises(ArtifactError):
        repo.load()


def test_rule_5_a_duplicate_wallet_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate wallet"):
        make_selection(wallets=(WALLET_A, WALLET_A))


def test_rule_6_an_invalid_address_is_refused() -> None:
    with pytest.raises(ValueError, match="base58"):
        make_selection(wallets=("not-a-valid-address-0OIl",))


def test_rule_7_weight_above_max_weight_is_refused() -> None:
    from decimal import Decimal

    from gundix_contracts.models import CopyPnlByLatency, SelectedWallet

    zero = CopyPnlByLatency.model_validate(
        {"1s": "0", "3s": "0", "5s": "0", "15s": "0", "60s": "0"}
    )
    with pytest.raises(ValueError, match="max_weight"):
        SelectedWallet(
            wallet=WALLET_A,
            status=WalletStatus.ACTIVE,
            rank=1,
            score=Decimal(0),
            weight=Decimal("0.9"),
            max_weight=Decimal("0.5"),
            reason_codes=(),
            risks=(),
            coverage_ratio=Decimal("1"),
            metrics_ref=None,
            copy_pnl_by_latency=zero,
        )


def test_rule_7_weights_summing_above_one_are_refused() -> None:
    from decimal import Decimal

    with pytest.raises(ValueError, match="sum of weights"):
        make_selection(wallets=(WALLET_A, WALLET_B), weight=Decimal("2.0"))


def test_rule_8_an_empty_code_commit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sel.json"
    write_selection_artifact(path, make_selection())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["code_commit"] = ""
    from gundix_contracts.schema_registry import validate_document

    with pytest.raises(SchemaValidationError):
        validate_document("trader_selection", document)


def test_an_unknown_major_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sel.json"
    write_selection_artifact(path, make_selection())
    manifest_file = manifest_path_for(path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["record_schema_version"] = "9.0.0"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8", newline=chr(10)
    )
    repo = SelectionRepository(path, clock=FrozenClock(T0))
    with pytest.raises(ArtifactError, match="unsupported schema_version"):
        repo.load()


# -- status handling ---------------------------------------------------------------------
@pytest.mark.parametrize("status", [WalletStatus.OBSERVE_ONLY, WalletStatus.SUSPENDED])
def test_only_active_wallets_may_trade(tmp_path: Path, status: WalletStatus) -> None:
    repo, _ = _repo(tmp_path, make_selection(status=status))
    active = repo.load()

    assert active.wallet(WALLET_A) is not None
    assert not active.is_tradeable_wallet(WALLET_A)
    assert active.active_wallet_count == 0


# -- hot reload ---------------------------------------------------------------------------
def test_reload_activates_a_changed_file(tmp_path: Path) -> None:
    repo, path = _repo(tmp_path, make_selection(wallets=(WALLET_A,)))
    repo.load()

    write_selection_artifact(
        path, make_selection(wallets=(WALLET_A, WALLET_B), selection_id="test_selection_v2")
    )
    assert repo.reload_if_changed() is True
    assert repo.active is not None
    assert repo.active.selection_id == "test_selection_v2"
    assert repo.active.active_wallet_count == 2


def test_reload_is_a_no_op_when_nothing_changed(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, make_selection())
    repo.load()
    assert repo.reload_if_changed() is False


def test_a_broken_reload_keeps_the_last_valid_selection(tmp_path: Path) -> None:
    """The whole point of atomic activation: a bad update must change nothing."""
    repo, path = _repo(tmp_path, make_selection(wallets=(WALLET_A,)))
    repo.load()
    before = repo.active

    path.write_text("{ this is not json", encoding="utf-8", newline=chr(10))
    assert repo.reload_if_changed() is False

    assert repo.active is before
    assert repo.active is not None
    assert repo.active.is_tradeable_wallet(WALLET_A)
    assert repo.last_error is not None


def test_a_partially_written_file_keeps_the_last_valid_selection(tmp_path: Path) -> None:
    repo, path = _repo(tmp_path, make_selection(wallets=(WALLET_A,)))
    repo.load()
    content = path.read_text(encoding="utf-8")
    path.write_text(content[: len(content) // 2], encoding="utf-8", newline=chr(10))

    assert repo.reload_if_changed() is False
    assert repo.active is not None
    assert repo.active.is_tradeable_wallet(WALLET_A)


def test_an_expired_reload_keeps_the_last_valid_selection(tmp_path: Path) -> None:
    path = tmp_path / "sel.json"
    write_selection_artifact(path, make_selection(wallets=(WALLET_A,)))
    clock = FrozenClock(T0)
    repo = SelectionRepository(path, clock=clock)
    repo.load()

    write_selection_artifact(
        path, make_selection(wallets=(WALLET_B,), selection_id="expired_one", expires_in_days=1)
    )
    clock.advance(60 * 60 * 48)
    assert repo.reload_if_changed() is False
    assert repo.active is not None
    assert repo.active.selection_id == "test_selection"


# -- execution gating ----------------------------------------------------------------------
def test_observe_mode_never_produces_intents(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, make_selection())
    active = repo.load()
    assert (
        selection_blocks_execution(active, now=T0, mode=OperatingMode.OBSERVE)
        == "mode_disallows_execution"
    )


def test_missing_selection_blocks_execution() -> None:
    assert selection_blocks_execution(None, now=T0, mode=OperatingMode.PAPER) == "selection_invalid"


def test_expired_selection_blocks_execution(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, make_selection(expires_in_days=1))
    active = repo.load()
    assert (
        selection_blocks_execution(active, now=T0 + timedelta(days=2), mode=OperatingMode.PAPER)
        == "selection_expired"
    )
