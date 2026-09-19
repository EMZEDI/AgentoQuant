"""Early-signal listeners (Task 4): exchange listing feeds, on-chain webhooks, GitHub releases,
Google News headlines, Telegram previews and the runner that supervises them all.

Task 4 acceptance criteria (``tasks/todo.md``):
  - a new Kraken or Bybit listing appears in the ledger within one minute of the announcement
  - on-chain webhook events arrive and are attributed to a token within two blocks
  - every early signal carries a source class (exchange, on-chain, official handle, release,
    headline) for the Verifier

Task 4 verification steps:
  - replay of three past listing days shows the signal ahead of the first article
  - one week of listener uptime with restarts logged

Every test here is offline. HTTP goes through a local recording transport that replays canned
bodies (``RecordingTransport``); ledger writes go to a DuckDB file under ``tmp_path``. Nothing
places an order, moves funds, or speaks MTProto - the listeners are read-only by construction.

GAP, REPORTED NOT HIDDEN: the on-chain receiver measures and returns a per-event block lag but
nothing enforces the "within two blocks" bound, and the frozen ``EarlySignalPayload`` has no
block column (the block number lives inside ``raw_text_or_ref``). See
``test_onchain_block_lag_is_reported_but_not_enforced`` and ``test_onchain_block_is_not_a_ledger_column``.
"""

from __future__ import annotations

import json
import threading
import time
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentoquant.data.early_signals import (
    EVENT_TYPES,
    PRODUCER_ROLE_PREFIX,
    HttpFetcher,
    JsonlLog,
    Listener,
    ListenerStats,
    SignalEvent,
    SignalWriter,
    listener_cycle_id,
    utcnow,
)
from agentoquant.data.early_signals import (
    bybit_listings,
    github_releases,
    google_news_rss,
    kraken_listings,
    okx_listings,
    telegram_previews,
)
from agentoquant.data.early_signals import onchain_webhooks as onchain
from agentoquant.data.early_signals import runner as runner_module
from agentoquant.enums import Harness, ModelFamily, SourceClass, Stage
from agentoquant.ledger.store import LedgerStore

# ----------------------------------------------------------------------------------------------
# Offline HTTP: a recording transport plus a fetcher that can only reach it
# ----------------------------------------------------------------------------------------------


class RecordingTransport(httpx.BaseTransport):
    """Records every request and replays a canned response per URL path. Never touches the network.

    ``routes`` maps a URL path to ``(status, body)``; ``body`` may be a str (sent as text), bytes or
    a JSON-serialisable object.
    """

    def __init__(self, routes: dict[str, tuple[int, Any]] | None = None) -> None:
        self.routes: dict[str, tuple[int, Any]] = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def add(self, path: str, status: int, body: Any) -> RecordingTransport:
        self.routes[path] = (status, body)
        return self

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": f"no canned route for {request.url.path}"})
        status, body = route
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def query_params(self) -> list[dict[str, str]]:
        return [dict(request.url.params) for request in self.requests]


def make_fetcher(transport: RecordingTransport, *, base_url: str = "", **kwargs: Any) -> HttpFetcher:
    """An :class:`HttpFetcher` whose only client is the canned transport."""
    return HttpFetcher(
        base_url=base_url,
        client=httpx.Client(transport=transport),
        rate_limiter=None,
        max_retries=int(kwargs.pop("max_retries", 1)),
        sleep=lambda _seconds: None,
        **kwargs,
    )


# ----------------------------------------------------------------------------------------------
# Ledger helpers
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """A real DuckDB ledger under ``tmp_path``: the repo's ``data/ledger.duckdb`` is never touched."""
    ledger = LedgerStore(db_path=tmp_path / "ledger.duckdb")
    yield ledger
    ledger.close()


@pytest.fixture
def log(tmp_path: Path) -> JsonlLog:
    """The listener JSON-lines log, under ``tmp_path`` rather than ``logs/``."""
    return JsonlLog(tmp_path / "early_signals.jsonl")


@pytest.fixture
def writer(store: LedgerStore, log: JsonlLog) -> SignalWriter:
    return SignalWriter(store, log=log)


def signal_rows(store: LedgerStore, cycle_id: str | None = None) -> list[dict]:
    """Every ``early_signal`` row, oldest first, optionally filtered to one cycle."""
    sql = (
        'SELECT "record_id" AS record_id, "cycle_id" AS cycle_id, "stage" AS stage, "ts" AS ts, '
        '"producer_role" AS producer_role, "producer_model_family" AS family, "harness" AS harness, '
        '"source_class" AS source_class, "ticker" AS ticker, "event_type" AS event_type, '
        '"raw_text_or_ref" AS raw_text_or_ref, "detected_at" AS detected_at, '
        '"latency_seconds_vs_first_article" AS latency FROM "early_signal"'
    )
    params: list[Any] = []
    if cycle_id is not None:
        sql += ' WHERE "cycle_id" = ?'
        params.append(cycle_id)
    return store.query(sql + ' ORDER BY "ts", "record_id"', params)


def log_events(log: JsonlLog, name: str | None = None) -> list[dict]:
    """Log records, optionally only those with a given ``event`` name."""
    records = log.tail(limit=5000)
    return [record for record in records if name is None or record.get("event") == name]


# __SENTINEL__
