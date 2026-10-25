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
    enable_cover: bool = True  # cross-series covers: thresholds + buckets settling on the same number
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
class EdgeSettings:
    """Calibration edges: live markets in a price band where the atlas found Kalshi wrong after fees (and the
    newest weeks, unseen by the search, agreed). Dormant until an atlas with validated edges exists."""

    enabled: bool = True
    atlas_path: str = ""  # default <state_dir>/atlas.json, rebuilt by `omen atlas`
    max_atlas_age_days: int = 21  # an older atlas is ignored: markets change
    prior_sd: float = 0.03  # skepticism: true mispricings are usually small, so measured ones are shrunk toward 0
    min_confidence: float = 0.90  # P(after-fee edge > 0)
    min_ev: float = 0.01  # dollars per contract after fees
    kelly_fraction: float = 0.25  # stake = this x full Kelly x bankroll
    bankroll_usd: Decimal = Decimal("1000")
    max_capital_usd: Decimal = Decimal("200")


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
class LiquiditySettings:
    """Liquidity-reward market making (``omen lp``): rest bids where Kalshi pays liquidity rewards.

    paper (default) fills virtual orders from the public tape; live places real post-only orders and also needs
    KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH and OMEN_LIVE_ACK."""

    enabled: bool = True
    mode: str = "paper"  # paper | live
    cycle_seconds: int = 60
    max_markets: int = 100
    sides: str = "cheap"  # cheap: one bid on the cheaper side; both: a bid on each side
    min_quote_price: float = 0.02
    max_quote_price: float = 0.50
    capital_per_market_usd: float = 25.0  # resting bids plus inventory, per market
    max_total_capital_usd: float = 1000.0
    max_series_share: float = 0.30  # no series (often one news event) gets more than this share of the capital
    max_contracts_per_order: int = 500
    min_reward_per_day_usd: float = 5.0
    min_program_hours: float = 12.0  # skip 15-minute and hourly programs (fast markets pick resting orders off)
    stop_before_close_minutes: int = 720  # fills in the last 12 hours before close lost ~7c/contract in history
    # fast markets, plus markets that resolve on what someone says or on insider-decided outcomes (fills lost
    # 23-73c per contract-day there in the settled-market replay)
    exclude_series: str = (
        r"15M$|^KXTEMP\w*H$|RATEH$|^KXAAAGAS|^KXDIESEL|MATCH$|^KXCRYPTOLEAD|GAME$|SPREAD|TOTAL"
        r"|MENTION|SAY|TRUTHSOCIAL|ELIMINATION|^KXVMA|NETFLIX"
    )
    # quote only where the expected reward beats the pessimistic fill cost: settled cheap-side fills cost 2.6c (1.6 to
    # 3.9) per contract-day across 7,054 rewarded markets (docs/LIQUIDITY.md section 4), so require 4c
    min_reward_per_contract_day: float = 0.04
    # complete the empty side of decided markets with 1c bids (see docs/LIQUIDITY.md "Dead sides"); the loss is capped
    # at 1c per contract filled and at dead_side_max_loss_usd per market
    complete_dead_sides: bool = True
    dead_side_max_loss_usd: float = 50.0
    dead_side_exclude_series: str = r"15M$|^KXTEMP\w*H$|RATEH$|^KXCRYPTOLEAD"
    # bid only while the trailing 24 h of sales into 1c dead-side bids, at 1c each, cost at most this share of the
    # reward the bid earns; with under 30 minutes of such bids seen, assume dead_side_flow_prior contracts a minute
    # (the median across 5,607 decided rewarded markets of Oct 17-23 was 2.8; the mean 9.0)
    dead_side_max_flow_ratio: float = 0.5
    dead_side_flow_prior: float = 3.0
    # a dead-side bid loses at most 1c a contract, so it may rest closer to the close than cheap-side quotes: with
    # the flow filter, stops of 12, 6, 3 and 1 h all made money in both halves of the replay; 6 h added 18% over 12 h
    dead_side_stop_before_close_minutes: int = 360
    summary_every_hours: int = 6


@dataclass
class Settings:
    scan: ScanSettings = field(default_factory=ScanSettings)
    lock: LockSettings = field(default_factory=LockSettings)
    fees: FeeSettings = field(default_factory=FeeSettings)
    crypto: CryptoModelSettings = field(default_factory=CryptoModelSettings)
    edge: EdgeSettings = field(default_factory=EdgeSettings)
    gates: ValidationGates = field(default_factory=ValidationGates)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    paper: PaperSettings = field(default_factory=PaperSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    liquidity: LiquiditySettings = field(default_factory=LiquiditySettings)

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
    web_url: str | None = None  # the OMEN website the Pi publishes its live feed to
    ingest_secret: str | None = None  # shared HMAC secret (same value in Vercel)
    vercel_bypass: str | None = None  # only if Vercel Deployment Protection covers the production URL
    publish_interval_seconds: int = 120
    project_root: Path = field(default_factory=lambda: Path.cwd())

    @property
    def database(self) -> Path:
        return self.db_path or (self.state_dir / "omen.sqlite3")


SECTIONS = {
    "edge": EdgeSettings,
    "scan": ScanSettings,
    "lock": LockSettings,
    "fees": FeeSettings,
    "crypto": CryptoModelSettings,
    "gates": ValidationGates,
    "alerts": AlertSettings,
    "paper": PaperSettings,
    "execution": ExecutionSettings,
    "liquidity": LiquiditySettings,
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
    settings.web_url = (env.get("OMEN_WEB_URL") or "").strip().rstrip("/") or None
    settings.ingest_secret = env.get("OMEN_INGEST_SECRET") or None
    settings.vercel_bypass = env.get("OMEN_VERCEL_BYPASS") or None
    if env.get("OMEN_PUBLISH_SECONDS"):
        try:
            settings.publish_interval_seconds = max(30, int(env["OMEN_PUBLISH_SECONDS"]))
        except ValueError:
            raise ConfigError("OMEN_PUBLISH_SECONDS must be a whole number of seconds") from None
    if settings.web_url and not settings.web_url.startswith("https://"):
        raise ConfigError("OMEN_WEB_URL must start with https:// (the feed carries your plays)")
    if settings.web_url and len(settings.ingest_secret or "") < 16:
        raise ConfigError("OMEN_WEB_URL is set but OMEN_INGEST_SECRET is missing or shorter than 16 characters")
    if env.get("OMEN_EXECUTION_MODE"):
        settings.execution.mode = env["OMEN_EXECUTION_MODE"].strip().lower()
    if settings.execution.mode not in {"off", "demo", "live"}:
        raise ConfigError("execution mode must be off, demo or live")
    if env.get("OMEN_LP_MODE"):
        settings.liquidity.mode = env["OMEN_LP_MODE"].strip().lower()
    lp = settings.liquidity
    if lp.mode not in {"paper", "live"}:
        raise ConfigError("[liquidity].mode must be paper or live")
    if lp.sides not in {"cheap", "both"}:
        raise ConfigError("[liquidity].sides must be cheap or both")
    if not 0 < lp.max_series_share <= 1:
        raise ConfigError("[liquidity].max_series_share must be in (0, 1]")
    if not 0 < lp.min_quote_price <= lp.max_quote_price < 1:
        raise ConfigError("[liquidity] quote prices must satisfy 0 < min_quote_price <= max_quote_price < 1")
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
