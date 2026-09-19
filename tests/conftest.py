"""Shared pytest fixtures.

Tests run against the real ``config/*.yaml`` in the repo (they are committed artifacts, and validating
them is the point). Tests that need a temporary config tree or a temporary credentials file
monkeypatch the loader's module-level paths instead of touching the real ones.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentoquant import config_loader


@pytest.fixture
def repo_root() -> Path:
    return config_loader.repo_root()


@pytest.fixture
def call_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the shared call log to a temp file, so tests never write into the repo."""
    path = tmp_path / "mcp_calls.jsonl"
    monkeypatch.setenv("AGENTOQUANT_CALL_LOG", str(path))
    return path


@pytest.fixture
def temp_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config loader at an empty temp repo root, for missing/invalid-config tests."""
    monkeypatch.setattr(config_loader, "REPO_ROOT", tmp_path)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return tmp_path


def write_secret_file(path: Path, mode: int, body: str = "KRAKEN_API_KEY=abc\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path
