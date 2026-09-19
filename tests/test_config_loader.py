"""Config loading and validation: the committed config must be valid, and invalid config must fail closed.

Acceptance criterion (Task 1): "Config (settings, sources, worldview, fee tier) is loaded from files
and validated on start".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentoquant import config_loader as cfg
from agentoquant.config_loader import (
    CREDENTIAL_NAMES,
    REQUIRED_ROLES,
    ConfigError,
    DisallowedModelError,
    FeeTiers,
    Models,
    Worldview,
    credentials,
    load_fee_tiers,
    load_models,
    load_risk_limits,
    load_settings,
    load_sleeves,
    load_sources,
    load_worldview,
    validate_all,
)
from agentoquant.enums import Sleeve
from tests.conftest import write_secret_file

# ----------------------------------------------------------------------------------------------
# The committed config is valid
# ----------------------------------------------------------------------------------------------


def test_validate_all_loads_every_config() -> None:
    loaded = validate_all()
    assert set(loaded) == {
        "settings",
        "sources",
        "models",
        "risk_limits",
        "sleeves",
        "fee_tiers",
        "worldview",
    }


def test_settings_match_the_plan() -> None:
    settings = load_settings()
    assert settings.cadence == "hourly"
    assert settings.cadence_minutes == 60
    assert settings.timezone == "America/Toronto"
    assert settings.daily_report_time == "10:00"
    assert settings.venue.exchange == "kraken"
    assert settings.venue.mode == "paper"
    assert settings.venue.dry_run is True
    assert settings.llm.monthly_cost_cap_cad == 130.0
    assert settings.human_in_the_loop.veto_window_minutes == 10


def test_settings_rejects_an_unknown_timezone() -> None:
    with pytest.raises(ValidationError):
        cfg.Settings.model_validate({"timezone": "Mars/Olympus_Mons"})


def test_settings_rejects_a_malformed_report_time() -> None:
    with pytest.raises(ValidationError):
        cfg.Settings.model_validate({"daily_report_time": "10:00am"})


def test_every_source_is_usable() -> None:
    sources = load_sources().sources
    assert sources, "at least one source must be declared"
    for name, spec in sources.items():
        assert spec.base_url.startswith("https://"), name
        assert spec.read_only is True, f"{name} must be read-only"
        # SourceQuota guarantees at least one ceiling; assert it here so the guarantee is visible.
        assert any(
            (
                spec.quota.calls_per_minute,
                spec.quota.calls_per_hour,
                spec.quota.calls_per_day,
                spec.quota.monthly_credits,
            )
        ), name
        for credential in spec.credentials:
            assert credential in CREDENTIAL_NAMES, f"{name} references an unknown credential name"


def test_sources_carry_the_verified_kraken_quirks() -> None:
    kraken = load_sources().sources["kraken_rest"]
    joined = " ".join(kraken.endpoints)
    assert "/0/private/Ledgers" in joined
    assert "/0/private/TradeVolume" in joined
    # A write endpoint must never be declared: the agent places orders only through freqtrade.
    for forbidden in ("AddOrder", "CancelOrder", "AmendOrder", "Withdraw"):
        assert forbidden not in joined


def test_sources_reject_a_raw_key_value() -> None:
    """A pasted secret in sources.yaml must fail validation, not be stored."""
    with pytest.raises(ValidationError):
        cfg.SourceSpec.model_validate(
            {
                "kind": "exchange",
                "base_url": "https://api.kraken.com",
                "credentials": ["AbCdEf123456SecretKeyValue"],
                "quota": {"calls_per_minute": 1},
            }
        )


def test_models_routes_every_required_role() -> None:
    models = load_models()
    for role in REQUIRED_ROLES:
        assert role in models.roles, f"{role} is not routed in models.yaml"
        assert models.roles[role].profile == "small"


def test_models_use_the_verified_slugs() -> None:
    models = load_models()
    assert models.roles["proposer_deepseek"].model == "deepseek/deepseek-v4.1-flash"
    assert models.roles["judge"].model == "anthropic/claude-sonnet-5"
    assert models.roles["adversary"].model == "z-ai/glm-5.3-flash"
    # The Adversary must run on a different family than the leading proposal sample.
    assert models.roles["adversary"].family != models.roles["proposer_deepseek"].family


def test_fee_tiers_match_kraken_july_2026() -> None:
    tiers = load_fee_tiers()
    assert tiers.current_tier == "tier_1"
    assert tiers.backtest_default_tier == "tier_2"
    assert (tiers.current.maker_pct, tiers.current.taker_pct) == (0.40, 0.80)
    assert (tiers.tier("tier_2").maker_pct, tiers.tier("tier_2").taker_pct) == (0.30, 0.60)
    assert (tiers.tier("tier_4").maker_pct, tiers.tier("tier_4").taker_pct) == (0.20, 0.35)
    assert (tiers.stablecoin_and_fx.maker_pct, tiers.stablecoin_and_fx.taker_pct) == (0.20, 0.20)
    volumes = [tier.volume_30d_usd_min for tier in tiers.tiers]
    assert volumes == sorted(volumes)


def test_sleeves_match_the_plan() -> None:
    sleeves = load_sleeves()
    a = sleeves.get(Sleeve.A)
    assert (a.floor_pct, a.position_cap_pct, a.entry_confidence) == (40, 15, 60)
    b = sleeves.get(Sleeve.B)
    assert (b.cap_pct, b.position_cap_pct, b.entry_confidence) == (35, 15, 65)
    c = sleeves.get(Sleeve.C)
    assert (c.cap_pct, c.position_cap_pct, c.entry_confidence) == (20, 5, 75)
    # Sleeve C opens only after A and B prove positive (Task 27).
    assert c.enabled is False
    assert c.usage_filter_required is True


def test_risk_limits_match_the_plan() -> None:
    limits = load_risk_limits()
    assert (limits.positions.min_concurrent, limits.positions.max_concurrent) == (3, 5)
    assert limits.positions.max_position_pct == 15
    assert limits.halts.daily_loss_halt_pct == 3
    assert limits.halts.weekly_drawdown_halt_pct == 8
    assert limits.halts.weekly_halt_requires_human_restart is True
    assert limits.execution.post_only_default is True
    assert limits.execution.mandatory_stop_on_exchange is True
    assert limits.regulatory.ontario_net_buy_cap_cad == 30000
    assert limits.regulatory.ontario_net_buy_window_months == 12
    # Shahrad's standing monthly cap on funding requests.
    assert limits.funding.monthly_request_cap >= 1


def test_worldview_holds_five_standing_priors() -> None:
    worldview = load_worldview()
    assert len(worldview.priors) == 5
    assert all(0.0 <= prior.bayesian_prior_mean <= 1.0 for prior in worldview.priors)
    assert worldview.regime_rules, "the regime overlay must be encoded as rules, not only prose"


# ----------------------------------------------------------------------------------------------
# Invalid config fails closed
# ----------------------------------------------------------------------------------------------


def test_models_rejects_an_opus_family() -> None:
    with pytest.raises(DisallowedModelError):
        Models.model_validate(
            {
                "roles": {
                    "judge": {
                        "family": "claude_opus",
                        "model": "anthropic/claude-opus-9",
                        "harness": "claude_native",
                    }
                }
            }
        )


def test_models_rejects_an_opus_model_id_under_an_allowed_family() -> None:
    with pytest.raises(DisallowedModelError):
        Models.model_validate(
            {
                "roles": {
                    "judge": {
                        "family": "claude_sonnet",
                        "model": "anthropic/claude-opus-9",
                        "harness": "claude_native",
                    }
                }
            }
        )


def test_models_rejects_a_big_profile() -> None:
    with pytest.raises(DisallowedModelError):
        Models.model_validate(
            {
                "roles": {
                    "judge": {
                        "family": "claude_sonnet",
                        "model": "anthropic/claude-sonnet-5",
                        "harness": "claude_native",
                        "profile": "big",
                    }
                }
            }
        )


def test_models_rejects_a_family_outside_the_enum() -> None:
    with pytest.raises(DisallowedModelError):
        Models.model_validate(
            {
                "roles": {
                    "judge": {
                        "family": "gpt_omega",
                        "model": "openai/gpt-omega",
                        "harness": "hermes_openrouter",
                    }
                }
            }
        )


def test_models_rejects_a_missing_required_role() -> None:
    with pytest.raises(ValidationError):
        Models.model_validate({"roles": {}})


def test_worldview_rejects_fewer_than_five_priors() -> None:
    with pytest.raises(ValidationError):
        Worldview.model_validate(
            {
                "doc_version": "1.0.0",
                "effective_from": "2026-09-18",
                "priors": [{"id": "only_one", "text": "x", "bayesian_prior_mean": 0.5}],
            }
        )


def test_fee_tiers_reject_an_undeclared_current_tier() -> None:
    with pytest.raises(ValidationError):
        FeeTiers.model_validate(
            {
                "effective_from": "2026-07-09",
                "current_tier": "tier_9",
                "backtest_default_tier": "tier_1",
                "tiers": [
                    {
                        "id": "tier_1",
                        "name": "Tier 1",
                        "maker_pct": 0.40,
                        "taker_pct": 0.80,
                        "volume_30d_usd_min": 0,
                    }
                ],
                "stablecoin_and_fx": {"maker_pct": 0.20, "taker_pct": 0.20},
            }
        )


def test_missing_config_file_fails_closed(temp_config_root) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "missing config file" in str(excinfo.value)


def test_unknown_key_fails_closed(temp_config_root) -> None:
    (temp_config_root / "config" / "settings.yaml").write_text(
        "version: 1\ncadence: hourly\nnot_a_real_key: 5\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "settings.yaml" in str(excinfo.value)


# ----------------------------------------------------------------------------------------------
# Credentials: present, private, never echoed
# ----------------------------------------------------------------------------------------------


def test_credentials_are_loaded_and_never_revealed() -> None:
    values = credentials()
    assert "KRAKEN_API_KEY" in values
    rendered = repr(values)
    assert "values=<redacted>" in rendered
    # No value may appear in the repr, however short.
    for name, value in values.items():
        if value:
            assert value not in rendered, f"{name}'s value leaked into repr"


def test_credentials_reject_a_permissive_mode(tmp_path, monkeypatch) -> None:
    path = write_secret_file(tmp_path / "credentials.env", 0o644)
    monkeypatch.setattr(cfg, "CREDENTIALS_PATH", path)
    with pytest.raises(ConfigError) as excinfo:
        credentials()
    assert "too permissive" in str(excinfo.value)


def test_credentials_reject_a_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "CREDENTIALS_PATH", tmp_path / "nope.env")
    with pytest.raises(ConfigError) as excinfo:
        credentials()
    assert "missing credentials file" in str(excinfo.value)


def test_credentials_parse_quotes_and_comments(tmp_path, monkeypatch) -> None:
    path = write_secret_file(
        tmp_path / "credentials.env",
        0o600,
        '# a comment\nKRAKEN_API_KEY="quoted value"\n\nTWELVE_DATA_API_KEY=plain\n',
    )
    monkeypatch.setattr(cfg, "CREDENTIALS_PATH", path)
    values = credentials()
    assert values["KRAKEN_API_KEY"] == "quoted value"
    assert values["TWELVE_DATA_API_KEY"] == "plain"
