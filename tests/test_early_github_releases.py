"""Per-module tests for ``agentoquant.data.early_signals.github_releases`` (Task 4, Phase 0).

Offline by construction: the fetcher is the canned stub from ``tests/test_early_signals.py`` (imported,
never duplicated) and the ledger is a scratch DuckDB file under ``tmp_path``. No decorators in this file
(see the at-sign rule in ``.hermes.md``): the scratch ledger, the release fixtures and the listener
factory are plain helpers.

Coverage map: the parser against a real-shaped body, drafts and prereleases, the ``created_at`` fallback,
malformed / empty / wrong-type bodies, the per-repo failure isolation (a dead repo must not hide a live
one), the ``GITHUB_TOKEN`` header (optional, never logged, never in the URL), the quota arithmetic
behind the 10-minute interval, ``poll_once`` ledger writes, dedupe, failure accounting with backoff and
recovery, and close().
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agentoquant.data.early_signals import JsonlLog, SignalWriter, utcnow
from agentoquant.data.early_signals.github_releases import (
    BASE_URL,
    CALLS_PER_HOUR,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_REPOS,
    RELEASES_PATH,
    GithubReleasesListener,
    parse_github_releases,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import StubFetcher, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
REPO = "RaoFoundation/bittensor"
GOOD_REPO_PATH = RELEASES_PATH.format(owner="RaoFoundation", repo="bittensor")
MISSING_REPO = "akash-network/node"
MISSING_REPO_PATH = RELEASES_PATH.format(owner="akash-network", repo="node")

RELEASES: list[dict[str, Any]] = [
    {
        "tag_name": "v8.0.0",
        "name": "Bittensor 8.0.0",
        "html_url": "https://github.com/RaoFoundation/bittensor/releases/tag/v8.0.0",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-15T12:00:00Z",
    },
    {
        "tag_name": "v8.0.1-rc1",
        "name": "Bittensor 8.0.1 rc1",
        "html_url": "https://github.com/RaoFoundation/bittensor/releases/tag/v8.0.1-rc1",
        "draft": False,
        "prerelease": True,
        "published_at": "2026-09-16T12:00:00Z",
    },
    {
        "tag_name": "v8.1.0",
        "name": "unreleased draft",
        "html_url": "https://github.com/RaoFoundation/bittensor/releases/tag/v8.1.0",
        "draft": True,
        "prerelease": False,
        "published_at": "2026-09-17T12:00:00Z",
    },
]


class ExplodingFetcher:
    """A fetcher that raises a non-``FetchError``: the failure the listener does not isolate.

    A dead repo raises :class:`FetchError` and is caught per repo. Anything else - a bug, a schema
    surprise - escapes ``poll()`` and is accounted as a poll failure with backoff.
    """

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def get_text(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        raise self.error

    def get_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.error

    def close(self) -> None:
        return None


def scratch_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> LedgerStore:
    """A real DuckDB ledger under ``tmp_path``, plus the env override for implicit stores."""
    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env-ledger.duckdb"))
    return LedgerStore(db_path=tmp_path / "ledger.duckdb")


def writer_for(store: LedgerStore, tmp_path: Any) -> SignalWriter:
    return SignalWriter(store, log=JsonlLog(tmp_path / "early_signals.jsonl"))


def make_listener(writer: SignalWriter, fetcher: Any, **kwargs: Any) -> GithubReleasesListener:
    """A GitHub listener over one canned repo, logging to the writer's log."""
    kwargs.setdefault("repos", ((REPO, "TAO"),))
    kwargs.setdefault("log", writer.log)
    return GithubReleasesListener(writer, fetcher=fetcher, **kwargs)


# ----------------------------------------------------------------------------------------------
# The parser: real-shaped, drafts, prereleases, malformed, empty, wrong-type
# ----------------------------------------------------------------------------------------------


def test_parse_github_releases_reads_the_documented_shape() -> None:
    events = parse_github_releases(RELEASES, ticker="TAO", repo=REPO, observed_at=OBSERVED_AT)

    assert len(events) == 2  # the draft is not public yet
    assert [event.event_type for event in events] == ["release", "release"]
    assert [event.source_class for event in events] == [SourceClass.GITHUB_RELEASE] * 2
    assert [event.ticker for event in events] == ["TAO", "TAO"]
    assert events[0].raw_text_or_ref == "https://github.com/RaoFoundation/bittensor/releases/tag/v8.0.0"
    assert events[0].detected_at == PUBLISHED_AT
    assert events[0].published_at == PUBLISHED_AT
    assert events[0].observed_at == OBSERVED_AT
    assert events[0].source == "github_releases"


def test_parse_github_releases_can_exclude_prereleases() -> None:
    """A release candidate is a release for the Verifier only if the caller says so."""
    events = parse_github_releases(RELEASES, include_prereleases=False)
    assert [event.raw_text_or_ref for event in events] == [RELEASES[0]["html_url"]]


def test_parse_github_releases_falls_back_to_created_at_then_to_observed_at() -> None:
    payload = [
        {"tag_name": "v1", "html_url": "https://x/v1", "created_at": "2026-09-14T09:00:00Z"},
        {"tag_name": "v2", "html_url": "https://x/v2", "published_at": "not a timestamp"},
        {"tag_name": "v3", "html_url": "https://x/v3"},
    ]
    events = parse_github_releases(payload, observed_at=OBSERVED_AT)
    assert events[0].detected_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert events[1].detected_at == OBSERVED_AT
    assert events[1].published_at is None
    assert events[2].detected_at == OBSERVED_AT


def test_parse_github_releases_handles_malformed_empty_and_wrong_type_bodies() -> None:
    for payload in [None, {}, "nope", 42, b"[]", {"message": "Not Found"}]:
        assert parse_github_releases(payload, observed_at=OBSERVED_AT) == [], payload
    assert parse_github_releases([], observed_at=OBSERVED_AT) == []
    for payload in [
        [None, 7, "nope"],
        [{"draft": True, "html_url": "https://x/draft"}],
        [{"tag_name": "", "html_url": ""}],
        [{"tag_name": "", "html_url": "", "name": "no reference at all"}],
    ]:
        assert parse_github_releases(payload, observed_at=OBSERVED_AT) == [], payload


def test_parse_github_releases_cites_the_repo_and_tag_when_there_is_no_url() -> None:
    events = parse_github_releases([{"tag_name": "v9.9.9"}], repo=REPO, observed_at=OBSERVED_AT)
    assert events[0].raw_text_or_ref == f"{REPO} v9.9.9"
    assert events[0].dedupe_key == ("github_release", f"{REPO} v9.9.9")


# ----------------------------------------------------------------------------------------------
# The token header and the quota arithmetic
# ----------------------------------------------------------------------------------------------


def test_the_github_token_is_optional_and_never_in_the_url(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)

    anonymous = GithubReleasesListener(writer, repos=((REPO, "TAO"),))
    assert "Authorization" not in anonymous.fetcher.headers
    assert anonymous.fetcher.headers["Accept"] == "application/vnd.github+json"
    assert anonymous.fetcher.base_url == BASE_URL
    assert anonymous.fetcher.client is None, "constructing a listener must not open a socket"

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example_token")
    from_env = GithubReleasesListener(writer, repos=((REPO, "TAO"),))
    assert from_env.fetcher.headers["Authorization"] == "Bearer ghp_example_token"

    explicit = GithubReleasesListener(writer, repos=((REPO, "TAO"),), token="ghp_explicit")
    assert explicit.fetcher.headers["Authorization"] == "Bearer ghp_explicit"

    anonymous.close()
    from_env.close()
    explicit.close()


def test_the_default_interval_stays_inside_the_unauthenticated_quota() -> None:
    """4 repos every 600 s is 24 calls/hour, under GitHub's 30 calls/hour budget."""
    calls_per_hour = len(DEFAULT_REPOS) * (3600.0 / DEFAULT_INTERVAL_SECONDS)
    assert calls_per_hour <= CALLS_PER_HOUR
    assert DEFAULT_INTERVAL_SECONDS == 600.0
    assert len({repo for repo, _ticker in DEFAULT_REPOS}) == len(DEFAULT_REPOS)


# ----------------------------------------------------------------------------------------------
# Per-repo failure isolation
# ----------------------------------------------------------------------------------------------


def test_one_dead_repo_does_not_hide_a_live_one(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fetcher = StubFetcher(jsons={GOOD_REPO_PATH: RELEASES})
    listener = make_listener(writer, fetcher, repos=((REPO, "TAO"), (MISSING_REPO, "AKT")))

    written = listener.poll_once()

    assert len(written) == 2
    rows = signal_rows(store)
    assert [row["ticker"] for row in rows] == ["TAO", "TAO"]
    assert all(row["source_class"] == SourceClass.GITHUB_RELEASE.value for row in rows)
    assert all(row["producer_role"] == "listener_github_releases" for row in rows)
    assert listener.stats.failures == 0, "a dead repo is isolated, not a poll failure"
    assert listener.stats.consecutive_failures == 0

    failed = log_events(writer.log, "repo_failed")
    assert [record["repo"] for record in failed] == [MISSING_REPO]
    assert "HTTP 404" in failed[0]["error"]
    assert [call[1] for call in fetcher.calls] == [GOOD_REPO_PATH, MISSING_REPO_PATH]


def test_every_repo_being_down_is_an_empty_poll_not_a_crash(tmp_path: Any, monkeypatch: Any) -> None:
    """GitHub can be unreachable for all repos at once; the listener stays healthy and says so."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher(), repos=((REPO, "TAO"), (MISSING_REPO, "AKT")))

    assert listener.poll_once() == []
    assert listener.stats.failures == 0
    assert len(log_events(writer.log, "repo_failed")) == 2
    assert signal_rows(store) == []


# ----------------------------------------------------------------------------------------------
# The ledger write path and dedupe
# ----------------------------------------------------------------------------------------------


def test_poll_once_writes_release_rows_and_deduplicates_a_repeated_poll(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher(jsons={GOOD_REPO_PATH: RELEASES}))

    assert len(listener.poll_once()) == 2
    assert listener.poll_once() == []

    rows = signal_rows(store)
    assert len(rows) == 2
    assert rows[0]["event_type"] == "release"
    assert rows[0]["stage"] == "early_signal"
    assert rows[0]["cycle_id"].endswith("-listen")
    assert rows[0]["latency"] is None
    assert listener.stats.events == 4
    assert listener.stats.written == 2
    assert listener.stats.duplicates == 2
    assert log_events(writer.log, "poll_ok")[1]["duplicates"] == 2


def test_the_listener_passes_its_prerelease_setting_through(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer, StubFetcher(jsons={GOOD_REPO_PATH: RELEASES}), include_prereleases=False
    )
    assert len(listener.poll_once()) == 1
    assert [row["raw_text_or_ref"] for row in signal_rows(store)] == [RELEASES[0]["html_url"]]


# ----------------------------------------------------------------------------------------------
# Failure accounting: failures, consecutive_failures, backoff, recovery
# ----------------------------------------------------------------------------------------------


def test_an_unexpected_failure_is_counted_backed_off_and_recovered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, ExplodingFetcher(RuntimeError("schema surprise")))

    assert listener.poll_once() == []
    first_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert first_backoff > 0
    assert listener.stats.last_error is not None
    assert "RuntimeError" in listener.stats.last_error
    assert listener.stats.last_success_at is None
    assert signal_rows(store) == []

    assert listener.poll_once() == []
    assert listener.stats.failures == 2
    assert listener.stats.consecutive_failures == 2
    assert listener.stats.backoff_seconds > first_backoff
    assert listener.stats.backoff_seconds <= listener.max_backoff_seconds * 1.25
    assert [record["consecutive_failures"] for record in log_events(writer.log, "poll_failed")] == [1, 2]

    listener.fetcher = StubFetcher(jsons={GOOD_REPO_PATH: RELEASES})
    assert len(listener.poll_once()) == 2
    assert listener.stats.consecutive_failures == 0
    assert listener.stats.backoff_seconds == 0.0
    assert listener.stats.last_error is None
    assert listener.stats.last_success_at is not None
    assert listener.stats.last_success_at <= utcnow()


def test_close_releases_the_fetcher(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    stub = StubFetcher(jsons={GOOD_REPO_PATH: RELEASES})
    listener = make_listener(writer_for(store, tmp_path), stub)
    listener.close()
    assert stub.closed is True
