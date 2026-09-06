"""Runtime configuration for Plan B.

Two rules make this file more than a settings bag:

1. **Absolute code-level ceilings.** ``ABSOLUTE_*`` cannot be raised by configuration.
   A typo in a YAML file must not be able to authorise a larger trade than the code
   itself is willing to make (02_PLAN_B section 6, "absolute Obergrenzen können nicht
   durch Konfiguration überschritten werden").
2. **Config hash.** Every artifact records the hash of the configuration that produced
   it, so a run can be reproduced or shown to be irreproducible.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from gundix_contracts.enums import DedupPolicy, Finality
from gundix_contracts.ids import config_hash
from gundix_contracts.mints import WSOL_MINT
from gundix_contracts.types import SemVer
from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------------------
# Hard ceilings. Not configurable. Changing one is a code change and a code review.
# --------------------------------------------------------------------------------------
ABSOLUTE_MAX_TRADE_SIZE_SOL = Decimal("1.0")
ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL = Decimal("2.0")
ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL = Decimal("10.0")
ABSOLUTE_MAX_DAILY_EXPOSURE_SOL = Decimal("20.0")
ABSOLUTE_MAX_OPEN_POSITIONS = 25
ABSOLUTE_MAX_SLIPPAGE_BPS = 1000


class ConfigError(ValueError):
    """The configuration is unusable. The process does not start."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SelectionConfig(_Section):
    path: Path
    require_approval: bool = True
    reload_interval_seconds: float = Field(default=30.0, gt=0)


class ReconnectConfig(_Section):
    initial_delay_seconds: float = Field(default=1.0, gt=0)
    max_delay_seconds: float = Field(default=60.0, gt=0)
    multiplier: float = Field(default=2.0, gt=1)
    jitter_ratio: float = Field(default=0.25, ge=0, le=1)
    max_attempts: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must not be below initial_delay_seconds")
        return self


class StreamConfig(_Section):
    source: Literal["replay", "rpc_polling", "websocket"] = "replay"
    rpc_http_url: str | None = None
    ws_url: str | None = None
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    rpc_max_requests_per_second: float = Field(default=4.0, gt=0)
    replay_path: Path | None = None
    commitment: Finality = Finality.CONFIRMED
    required_finality: Finality = Finality.CONFIRMED
    max_queue_depth: int = Field(default=1000, ge=1)
    max_signature_backlog_per_wallet: int = Field(default=200, ge=1)
    reconnect: ReconnectConfig = ReconnectConfig()

    @model_validator(mode="after")
    def _source_requirements(self) -> Self:
        if self.source == "replay" and self.replay_path is None:
            raise ValueError("stream.source=replay requires stream.replay_path")
        if self.source == "rpc_polling" and not self.rpc_http_url:
            raise ValueError("stream.source=rpc_polling requires stream.rpc_http_url")
        if self.source == "websocket" and not self.ws_url:
            raise ValueError("stream.source=websocket requires stream.ws_url")
        if self.source == "websocket" and not self.rpc_http_url:
            # Gap recovery always needs an HTTP endpoint to fetch missed transactions.
            raise ValueError(
                "stream.source=websocket also requires stream.rpc_http_url for gap recovery"
            )
        return self


class PolicyConfig(_Section):
    policy_version: SemVer = "0.1.0"
    dedup_policy: DedupPolicy = DedupPolicy.FIRST_SIGNAL_ONLY
    consensus_required_wallets: int = Field(default=2, ge=2)
    consensus_window_seconds: float = Field(default=120.0, gt=0)
    max_signal_age_seconds: float = Field(default=90.0, gt=0)
    intent_ttl_seconds: float = Field(default=20.0, gt=0)
    base_buy_size_sol: Decimal = Decimal("0.05")
    min_buy_size_sol: Decimal = Decimal("0.01")
    max_slippage_bps: int = Field(default=300, ge=1)
    max_price_impact_bps: int = Field(default=200, ge=1)
    min_pool_liquidity_sol: Decimal = Decimal("20")
    allowed_quote_mints: tuple[str, ...] = (WSOL_MINT,)
    blocked_mints: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _ceilings(self) -> Self:
        if self.max_slippage_bps > ABSOLUTE_MAX_SLIPPAGE_BPS:
            raise ValueError(
                f"policy.max_slippage_bps={self.max_slippage_bps} exceeds the absolute "
                f"code ceiling of {ABSOLUTE_MAX_SLIPPAGE_BPS}"
            )
        if self.min_buy_size_sol > self.base_buy_size_sol:
            raise ValueError("policy.min_buy_size_sol must not exceed policy.base_buy_size_sol")
        if self.base_buy_size_sol <= 0:
            raise ValueError("policy.base_buy_size_sol must be positive")
        return self


class RiskConfig(_Section):
    max_trade_size_sol: Decimal = Decimal("0.1")
    max_position_per_token_sol: Decimal = Decimal("0.3")
    max_total_exposure_sol: Decimal = Decimal("1.0")
    max_daily_exposure_sol: Decimal = Decimal("2.0")
    max_daily_loss_sol: Decimal = Decimal("0.5")
    max_open_positions: int = Field(default=10, ge=1)
    fee_reserve_sol: Decimal = Decimal("0.05")
    starting_balance_sol: Decimal = Decimal("2.0")
    max_consecutive_failures: int = Field(default=5, ge=1)
    max_slippage_observed_bps: int = Field(default=1500, ge=1)
    stale_feed_seconds: float = Field(default=120.0, gt=0)
    min_decoder_coverage: Decimal = Decimal("0.90")
    kill_switch_file: Path = Path("data/KILL_SWITCH")

    @model_validator(mode="after")
    def _ceilings(self) -> Self:
        checks: list[tuple[str, Decimal | int, Decimal | int]] = [
            ("max_trade_size_sol", self.max_trade_size_sol, ABSOLUTE_MAX_TRADE_SIZE_SOL),
            (
                "max_position_per_token_sol",
                self.max_position_per_token_sol,
                ABSOLUTE_MAX_POSITION_PER_TOKEN_SOL,
            ),
            (
                "max_total_exposure_sol",
                self.max_total_exposure_sol,
                ABSOLUTE_MAX_TOTAL_EXPOSURE_SOL,
            ),
            (
                "max_daily_exposure_sol",
                self.max_daily_exposure_sol,
                ABSOLUTE_MAX_DAILY_EXPOSURE_SOL,
            ),
            ("max_open_positions", self.max_open_positions, ABSOLUTE_MAX_OPEN_POSITIONS),
        ]
        for name, value, ceiling in checks:
            if value > ceiling:
                raise ValueError(
                    f"risk.{name}={value} exceeds the absolute code ceiling of {ceiling}. "
                    "Configuration cannot widen a hard limit."
                )
        if self.max_trade_size_sol > self.max_position_per_token_sol:
            raise ValueError(
                "risk.max_trade_size_sol must not exceed risk.max_position_per_token_sol"
            )
        if self.max_position_per_token_sol > self.max_total_exposure_sol:
            raise ValueError(
                "risk.max_position_per_token_sol must not exceed risk.max_total_exposure_sol"
            )
        for name in ("max_trade_size_sol", "max_total_exposure_sol", "starting_balance_sol"):
            if getattr(self, name) <= 0:
                raise ValueError(f"risk.{name} must be positive")
        return self


class PaperConfig(_Section):
    model_version: SemVer = "0.1.0"
    land_probability: Decimal = Decimal("0.9")
    extra_slippage_bps: int = Field(default=50, ge=0)
    priority_fee_lamports: int = Field(default=100_000, ge=0)
    base_fee_lamports: int = Field(default=5_000, ge=0)
    rng_seed: str = "gundix-paper-v1"

    @model_validator(mode="after")
    def _probability(self) -> Self:
        if not (Decimal(0) <= self.land_probability <= Decimal(1)):
            raise ValueError("paper.land_probability must be in [0, 1]")
        return self


class QuoteConfig(_Section):
    provider: Literal["simulated", "jupiter"] = "simulated"
    base_url: str | None = None
    quote_ttl_seconds: float = Field(default=10.0, gt=0)
    http_timeout_seconds: float = Field(default=5.0, gt=0)
    simulated_pool_quote_liquidity_sol: Decimal = Decimal("50")
    simulated_fee_bps: int = Field(default=25, ge=0)


class DecoderConfig(_Section):
    """How the decoder treats programs it does not recognise.

    QUARANTINE_TRANSACTION is the behaviour S0 rule 9 mandates and the only value allowed
    without an approved CCR. RECONCILE_BALANCES is the CCR-001 proposal; switching it on is
    a documented, logged deviation from the contract, not a tuning knob.
    """

    unknown_program_policy: Literal["QUARANTINE_TRANSACTION", "RECONCILE_BALANCES"] = (
        "QUARANTINE_TRANSACTION"
    )
    source_provider: str = "public_rpc"


class DatabaseConfig(_Section):
    url: str = "sqlite:///data/gundix_paper.sqlite"
    busy_timeout_ms: int = Field(default=5000, ge=0)


class ExportConfig(_Section):
    directory: Path = Path("artifacts/observations")
    producer_prefix: str = "planb"


class RuntimeConfig(_Section):
    """The whole Plan B runtime configuration."""

    selection: SelectionConfig
    stream: StreamConfig = StreamConfig(replay_path=Path("contracts/golden/transactions"))
    policy: PolicyConfig = PolicyConfig()
    risk: RiskConfig = RiskConfig()
    paper: PaperConfig = PaperConfig()
    quotes: QuoteConfig = QuoteConfig()
    decoder: DecoderConfig = DecoderConfig()
    database: DatabaseConfig = DatabaseConfig()
    export: ExportConfig = ExportConfig()

    @model_validator(mode="after")
    def _cross_section_limits(self) -> Self:
        # There is deliberately no mode setting here at all: S0 8.2 puts the mode in the
        # environment, and a configuration file must not be able to influence it.
        if self.policy.base_buy_size_sol > self.risk.max_trade_size_sol:
            raise ValueError(
                "policy.base_buy_size_sol must not exceed risk.max_trade_size_sol; "
                "the risk engine would reject every intent"
            )
        return self

    def hash(self) -> str:
        """Stable hash of the effective configuration, recorded in every artifact."""
        return config_hash(_jsonify(self.model_dump(mode="json")))


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (Path, Decimal)):
        return str(value)
    return value


def load_config(path: str | Path) -> RuntimeConfig:
    """Load and validate a YAML runtime configuration."""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"configuration file not found: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file_path} must contain a mapping at the top level")
    try:
        return RuntimeConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError and our own ValueErrors
        raise ConfigError(
            f"{file_path} is not a valid GundiX runtime configuration:\n{exc}"
        ) from exc
