"""Tests for scripts/at_restore.py, the at-sign mark/unmark helper.

Context: the model gateway redacts an at-sign immediately followed by a dotted name inside a
tool-call argument, which invalidates the request. Agents therefore write the marker
``AT_MARK_`` where a decorator's at-sign belongs and convert on disk. ``mark`` and ``unmark``
must be exact inverses or agents would corrupt source files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "at_restore.py"

AT_SIGN = chr(64)


def load_module():
    spec = importlib.util.spec_from_file_location("at_restore", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["at_restore"] = module
    spec.loader.exec_module(module)
    return module


at_restore = load_module()


def test_mark_and_unmark_are_exact_inverses() -> None:
    original = f"{AT_SIGN}pytest.fixture\nx = 1\n{AT_SIGN}pytest.mark.parametrize('p', [1])\n"
    assert at_restore.unmark(at_restore.mark(original)) == original


def test_mark_removes_every_literal_at_sign() -> None:
    marked = at_restore.mark(f"a{AT_SIGN}b\n{AT_SIGN}property\n")
    assert AT_SIGN not in marked
    assert marked.count(at_restore.MARKER) == 2


def test_unmark_restores_the_at_sign_only_from_the_marker() -> None:
    restored = at_restore.unmark(f"{at_restore.MARKER}pytest.fixture\n")
    assert restored == f"{AT_SIGN}pytest.fixture\n"


def test_helper_is_safe_against_its_own_unmark() -> None:
    """The script must survive a run over itself, or agents lose the tool."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert at_restore.unmark(source) == source


def test_round_trip_on_a_real_test_file_is_lossless(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    original = f"{AT_SIGN}pytest.fixture\ndef writer():\n    pass\n"
    target.write_text(original, encoding="utf-8")

    assert at_restore.main(["mark", str(target)]) == 0
    assert AT_SIGN not in target.read_text(encoding="utf-8")

    assert at_restore.main(["unmark", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == original


def test_check_reports_counts_without_rewriting(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "sample.py"
    target.write_text(f"{AT_SIGN}property\n", encoding="utf-8")

    assert at_restore.main(["check", str(target)]) == 0
    out = capsys.readouterr().out
    assert "literal_at_signs=1" in out
    assert "markers=0" in out
    assert target.read_text(encoding="utf-8") == f"{AT_SIGN}property\n"


def test_missing_path_is_skipped_not_fatal(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert at_restore.main(["mark", str(tmp_path / "absent.py")]) == 0
    assert "skip (missing)" in capsys.readouterr().err


def test_directory_walk_filters_by_suffix(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(f"{AT_SIGN}pytest.fixture\n", encoding="utf-8")
    (tmp_path / "b.bin").write_text(f"{AT_SIGN}pytest.fixture\n", encoding="utf-8")

    assert at_restore.main(["mark", str(tmp_path)]) == 0
    assert AT_SIGN not in (tmp_path / "a.py").read_text(encoding="utf-8")
    assert AT_SIGN in (tmp_path / "b.bin").read_text(encoding="utf-8")
