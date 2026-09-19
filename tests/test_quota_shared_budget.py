"""F14 and F15: one quota authority for every source that makes a request, and it is persisted.

The adversary review found two data-layer defects (``docs/reviews/phase0_adversary.md``):

* **F14** -- the early-signal layer bypassed the repo-wide quota manager entirely. Seven sources each
  built a local minimum-interval ``RateLimiter`` and never called ``QuotaManager.acquire``, so their
  declared ceilings were unenforced and ``quota.breaches()`` (the checkpoint's "no API quota
  breaches" evidence) was structurally silent about the one path that runs continuously.
* **F15** -- quota accounting was per process. ``QuotaManager`` memoised the journal on first use and
  never re-read it, so a second instance kept the budget it first loaded. The hourly ingest is a
  fresh ``oneshot`` process every tick and the listener runner is a long-lived one, so the same
  budget could be spent twice.

Every test here fails on the pre-fix code, and each names which finding it pins:

* the F14 tests assert that a listener over budget sends **no** request, that its calls are charged to
  its ``config/sources.yaml`` name, and that the runner wires all seven names into one manager;
* the F15 tests assert that a spend is visible to a second instance and to the **next process**, that
  a budget survives a restart, and that racing processes cannot exceed a ceiling between them.

Two deliberate properties of this file:

1. **No decorators.** The repo's at-sign rule (``.hermes.md``) kills an agent that emits a decorator in
   a tool call, so there are no fixtures and no ``parametrize``: ``tmp_path`` and ``monkeypatch``
   arrive as plain function arguments and the case tables are plain loops.
2. **No network.** HTTP goes through an in-process ``httpx`` transport; the only subprocesses are
   local Python interpreters spending a quota journal under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentoquant.config_loader import load_sources, repo_root
from agentoquant.data.early_signals import FetchError, HttpFetcher, RateLimiter, SignalWriter
from agentoquant.data.early_signals.bybit_listings import BybitListingsListener
from agentoquant.data.quota_manager import (
    DEFAULT_JOURNAL_PATH,
    JOURNAL_PATH_ENV,
    QuotaExceeded,
    QuotaManager,
    default_journal_path,
)
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A child process that spends a budget through the real ``QuotaManager`` and reports what it got.
#: Run with ``sys.executable`` so it is the same interpreter and the same package as the test.
SPEND_CHILD = """
import json
import sys

from agentoquant.data.quota_manager import QuotaExceeded, QuotaManager

journal, source, calls = sys.argv[1], sys.argv[2], int(sys.argv[3])
quota = QuotaManager(journal_path=journal)
spent, refused, reason = 0, False, ""
for _ in range(calls):
    try:
        quota.acquire(source)
        spent += 1
    except QuotaExceeded as exc:
        refused, reason = True, str(exc)
        break
print(json.dumps({"spent": spent, "refused": refused, "reason": reason}))
"""


class MockTransport(httpx.BaseTransport):
    """An in-process transport that records requests and answers a canned body. No network."""

    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.body = {"retCode": 0, "result": {"list": []}} if body is None else body
        self.status = status
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def count(self) -> int:
        return len(self.requests)


def start_child(journal: Path, source: str, calls: int) -> subprocess.Popen:
    """Start (do not wait for) a child process that tries to spend ``calls`` of ``source``."""
    return subprocess.Popen(
        [sys.executable, "-c", SPEND_CHILD, str(journal), source, str(calls)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def child_result(process: subprocess.Popen) -> dict:
    """Wait for a child from :func:`start_child` and parse the JSON line it printed."""
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, f"child failed: {stderr[-600:]}"
    return json.loads(stdout.strip().splitlines()[-1])


def spend_in_a_child_process(journal: Path, source: str, calls: int) -> dict:
    """Spend ``calls`` of ``source`` in a *separate process* over the same journal file."""
    return child_result(start_child(journal, source, calls))


def journal_lines(journal: Path) -> list[dict]:
    """Every journal line as a dict (an empty list when nothing has been written yet)."""
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]


def refused_lines(journal: Path, source: str | None = None) -> list[dict]:
    """The refusal lines, optionally for one source."""
    return [
        line
        for line in journal_lines(journal)
        if line.get("refused") and (source is None or line.get("source") == source)
    ]


def call_lines(journal: Path, source: str) -> list[dict]:
    """The lines that count as calls for one source."""
    return [
        line
        for line in journal_lines(journal)
        if line.get("source") == source and line.get("call")
    ]


def build_listener(
    tmp_path: Path,
    quota: QuotaManager,
    *,
    transport: MockTransport | None = None,
    source: str = "bybit_announcements",
) -> tuple[BybitListingsListener, MockTransport]:
    """A Bybit listener whose fetcher spends from ``quota`` and whose only client is in-process."""
    transport = transport if transport is not None else MockTransport()
    fetcher = HttpFetcher(
        base_url="https://api.bybit.com",
        client=httpx.Client(transport=transport),
        rate_limiter=RateLimiter(0.0),
        max_retries=1,
        sleep=lambda _seconds: None,
        quota=quota,
        source=source,
    )
    writer = SignalWriter(LedgerStore(db_path=tmp_path / "ledger.duckdb"))
    return BybitListingsListener(writer, fetcher=fetcher), transport


def scratch_ledger(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point the default ledger path at ``tmp_path`` so no test can reach ``data/ledger.duckdb``."""
    path = tmp_path / "env-ledger.duckdb"
    monkeypatch.setenv(DB_PATH_ENV, str(path))
    return path


# ----------------------------------------------------------------------------------------------
# F15 -- one persisted budget, shared by every process
# ----------------------------------------------------------------------------------------------


def test_two_managers_over_one_journal_share_the_budget(tmp_path: Path) -> None:
    """The F15 reproduction: the second instance must see the first instance's spend.

    On the pre-fix code ``second`` kept the view it loaded before ``first`` spent the budget, reported
    the full ceiling as remaining, and acquired anyway -- the budget was spent twice.
    """
    journal = tmp_path / "quota.jsonl"
    first = QuotaManager(journal_path=journal)
    second = QuotaManager(journal_path=journal)
    ceiling = int(first.limits("kraken_rest")["calls_per_minute"])

    assert second.remaining("kraken_rest") == ceiling  # second loads its view first

    for _ in range(ceiling):
        first.acquire("kraken_rest")

    assert first.remaining("kraken_rest") == 0
    assert second.remaining("kraken_rest") == 0, "a second instance must not still hold the budget"
    with pytest.raises(QuotaExceeded):
        second.acquire("kraken_rest")


def test_a_budget_spent_in_one_process_is_visible_to_the_next(tmp_path: Path) -> None:
    """A spend by another *process* is visible to a manager that had already loaded its view.

    The pre-fix manager memoised the journal on first use, so a spend made after that point was
    invisible: it reported the full ceiling as remaining and spent it a second time.
    """
    journal = tmp_path / "quota.jsonl"
    watcher = QuotaManager(journal_path=journal)
    assert watcher.remaining("gold_api") == 60  # the view is loaded before the other process spends

    result = spend_in_a_child_process(journal, "gold_api", 5)
    assert result["spent"] == 5
    assert not result["refused"]

    assert watcher.used("gold_api", 3600) == 5
    assert watcher.remaining("gold_api") == 60 - 5


def test_the_budget_survives_a_process_restart(tmp_path: Path) -> None:
    """Four calls spent before the restart count against the ten after it."""
    journal = tmp_path / "quota.jsonl"
    restarted = QuotaManager(journal_path=journal)  # the new process's manager, built first
    assert restarted.remaining("bybit_announcements") == 10  # and it has loaded its view
    assert spend_in_a_child_process(journal, "bybit_announcements", 4)["spent"] == 4

    for _ in range(6):
        restarted.acquire("bybit_announcements")
    assert restarted.remaining("bybit_announcements") == 0
    with pytest.raises(QuotaExceeded):
        restarted.acquire("bybit_announcements")


def test_racing_processes_cannot_spend_the_same_budget_twice(tmp_path: Path) -> None:
    """Four processes each try to take five calls of a ten-call ceiling: exactly ten are granted.

    This is what the exclusive lock on the journal buys. Without it two processes can both read
    "ten left" and both spend.
    """
    journal = tmp_path / "quota.jsonl"
    ceiling = int(QuotaManager(journal_path=journal).limits("bybit_announcements")["calls_per_minute"])
    processes = [start_child(journal, "bybit_announcements", 5) for _ in range(4)]
    granted = sum(child_result(process)["spent"] for process in processes)

    assert granted == ceiling, f"{granted} calls granted against a ceiling of {ceiling}"
    assert len(call_lines(journal, "bybit_announcements")) == ceiling


def test_a_refusal_is_recorded_and_visible_to_another_process(tmp_path: Path) -> None:
    """A refusal is a durable, shared fact: the reason is journalled and the next process counts it."""
    journal = tmp_path / "quota.jsonl"
    ceiling = int(QuotaManager(journal_path=journal).limits("gold_api")["calls_per_hour"])

    result = spend_in_a_child_process(journal, "gold_api", ceiling + 1)
    assert result["spent"] == ceiling
    assert result["refused"] is True
    assert "calls_per_hour" in result["reason"]

    refusals = refused_lines(journal, "gold_api")
    assert len(refusals) == 1
    assert refusals[0]["call"] is False, "a refusal must never count as a call"
    assert "quota refused" in refusals[0]["detail"]

    other_process = QuotaManager(journal_path=journal)
    assert other_process.breaches() == {"gold_api": 1}
    row = next(row for row in other_process.report() if row.source == "gold_api")
    assert row.breaches == 1
    assert "calls_per_hour" in row.last_refusal_reason


def test_a_refusal_does_not_extend_the_exhaustion(tmp_path: Path) -> None:
    """Refusals are journalled but never counted as calls, so they cannot push the window further out."""
    journal = tmp_path / "quota.jsonl"
    quota = QuotaManager(journal_path=journal)
    ceiling = int(quota.limits("bybit_announcements")["calls_per_minute"])
    for _ in range(ceiling):
        quota.acquire("bybit_announcements")
    for _ in range(3):
        with pytest.raises(QuotaExceeded):
            quota.acquire("bybit_announcements")

    assert quota.used("bybit_announcements", 60) == ceiling
    assert len(refused_lines(journal)) == 3


def test_both_processes_default_to_the_same_journal_file(tmp_path: Path, monkeypatch: Any) -> None:
    """The ingest process and the listener runner both construct ``QuotaManager()`` with no argument.

    They must therefore resolve to the same file, or "one authority" is only true per process.
    """
    monkeypatch.setenv(JOURNAL_PATH_ENV, str(tmp_path / "shared.jsonl"))
    assert default_journal_path() == tmp_path / "shared.jsonl"
    assert QuotaManager().journal_path == tmp_path / "shared.jsonl"

    monkeypatch.delenv(JOURNAL_PATH_ENV, raising=False)
    assert default_journal_path() == repo_root() / DEFAULT_JOURNAL_PATH
    assert QuotaManager().journal_path == repo_root() / DEFAULT_JOURNAL_PATH


# ----------------------------------------------------------------------------------------------
# F14 -- every source that makes a request reports to that one authority
# ----------------------------------------------------------------------------------------------


def test_an_over_budget_listener_sends_no_request(tmp_path: Path, monkeypatch: Any) -> None:
    """The F14 reproduction: with the budget exhausted, the listener must not fetch at all.

    On the pre-fix code the listener had no quota manager to consult and sent the request anyway,
    while ``breaches()`` stayed empty.
    """
    scratch_ledger(tmp_path, monkeypatch)
    journal = tmp_path / "quota.jsonl"
    quota = QuotaManager(journal_path=journal)
    ceiling = int(quota.limits("bybit_announcements")["calls_per_minute"])
    for _ in range(ceiling):
        quota.acquire("bybit_announcements")

    listener, transport = build_listener(tmp_path, quota)
    written = listener.poll_once()

    assert written == []
    assert transport.count == 0, "the request went out despite an exhausted budget"
    assert listener.stats.failures == 1
    assert "quota" in (listener.stats.last_error or "")
    assert quota.breaches() == {"bybit_announcements": 1}
    assert refused_lines(journal, "bybit_announcements")


def test_a_listener_call_is_charged_to_its_sources_yaml_name(tmp_path: Path, monkeypatch: Any) -> None:
    """A listener call is journalled under the name ``config/sources.yaml`` declares, with its ceiling."""
    scratch_ledger(tmp_path, monkeypatch)
    journal = tmp_path / "quota.jsonl"
    quota = QuotaManager(journal_path=journal)

    listener, transport = build_listener(tmp_path, quota)
    listener.poll_once()

    assert transport.count == 1
    assert quota.used("bybit_announcements", 60) == 1
    assert quota.breaches() == {}
    assert len(call_lines(journal, "bybit_announcements")) == 1
    assert quota.limits("bybit_announcements") == {"calls_per_minute": 10}


def test_the_listener_and_the_hourly_ingest_share_one_journal(tmp_path: Path, monkeypatch: Any) -> None:
    """A call the listener made is in the report a freshly built ingest-process manager reads."""
    scratch_ledger(tmp_path, monkeypatch)
    journal = tmp_path / "quota.jsonl"
    listener, _transport = build_listener(tmp_path, QuotaManager(journal_path=journal))
    listener.poll_once()

    ingest = QuotaManager(journal_path=journal)  # what ingest.py constructs for the hourly tick
    row = next(row for row in ingest.report() if row.source == "bybit_announcements")
    assert row.calls_1m == 1
    assert row.limits == {"calls_per_minute": 10}
    assert row.breaches == 0


def test_every_listener_the_runner_builds_reports_to_one_manager(tmp_path: Path, monkeypatch: Any) -> None:
    """``build_listeners`` wires one manager into every fetching listener, and the audit says so."""
    from agentoquant.data.early_signals.runner import build_listeners, quota_audit, quota_sources

    scratch_ledger(tmp_path, monkeypatch)
    journal = tmp_path / "quota.jsonl"
    quota = QuotaManager(journal_path=journal)
    writer = SignalWriter(LedgerStore(db_path=tmp_path / "ledger.duckdb"))

    listeners = build_listeners(writer, quota=quota, include_onchain=False)

    assert [listener.name for listener in listeners] == [
        "bybit_listings",
        "okx_listings",
        "kraken_listings",
        "github_releases",
        "google_news_rss",
        "telegram_previews",
    ]
    assert quota_audit(listeners) == [], "a listener fetches outside the shared quota manager"
    assert quota_sources(listeners) == [
        "bybit_announcements",
        "github_releases",
        "google_news_rss",
        "kraken_listings_rss",
        "kraken_rest",
        "okx_announcements",
        "telegram_previews",
    ]
    declared = load_sources().sources
    for name in quota_sources(listeners):
        assert name in declared, f"{name} is not a declared source"
        assert declared[name].quota is not None

    seen_managers = {
        id(fetcher.quota)
        for listener in listeners
        for fetcher in listener.fetchers()
    }
    assert seen_managers == {id(quota)}, "the listeners must share one manager, not one each"


def test_the_audit_names_an_unbudgeted_fetcher(tmp_path: Path, monkeypatch: Any) -> None:
    """The audit is not vacuous: a fetcher with no manager is named (the F14 shape)."""
    from agentoquant.data.early_signals.runner import quota_audit, quota_sources

    scratch_ledger(tmp_path, monkeypatch)
    writer = SignalWriter(LedgerStore(db_path=tmp_path / "ledger.duckdb"))
    unbudgeted = BybitListingsListener(
        writer,
        fetcher=HttpFetcher(base_url="https://api.bybit.com", client=httpx.Client(transport=MockTransport())),
    )

    assert quota_audit([unbudgeted]) == ["bybit_listings:https://api.bybit.com"]
    assert quota_sources([unbudgeted]) == []


def test_a_fetcher_without_a_manager_keeps_working_standalone(tmp_path: Path) -> None:
    """The quota hook is optional for a one-off fetch: no manager, no reservation, no crash."""
    transport = MockTransport()
    fetcher = HttpFetcher(
        base_url="https://api.bybit.com",
        client=httpx.Client(transport=transport),
        max_retries=1,
        sleep=lambda _seconds: None,
    )
    assert fetcher.get_json("/v5/announcements/index") == {"retCode": 0, "result": {"list": []}}
    assert transport.count == 1


def test_a_retry_spends_a_second_unit_of_the_budget(tmp_path: Path) -> None:
    """A retry is a second request, so it must be reserved again -- not billed once."""
    journal = tmp_path / "quota.jsonl"
    quota = QuotaManager(journal_path=journal)
    transport = MockTransport(status=503)
    fetcher = HttpFetcher(
        base_url="https://api.bybit.com",
        client=httpx.Client(transport=transport),
        rate_limiter=RateLimiter(0.0),
        max_retries=3,
        sleep=lambda _seconds: None,
        quota=quota,
        source="bybit_announcements",
    )
    with pytest.raises(FetchError):
        fetcher.get_json("/v5/announcements/index")

    assert transport.count == 3
    assert quota.used("bybit_announcements", 60) == 3
