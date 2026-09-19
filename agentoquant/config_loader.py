"""Config loading and validation for ``config/*.yaml``.

Frozen public API (``docs/phase0_implementation_plan.md`` section 5.2)::

    class ConfigError(Exception): ...
    class DisallowedModelError(ConfigError): ...
    load_settings() -> Settings
    load_sources() -> Sources
    load_models() -> Models
    load_risk_limits() -> RiskLimits
    load_sleeves() -> Sleeves
    load_fee_tiers() -> FeeTiers
    load_worldview() -> Worldview
    credentials() -> dict[str, str]
    repo_root() -> Path

Validation **fails closed**: a missing required key, an unknown key, a disallowed model family (an
Opus-class model or a "big" profile), a malformed value, or a missing/mis-permissioned credentials
file raises :class:`ConfigError` at startup with a message naming the offending key.

Secret hygiene: this module never prints, logs or echoes a credential *value*. ``credentials()``
returns a mapping whose ``repr`` is redacted, and error messages name the config key, never the value.
"""

from __future__ import annotations

import re
import stat
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agentoquant.enums import Harness, ModelFamily, Sleeve

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
CREDENTIALS_PATH = Path.home() / ".config" / "agentoquant" / "credentials.env"

#: The nine canonical credential variable names. Config files reference these names only; a value
#: never appears in a config file, a log line or a report.
CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "AGENTOQUANT_OPENROUTER_KEY",
        "TWELVE_DATA_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "CRYPTORANK_API_KEY",
        "COINGECKO_API_KEY",
        "ALCHEMY_API_KEY",
        "HELIUS_API_KEY",
    }
)

#: Hard bans, independent of what the config file declares. No Opus-class model, no "big" profile.
BANNED_FAMILIES: frozenset[str] = frozenset({"claude_opus", "opus", "claude-opus"})
BANNED_PROFILES: frozenset[str] = frozenset({"big", "large", "xlarge", "xxlarge"})
BANNED_MODEL_SUBSTRINGS: frozenset[str] = frozenset({"opus"})

#: Every role that must be routed in ``config/models.yaml`` (plan section "How a decision is made").
REQUIRED_ROLES: tuple[str, ...] = (
    "analyst_technical",
    "analyst_news_policy",
    "analyst_social_hype",
    "analyst_macro_geopolitics",
    "analyst_onchain_usage",
    "proposer_deepseek",
    "proposer_claude",
    "adversary",
    "judge",
    "reviewer_draft",
    "reviewer_signoff",
    "reflector",
)

_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    """A config file is missing, unreadable, malformed or invalid. Raised at startup."""


class DisallowedModelError(ConfigError):
    """A config names an Opus-class model or a "big" profile. Banned everywhere."""


class _Strict(BaseModel):
    """Base for every config model: an unknown key is an error, not something to ignore."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)


# --------------------------------------------------------------------------------------
# settings.yaml
# --------------------------------------------------------------------------------------


class CapitalSettings(_Strict):
    currency: Literal["CAD", "USD"] = "CAD"
    starting_capital: float = Field(default=10_000.0, gt=0)
    go_live_fraction: float = Field(default=0.25, gt=0, le=1)
    note: str | None = None


class VenueSettings(_Strict):
    exchange: Literal["kraken"] = "kraken"
    # Paper until Task 26. The value lives in the file so going live is a reviewed config change.
    mode: Literal["paper", "live"] = "paper"
    quote_currency: Literal["USD", "CAD"] = "USD"
    dry_run: bool = True


class LLMSettings(_Strict):
    monthly_cost_cap_cad: float = Field(default=130.0, gt=0)
    cost_currency: Literal["CAD"] = "CAD"
    meter: Literal["per_call"] = "per_call"
    read_exact_cost_from_response: bool = True
    note: str | None = None


class HumanInTheLoop(_Strict):
    telegram: bool = True
    veto_window_minutes: int = Field(default=10, ge=1, le=60)
    commands: list[str] = Field(
        default_factory=lambda: ["veto", "pause", "resume", "status", "why", "flat", "fund"]
    )


class Settings(_Strict):
    version: int = 1
    cadence: Literal["hourly"] = "hourly"
    cadence_minutes: int = Field(default=60, ge=1)
    timezone: str = "America/Toronto"
    daily_report_time: str = "10:00"
    capital: CapitalSettings = Field(default_factory=CapitalSettings)
    venue: VenueSettings = Field(default_factory=VenueSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    human_in_the_loop: HumanInTheLoop = Field(default_factory=HumanInTheLoop)
    ledger_path: str = "data/ledger.duckdb"
    log_dir: str = "logs"
    call_log_path: str = "logs/mcp_calls.jsonl"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("daily_report_time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not _HHMM_RE.match(value):
            raise ValueError("expected HH:MM in 24-hour form, e.g. '10:00'")
        return value


# --------------------------------------------------------------------------------------
# sources.yaml
# --------------------------------------------------------------------------------------


class SourceQuota(_Strict):
    """Call budget per source. At least one ceiling must be declared."""

    calls_per_minute: int | None = Field(default=None, ge=1)
    calls_per_hour: int | None = Field(default=None, ge=1)
    calls_per_day: int | None = Field(default=None, ge=1)
    monthly_credits: int | None = Field(default=None, ge=1)
    note: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> SourceQuota:
        if not any(
            (self.calls_per_minute, self.calls_per_hour, self.calls_per_day, self.monthly_credits)
        ):
            raise ValueError("declare at least one of calls_per_minute/calls_per_hour/calls_per_day")
        return self


class SourceSpec(_Strict):
    kind: Literal[
        "exchange",
        "market_data",
        "macro",
        "news",
        "geopolitics",
        "onchain",
        "usage",
        "unlocks",
        "social",
    ]
    base_url: str = Field(min_length=1)
    endpoints: list[str] = Field(default_factory=list)
    #: Environment variable NAMES only. A raw key value here fails validation.
    credentials: list[str] = Field(default_factory=list)
    keyless: bool = False
    enabled: bool = True
    read_only: bool = True
    quota: SourceQuota
    cache_ttl_seconds: int | None = Field(default=None, ge=0)
    notes: str | None = None

    @model_validator(mode="after")
    def _credentials_are_names(self) -> SourceSpec:
        for name in self.credentials:
            if name not in CREDENTIAL_NAMES:
                # Deliberately does not echo the value: a pasted raw key must not reach a log.
                raise ValueError(
                    "credentials must list environment variable names from the canonical nine "
                    "(KRAKEN_API_KEY, KRAKEN_API_SECRET, AGENTOQUANT_OPENROUTER_KEY, "
                    "TWELVE_DATA_API_KEY, ALPHA_VANTAGE_API_KEY, CRYPTORANK_API_KEY, "
                    "COINGECKO_API_KEY, ALCHEMY_API_KEY, HELIUS_API_KEY); raw key values must "
                    "never appear in a config file"
                )
        if self.keyless and self.credentials:
            raise ValueError("keyless sources must not list credentials")
        if not self.keyless and not self.credentials:
            raise ValueError("non-keyless sources must list at least one credential name")
        return self


class Sources(_Strict):
    version: int = 1
    sources: dict[str, SourceSpec]

    @model_validator(mode="after")
    def _non_empty(self) -> Sources:
        if not self.sources:
            raise ValueError("declare at least one source")
        return self


# --------------------------------------------------------------------------------------
# fee_tiers.yaml
# --------------------------------------------------------------------------------------


class FeeTier(_Strict):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    maker_pct: float = Field(ge=0)
    taker_pct: float = Field(ge=0)
    volume_30d_usd_min: float = Field(ge=0)
    volume_30d_usd_max: float | None = Field(default=None, ge=0)
    note: str | None = None

    @model_validator(mode="after")
    def _maker_not_above_taker(self) -> FeeTier:
        if self.maker_pct > self.taker_pct:
            raise ValueError("maker_pct must not exceed taker_pct")
        return self


class StablecoinAndFxFees(_Strict):
    maker_pct: float = Field(ge=0)
    taker_pct: float = Field(ge=0)
    applies_to: list[str] = Field(default_factory=list)
    note: str | None = None


class FeeTiers(_Strict):
    version: int = 1
    venue: Literal["kraken"] = "kraken"
    effective_from: date
    #: Tier the account is actually on. Code prefers the tier read live from Kraken TradeVolume.
    current_tier: str = Field(min_length=1)
    prefer_live_tier: bool = True
    backtest_default_tier: str = Field(min_length=1)
    tiers: list[FeeTier] = Field(min_length=1)
    stablecoin_and_fx: StablecoinAndFxFees
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> FeeTiers:
        ids = [t.id for t in self.tiers]
        if len(set(ids)) != len(ids):
            raise ValueError("tier ids must be unique")
        for key in ("current_tier", "backtest_default_tier"):
            if getattr(self, key) not in ids:
                raise ValueError(f"{key} {getattr(self, key)!r} is not one of the declared tier ids")
        ordered = sorted(self.tiers, key=lambda t: t.volume_30d_usd_min)
        if ordered != self.tiers:
            raise ValueError("tiers must be listed in ascending volume_30d_usd_min order")
        return self

    def tier(self, tier_id: str) -> FeeTier:
        for tier in self.tiers:
            if tier.id == tier_id:
                return tier
        raise ConfigError(f"unknown fee tier id: {tier_id!r}")

    @property
    def current(self) -> FeeTier:
        return self.tier(self.current_tier)


# --------------------------------------------------------------------------------------
# sleeves.yaml
# --------------------------------------------------------------------------------------


class SleeveSpec(_Strict):
    name: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    floor_pct: float = Field(default=0.0, ge=0, le=100)
    cap_pct: float = Field(default=100.0, ge=0, le=100)
    position_cap_pct: float = Field(gt=0, le=100)
    entry_confidence: int = Field(ge=0, le=100)
    coins: list[str] = Field(default_factory=list)
    cash: list[str] = Field(default_factory=list)
    enabled: bool = True
    usage_filter_required: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def _floor_within_cap(self) -> SleeveSpec:
        if self.floor_pct > self.cap_pct:
            raise ValueError("floor_pct must not exceed cap_pct")
        return self


class Sleeves(_Strict):
    version: int = 1
    sleeves: dict[Sleeve, SleeveSpec]

    @model_validator(mode="after")
    def _all_three_sleeves(self) -> Sleeves:
        missing = [s.value for s in Sleeve if s not in self.sleeves]
        if missing:
            raise ValueError(f"missing sleeve definition(s): {', '.join(missing)}")
        return self

    def get(self, sleeve: Sleeve) -> SleeveSpec:
        return self.sleeves[sleeve]


# --------------------------------------------------------------------------------------
# risk_limits.yaml
# --------------------------------------------------------------------------------------


class PositionLimits(_Strict):
    min_concurrent: int = Field(ge=1)
    max_concurrent: int = Field(ge=1)
    max_position_pct: float = Field(gt=0, le=100)
    sleeve_caps_pct: dict[Sleeve, float]

    @model_validator(mode="after")
    def _ordered(self) -> PositionLimits:
        if self.min_concurrent > self.max_concurrent:
            raise ValueError("min_concurrent must not exceed max_concurrent")
        missing = [s.value for s in Sleeve if s not in self.sleeve_caps_pct]
        if missing:
            raise ValueError(f"missing sleeve_caps_pct entry for sleeve(s): {', '.join(missing)}")
        return self


class TurnoverLimits(_Strict):
    #: Percent of book value traded per day (buy notional + sell notional). Initial value, tunable.
    daily_turnover_cap_pct: float = Field(gt=0, le=100)
    note: str | None = None


class HaltLimits(_Strict):
    daily_loss_halt_pct: float = Field(gt=0, le=100)
    weekly_drawdown_halt_pct: float = Field(gt=0, le=100)
    weekly_halt_requires_human_restart: bool = True
    daily_halt_resets_at: str = "00:00"

    @model_validator(mode="after")
    def _daily_tighter_than_weekly(self) -> HaltLimits:
        if self.daily_loss_halt_pct >= self.weekly_drawdown_halt_pct:
            raise ValueError("daily_loss_halt_pct must be below weekly_drawdown_halt_pct")
        return self


class ExecutionLimits(_Strict):
    post_only_default: bool = True
    mandatory_stop_on_exchange: bool = True
    max_reprices: int = Field(ge=0, le=10)
    market_orders_allowed_for: list[str] = Field(default_factory=list)
    #: The smallest notional the venue accepts, in quote currency. Below it an exit is not a small
    #: exit, it is no exit at all: freqtrade rounds the amount to zero and logs
    #: "Wanted to exit of ... but exit amount is now 0.0 due to exchange limits - not exiting".
    min_order_cost_usd: float = Field(default=5.0, gt=0)


class LiquidityLimits(_Strict):
    #: 24-hour quote volume below this and the coin is untradeable. Initial value, tunable.
    min_24h_volume_usd: float = Field(gt=0)
    max_spread_pct: float = Field(gt=0, le=10)


class CooldownLimits(_Strict):
    consecutive_losses: int = Field(ge=1)
    cooldown_hours: int = Field(ge=1)


class RegulatoryLimits(_Strict):
    ontario_net_buy_cap_cad: float = Field(gt=0)
    ontario_net_buy_window_months: int = Field(ge=1)


class FundingLimits(_Strict):
    free_cash_floor_pct: dict[Sleeve, float]
    monthly_request_cap: int = Field(ge=0)
    note: str | None = None

    @model_validator(mode="after")
    def _all_sleeves(self) -> FundingLimits:
        missing = [s.value for s in Sleeve if s not in self.free_cash_floor_pct]
        if missing:
            raise ValueError(f"missing free_cash_floor_pct entry for sleeve(s): {', '.join(missing)}")
        return self


class RiskLimits(_Strict):
    version: int = 1
    positions: PositionLimits
    turnover: TurnoverLimits
    halts: HaltLimits
    execution: ExecutionLimits
    liquidity: LiquidityLimits
    cooldown: CooldownLimits
    regulatory: RegulatoryLimits
    funding: FundingLimits


# --------------------------------------------------------------------------------------
# models.yaml
# --------------------------------------------------------------------------------------


class BannedModels(_Strict):
    families: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    model_substrings: list[str] = Field(default_factory=list)


class RoleModel(_Strict):
    family: ModelFamily
    model: str = Field(min_length=1)
    harness: Harness
    profile: str = "small"
    max_cost_usd_per_call: float | None = Field(default=None, gt=0)
    notes: str | None = None


class Models(_Strict):
    version: int = 1
    banned: BannedModels = Field(default_factory=BannedModels)
    roles: dict[str, RoleModel]

    @model_validator(mode="before")
    @classmethod
    def _reject_disallowed_models(cls, data: Any) -> Any:
        """Fail closed before pydantic coercion, so the message names the offending key."""
        if not isinstance(data, dict):
            return data
        banned = data.get("banned") or {}
        families = {str(x).lower() for x in banned.get("families", [])} | BANNED_FAMILIES
        profiles = {str(x).lower() for x in banned.get("profiles", [])} | BANNED_PROFILES
        substrings = (
            {str(x).lower() for x in banned.get("model_substrings", [])} | BANNED_MODEL_SUBSTRINGS
        )
        known_families = {m.value for m in ModelFamily}
        roles = data.get("roles") or {}
        if isinstance(roles, dict):
            for role, spec in roles.items():
                if not isinstance(spec, dict):
                    continue
                where = f"roles.{role}"
                family = str(spec.get("family", "")).lower()
                if family and family not in known_families:
                    raise DisallowedModelError(
                        f"{where}.family: {family!r} is not a ModelFamily member "
                        f"({', '.join(sorted(known_families))}); Opus-class families and 'big' "
                        f"profiles are banned everywhere"
                    )
                if family in families or any(s in family for s in substrings):
                    raise DisallowedModelError(f"{where}.family: {family!r} is disallowed")
                model = str(spec.get("model", "")).lower()
                if any(s in model for s in substrings):
                    raise DisallowedModelError(f"{where}.model: disallowed model id for role {role!r}")
                profile = str(spec.get("profile", "small")).lower()
                if profile in profiles:
                    raise DisallowedModelError(
                        f"{where}.profile: {profile!r} is disallowed; no big profile anywhere"
                    )
        return data

    @model_validator(mode="after")
    def _every_role_routed(self) -> Models:
        missing = [r for r in REQUIRED_ROLES if r not in self.roles]
        if missing:
            raise ValueError(f"roles missing from models.yaml: {', '.join(missing)}")
        return self

    def role(self, name: str) -> RoleModel:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise ConfigError(f"unknown role: {name!r}") from exc


# --------------------------------------------------------------------------------------
# worldview.yaml
# --------------------------------------------------------------------------------------


class Prior(_Strict):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    #: Prior mean handed to the Bayesian model (plan: priors are encoded numerically, not just text).
    bayesian_prior_mean: float = Field(ge=0, le=1)
    regime_rule: str | None = None
    applies_to: list[str] = Field(default_factory=list)


class RegimeRule(_Strict):
    id: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)


class Worldview(_Strict):
    version: int = 1
    doc_version: str = Field(min_length=1)
    effective_from: date
    #: The five standing priors are Shahrad's. The system may flag one, never edit one.
    priors: list[Prior] = Field(min_length=5)
    regime_rules: list[RegimeRule] = Field(default_factory=list)
    prohibitions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_priors(self) -> Worldview:
        ids = [p.id for p in self.priors]
        if len(set(ids)) != len(ids):
            raise ValueError("prior ids must be unique")
        return self


# --------------------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------------------


def repo_root() -> Path:
    """Repository root of the installed package (worktree root when running from a worktree)."""
    return REPO_ROOT


def _config_dir() -> Path:
    return repo_root() / "config"


def _credentials_path() -> Path:
    return CREDENTIALS_PATH


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: could not be read as YAML: {type(exc).__name__}") from exc
    if raw is None:
        raise ConfigError(f"{path}: empty config file")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping of keys")
    return raw


def _format_validation_error(exc: ValidationError, path: Path) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        lines.append(f"  {path}: {loc}: {err.get('msg', 'invalid')}")
    return "invalid config:\n" + "\n".join(lines)


def _load(filename: str, model: type[BaseModel]) -> Any:
    path = _config_dir() / filename
    data = _read_yaml(path)
    try:
        return model.model_validate(data)
    except DisallowedModelError:
        raise
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, path)) from exc


def load_settings() -> Settings:
    """``config/settings.yaml`` -> :class:`Settings` (cadence, capital, cost cap, timezone)."""
    return _load("settings.yaml", Settings)


def load_sources() -> Sources:
    """``config/sources.yaml`` -> :class:`Sources` (endpoints, quotas, credential key NAMES)."""
    return _load("sources.yaml", Sources)


def load_models() -> Models:
    """``config/models.yaml`` -> :class:`Models`, validated against :class:`ModelFamily`."""
    return _load("models.yaml", Models)


def load_risk_limits() -> RiskLimits:
    """``config/risk_limits.yaml`` -> :class:`RiskLimits` (the Risk Gate's hard limits)."""
    return _load("risk_limits.yaml", RiskLimits)


def load_sleeves() -> Sleeves:
    """``config/sleeves.yaml`` -> :class:`Sleeves` (caps and entry confidence per sleeve)."""
    return _load("sleeves.yaml", Sleeves)


def load_fee_tiers() -> FeeTiers:
    """``config/fee_tiers.yaml`` -> :class:`FeeTiers` (Kraken's table plus the current tier)."""
    return _load("fee_tiers.yaml", FeeTiers)


def load_worldview() -> Worldview:
    """``config/worldview.yaml`` -> :class:`Worldview` (versioned doc plus the standing priors)."""
    return _load("worldview.yaml", Worldview)


class _RedactedCredentials(dict):
    """``dict[str, str]`` whose ``repr``/``str`` never show a value."""

    def __repr__(self) -> str:
        return f"Credentials(keys={sorted(self.keys())!r}, values=<redacted>)"

    __str__ = __repr__


def credentials() -> dict[str, str]:
    """Read ``~/.config/agentoquant/credentials.env`` (mode 600, outside the repo).

    Returns the mapping only. Never logs, prints or echoes a value; the returned object's ``repr`` is
    redacted. A missing file or a too-permissive mode raises :class:`ConfigError`.
    """
    path = _credentials_path()
    if not path.exists():
        raise ConfigError(
            f"missing credentials file: {path} (mode 600, outside the repo). "
            "See .env.example for the variable names."
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path}: mode {mode:04o} is too permissive, expected 600")

    values: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}: line {lineno}: expected KEY=VALUE")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not _VAR_NAME_RE.match(key):
            # Names the line, never the content: a mistyped key could itself be a secret.
            raise ConfigError(f"{path}: line {lineno}: invalid variable name")
        values[key] = value
    return _RedactedCredentials(values)


def _assert_credentials_readable() -> None:
    """Startup check: the credentials file exists and is mode 600. Reads no values into a message."""
    path = _credentials_path()
    if not path.exists():
        raise ConfigError(f"missing credentials file: {path} (mode 600, outside the repo)")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path}: mode {mode:04o} is too permissive, expected 600")


def validate_all() -> dict[str, Any]:
    """Load and validate every config file plus the credentials file. Used at CLI/MCP startup."""
    loaded = {
        "settings": load_settings(),
        "sources": load_sources(),
        "models": load_models(),
        "risk_limits": load_risk_limits(),
        "sleeves": load_sleeves(),
        "fee_tiers": load_fee_tiers(),
        "worldview": load_worldview(),
    }
    _assert_credentials_readable()
    return loaded


__all__ = [
    "BANNED_FAMILIES",
    "BANNED_MODEL_SUBSTRINGS",
    "BANNED_PROFILES",
    "CONFIG_DIR",
    "CREDENTIAL_NAMES",
    "CREDENTIALS_PATH",
    "CapitalSettings",
    "ConfigError",
    "DisallowedModelError",
    "ExecutionLimits",
    "FeeTier",
    "FeeTiers",
    "HaltLimits",
    "LiquidityLimits",
    "Models",
    "PositionLimits",
    "Prior",
    "REQUIRED_ROLES",
    "RegimeRule",
    "RiskLimits",
    "RoleModel",
    "Settings",
    "SleeveSpec",
    "Sleeves",
    "SourceSpec",
    "Sources",
    "TurnoverLimits",
    "Worldview",
    "credentials",
    "load_fee_tiers",
    "load_models",
    "load_risk_limits",
    "load_settings",
    "load_sleeves",
    "load_sources",
    "load_worldview",
    "repo_root",
    "validate_all",
]
