"""Settings: code defaults, overlaid by a TOML file, overlaid by environment variables (secrets live only in env).

Unknown TOML keys are an error, so a typo in a threshold can never silently fall back to a default.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


@dataclass
class ScanSettings:
    hot_interval_seconds: int = 300
    hot_horizon_hours: int = 72
    full_interval_seconds: int = 3600
    events_refresh_seconds: int = 1800
    series_refresh_seconds: int = 21600
    fee_changes_refresh_seconds: int = 1800
    settle_interval_seconds: int = 1800
    model_interval_seconds: int = 60
    max_requests_per_second: float = 8.0
    min_minutes_to_close: int = 3
    request_timeout_seconds: float = 30.0


@dataclass
class LockSettings:
    """Thresholds for structural (contract-logic) arbitrage."""

    min_profit_usd: Decimal = Decimal("1.00")
    min_return_pct: Decimal = Decimal("0.75")
    min_apr_pct: Decimal = Decimal("15")
    max_capital_usd: Decimal = Decimal("1000")
    max_contracts_per_leg: Decimal = Decimal("5000")
    # A "guaranteed" return above this is far more likely a data/rules inconsistency than free money: such plays go
    # to #kalshi-status for a manual check instead of #kalshi-plays (no paper position, never executed).
    max_plausible_return_pct: Decimal = Decimal("20")
    whole_contracts: bool = True
    enable_no_basket: bool = True
    enable_ladder: bool = True
    enable_yes_partition: bool = True
    # Buckets reported in steps ("59-60", "61-62" whole degrees; "84,999.99"/"85,000" cents) leave one-step gaps.
    allow_step_gap_partitions: bool = True


@dataclass
class FeeSettings:
    # Kalshi's published formula rounds the fee up to the next cent; direct members are aligned to $0.0001 with
    # rebates. Rounding each fill up to a full cent is the conservative (never understated) choice.
    rounding_increment: Decimal = Decimal("0.01")
    schedule_file: str = "config/fee_schedule.toml"
    # When a fee change takes effect within this window, price with the worse of the current and scheduled fee.
    change_guard_minutes: int = 60
    # Remind the owner to re-verify the schedule constants against the official PDF after this many days.
    verify_reminder_days: int = 30


@dataclass
class CryptoModelSettings:
    enabled: bool = True
    # Series -> underlying. Spot comes from Coinbase/Kraken/Bitstamp (median), implied vol from Deribit DVOL.
    series: dict[str, str] = field(
        default_factory=lambda: {
            "KXBTCD": "BTC",
            "KXBTC": "BTC",
            "KXETHD": "ETH",
            "KXETH": "ETH",
            "KXSOLD": "SOL",
            "KXSOLE": "SOL",
            "KXXRPD": "XRP",
            "KXDOGED": "DOGE",
        }
    )
    min_minutes_to_close: int = 5
    max_hours_to_close: int = 36
    vol_safety_multiplier: float = 1.30
    vol_floor_annual: float = 0.25
    student_t_dof: float = 4.0
    spot_basis_band_pct: float = 0.15
    max_spot_age_seconds: int = 30
    min_edge: float = 0.03
    # High confidence = favorites only: even the worst-case model must give the bought side this probability.
    min_conservative_probability: float = 0.85
    max_price: Decimal = Decimal("0.985")
    max_capital_usd: Decimal = Decimal("200")
    # one logged prediction per market per interval (plus every would-alert signal, at most every 30 minutes);
    # near-certain markets (central P outside 2-98%) are only logged when they would alert
    prediction_log_interval_minutes: int = 360


@dataclass
class ValidationGates:
    """A model's alerts reach #kalshi-plays only after its shadow record clears every gate and the owner promotes it."""

    min_resolved_signals: int = 150
    min_distinct_events: int = 30
    min_after_fee_roi_lower_bound: float = 0.0
    bootstrap_confidence: float = 0.95
    max_calibration_error: float = 0.05
    bootstrap_samples: int = 2000


@dataclass
class AlertSettings:
    cooldown_minutes: int = 60
    improvement_pct: Decimal = Decimal("25")
    post_gone_updates: bool = True
    digest_hour_et: int = 20
    max_alerts_per_cycle: int = 10
    max_research_alerts_per_hour: int = 12  # shadow-model signals in #kalshi-research


@dataclass
class PaperSettings:
    enabled: bool = True


@dataclass
class ExecutionSettings:
    mode: str = "off"  # off | demo | live
    max_cost_per_play_usd: Decimal = Decimal("50")
    max_daily_cost_usd: Decimal = Decimal("200")
    allow_models: bool = False
    kill_switch_file: str = "KILL"


@dataclass
class Settings:
    scan: ScanSettings = field(default_factory=ScanSettings)
    lock: LockSettings = field(default_factory=LockSettings)
    fees: FeeSettings = field(default_factory=FeeSettings)
    crypto: CryptoModelSettings = field(default_factory=CryptoModelSettings)
    gates: ValidationGates = field(default_factory=ValidationGates)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    paper: PaperSettings = field(default_factory=PaperSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)

    # runtime / environment (never from TOML)
    base_url: str = PROD_BASE_URL
    state_dir: Path = field(default_factory=lambda: Path.home() / ".omen")
    db_path: Path | None = None
    healthcheck_url: str | None = None
    discord_webhooks: dict[str, str] = field(default_factory=dict)
    discord_bot_token: str | None = None
    discord_channel_map: dict[str, str] = field(default_factory=dict)
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    live_ack: str | None = None
    project_root: Path = field(default_factory=lambda: Path.cwd())

    @property
    def database(self) -> Path:
        return self.db_path or (self.state_dir / "omen.sqlite3")


SECTIONS = {
    "scan": ScanSettings,
    "lock": LockSettings,
    "fees": FeeSettings,
    "crypto": CryptoModelSettings,
    "gates": ValidationGates,
    "alerts": AlertSettings,
    "paper": PaperSettings,
    "execution": ExecutionSettings,
}


class ConfigError(ValueError):
    pass


def _coerce(name: str, hint: Any, value: Any) -> Any:
    if hint is Decimal:
        return Decimal(str(value))
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true/false")
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{name} must be a number")
        return float(value)
    if hint is str:
        return str(value)
    if getattr(hint, "__origin__", None) is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a table")
        return {str(k): str(v) for k, v in value.items()}
    return value


def _apply_section(obj: Any, data: dict[str, Any], section: str) -> None:
    hints = get_type_hints(type(obj))
    known = {f.name for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown setting [{section}].{key}")
        setattr(obj, key, _coerce(f"[{section}].{key}", hints[key], value))


def load_settings(config_path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    settings = Settings()
    root = Path(env.get("OMEN_PROJECT_ROOT", Path.cwd()))
    settings.project_root = root

    path = config_path or env.get("OMEN_CONFIG")
    if path is None and (root / "config" / "omen.toml").exists():
        path = root / "config" / "omen.toml"
    if path is not None:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for section, value in data.items():
            if section not in SECTIONS:
                raise ConfigError(f"unknown section [{section}]")
            if not isinstance(value, dict):
                raise ConfigError(f"[{section}] must be a table")
            _apply_section(getattr(settings, section), value, section)

    if env.get("OMEN_KALSHI_BASE_URL"):
        settings.base_url = env["OMEN_KALSHI_BASE_URL"].rstrip("/")
    if env.get("OMEN_STATE_DIR"):
        settings.state_dir = Path(env["OMEN_STATE_DIR"]).expanduser()
    if env.get("OMEN_DB"):
        settings.db_path = Path(env["OMEN_DB"]).expanduser()
    settings.healthcheck_url = env.get("OMEN_HEALTHCHECK_URL") or None

    webhooks: dict[str, str] = {}
    if env.get("OMEN_DISCORD_WEBHOOKS"):
        webhooks.update(_json_map(env["OMEN_DISCORD_WEBHOOKS"], "OMEN_DISCORD_WEBHOOKS"))
    if env.get("DISCORD_WEBHOOK_URL"):
        webhooks.setdefault("default", env["DISCORD_WEBHOOK_URL"])
    settings.discord_webhooks = webhooks
    settings.discord_bot_token = env.get("DISCORD_BOT_TOKEN") or None
    if env.get("OMEN_DISCORD_CHANNEL_MAP"):
        settings.discord_channel_map = _json_map(env["OMEN_DISCORD_CHANNEL_MAP"], "OMEN_DISCORD_CHANNEL_MAP")

    settings.kalshi_api_key_id = env.get("KALSHI_API_KEY_ID") or None
    settings.kalshi_private_key_path = env.get("KALSHI_PRIVATE_KEY_PATH") or None
    settings.live_ack = env.get("OMEN_LIVE_ACK") or None
    if env.get("OMEN_EXECUTION_MODE"):
        settings.execution.mode = env["OMEN_EXECUTION_MODE"].strip().lower()
    if settings.execution.mode not in {"off", "demo", "live"}:
        raise ConfigError("execution mode must be off, demo or live")
    return settings


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a systemd-style EnvironmentFile: KEY=VALUE lines, # comments, optional surrounding quotes."""
    out: dict[str, str] = {}
    for raw in Path(path).expanduser().read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def _json_map(raw: str, name: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(v, (str, int)) for v in value.values()):
        raise ConfigError(f"{name} must be a JSON object of strings")
    return {str(k): str(v) for k, v in value.items()}
