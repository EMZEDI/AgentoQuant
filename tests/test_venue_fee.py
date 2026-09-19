"""The dry-run venue must not simulate a cheaper fee than the account's tier.

freqtrade honours a top-level ``fee`` in dry-run and otherwise charges the exchange's published
schedule. ccxt reports 0.26% for BTC/USD, the account sits on Tier 1 at 0.40% maker, so the default
flatters every fill by 0.14 points a side - and a soak that reports a P&L the account can never earn
is worse than no soak. ``assert_dry_run_config`` therefore fails closed unless the config states a
fee at or above the current tier's maker rate. Pessimism is allowed; optimism is not.

Deliberately decorator-free: the model gateway refuses a request whose history carries a tool call
containing an at-sign followed by a dotted name, so agent-written test files avoid decorators.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentoquant.config_loader import load_fee_tiers, repo_root
from agentoquant.execution.freqtrade_strategy import (
    CONFIG_FILENAME,
    USER_DATA_DIRNAME,
    BridgeConfigError,
    assert_dry_run_config,
)


def shipped_config() -> dict[str, Any]:
    path = repo_root() / USER_DATA_DIRNAME / CONFIG_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def write_config(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / CONFIG_FILENAME
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def current_maker_rate() -> float:
    tiers = load_fee_tiers()
    return float(tiers.tier(tiers.current_tier).maker_pct) / 100.0


def test_the_shipped_config_declares_a_fee_at_or_above_the_tier() -> None:
    config = shipped_config()
    assert "fee" in config, "the dry-run config must state a fee or the venue charges ccxt's number"
    assert float(config["fee"]) >= current_maker_rate()


def test_the_shipped_config_passes_its_own_assertion() -> None:
    assert_dry_run_config(repo_root() / USER_DATA_DIRNAME / CONFIG_FILENAME)


def test_a_config_with_no_fee_is_refused(tmp_path: Path) -> None:
    config = shipped_config()
    config.pop("fee")
    with pytest.raises(BridgeConfigError, match="no fee is configured"):
        assert_dry_run_config(write_config(tmp_path, config))


def test_ccxt_s_own_btc_rate_is_refused(tmp_path: Path) -> None:
    """0.0026 is what ccxt reports for BTC/USD; it is below the account's tier and must be refused."""
    config = shipped_config()
    config["fee"] = 0.0026
    with pytest.raises(BridgeConfigError, match="flatter every fill"):
        assert_dry_run_config(write_config(tmp_path, config))


def test_an_exactly_matching_fee_is_accepted(tmp_path: Path) -> None:
    config = shipped_config()
    config["fee"] = current_maker_rate()
    assert_dry_run_config(write_config(tmp_path, config))


def test_a_pessimistic_fee_is_accepted(tmp_path: Path) -> None:
    """Charging the taker rate for everything is allowed: it can only understate the P&L."""
    config = shipped_config()
    tiers = load_fee_tiers()
    config["fee"] = float(tiers.tier(tiers.current_tier).taker_pct) / 100.0
    assert_dry_run_config(write_config(tmp_path, config))


def test_the_existing_dry_run_checks_still_fire(tmp_path: Path) -> None:
    not_dry = shipped_config()
    not_dry["dry_run"] = False
    with pytest.raises(BridgeConfigError, match="dry_run is not true"):
        assert_dry_run_config(write_config(tmp_path, not_dry))

    keyed = shipped_config()
    keyed["exchange"]["key"] = "DUMMY_NOT_A_SECRET"
    with pytest.raises(BridgeConfigError, match="belong in the environment"):
        assert_dry_run_config(write_config(tmp_path, keyed))

    wrong_venue = shipped_config()
    wrong_venue["exchange"]["name"] = "binance"
    with pytest.raises(BridgeConfigError, match="exchange.name is not kraken"):
        assert_dry_run_config(write_config(tmp_path, wrong_venue))
