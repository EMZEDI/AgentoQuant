"""Data layer: connectors, quota manager, cache and the hourly ingest entry point.

Owned by Tasks 3 and 4. ``market_store.py`` is reserved for Task 7 and is deliberately not
created in Phase 0. Task 1 creates only this package marker.

What Task 3 adds here
---------------------
Two things live in this file rather than in a module of their own, because the file-ownership
matrix (``docs/phase0_implementation_plan.md`` section 4) gives Task 3 exactly nine connector
modules plus ``quota_manager.py``, ``cache.py`` and ``ingest.py``, and ``connectors/__init__.py``
belongs to Task 1:

* :class:`Transport` -- the single place every connector performs HTTP. It consults the cache first
  (a fresh entry costs no quota and no round trip), reserves the call with
  :class:`~agentoquant.data.quota_manager.QuotaManager` **before** the request is sent, retries
  429/5xx with exponential backoff, records the exact cost when a response reports one (Grok X
  Search does), and stores the payload in the cache. A connector therefore cannot accidentally
  bypass the budget: there is no other HTTP path in the data layer.
* :class:`ConnectorResult` -- the shape every connector returns: one record per source/coin/series,
  carrying the fields for the ledger's ``raw_snapshot`` payload plus the bookkeeping the ingest layer
  needs (whether it came from cache, whether it is stale, and the error text when it is missing).

Secrets never pass through here: a connector reads its key with
:func:`agentoquant.config_loader.credentials` and puts it in a header or query parameter that is
never logged. :meth:`Transport.redact` scrubs any credential-looking value out of an error message
before it can reach a ledger record or a printed summary.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agentoquant.data.cache import Cache, cache_key
from agentoquant.data.quota_manager import QuotaExceeded, QuotaManager

#: Statuses worth another attempt: rate limits, upstream hiccups and gateway timeouts.
RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Never sleep longer than this inside one cycle, however long the backoff wants to be.
MAX_SLEEP_SECONDS = 90.0

#: Query-parameter and header names whose values must never reach a log, a ledger record or stdout.
SECRET_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "x-api-key",
        "authorization",
        "key",
        "token",
        "access_key",
        "secret",
    }
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.]{8,}"),
)


class SourceError(Exception):
    """One source failed. The ingest layer records it as missing and carries on."""

    def __init__(
        self,
        source: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.source = source
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"{source}: {message}")


class SafetyError(Exception):
    """A connector tried to build a request the project forbids (a Kraken write endpoint)."""


@dataclass
class RetryPolicy:
    """How many attempts and how long to wait between them."""

    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff: ``base * 2**attempt``, capped at ``max_delay``."""
        return min(self.max_delay, self.base_delay * (2**attempt))


@dataclass
class TransportResponse:
    """One answer from :class:`Transport`, cached or live."""

    status_code: int
    payload: Any
    url: str
    source: str
    from_cache: bool = False
    cost_usd: float = 0.0
    attempts: int = 1
    elapsed_ms: int = 0
    text: str = ""

    def field(self, *names: str, default: Any = None) -> Any:
        """The first present key from a mapping payload (handy for chained API shapes)."""
        if not isinstance(self.payload, dict):
            return default
        for name in names:
            if name in self.payload:
                return self.payload[name]
        return default


@dataclass
class ConnectorResult:
    """One ledger-bound reading: a source, a coin or series name, and its fields."""

    source: str
    coin_or_series: str
    fields: dict = field(default_factory=dict)
    quota_remaining: int = 0
    is_stale: bool = False
    error: str | None = None
    from_cache: bool = False
    calls: int = 0
    cost_usd: float = 0.0
    detail: str = ""

    @property
    def missing(self) -> bool:
        """True when the source could not be read this cycle (the ledger records ``is_stale``)."""
        return self.is_stale or self.error is not None

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "coin_or_series": self.coin_or_series,
            "quota_remaining": self.quota_remaining,
            "is_stale": self.is_stale,
            "from_cache": self.from_cache,
            "error": self.error,
            "field_names": sorted(self.fields),
        }


def redact(value: Any) -> str:
    """Scrub anything that looks like a credential out of a message bound for a log or the ledger."""
    text = str(value)
    for name in SECRET_PARAM_NAMES:
        text = re.sub(rf"(?i)({re.escape(name)}=)[^&\s\"']+", r"\1<redacted>", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


class Transport:
    """Cache-aware, quota-aware HTTP for every connector in the data layer."""

    def __init__(
        self,
        *,
        quota: QuotaManager,
        cache: Cache | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        retry: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        respect_min_interval: bool = True,
        recorder: Callable[[dict], None] | None = None,
    ) -> None:
        self.quota = quota
        self.cache = cache if cache is not None else Cache()
        self.client = client
        self.sleeper = sleeper or time.sleep
        self.retry = retry or RetryPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.respect_min_interval = respect_min_interval
        self.recorder = recorder
        self.calls = 0
        self.cache_hits = 0
        self.costs: dict[str, float] = {}

    # -- plumbing ----------------------------------------------------------------------------

    def now(self) -> datetime:
        moment = self.clock()
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)

    def _client(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _send(
        self,
        method: str,
        url: str,
        *,
        source: str = "",
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """The one place a request actually leaves the process. Subclasses replay instead."""
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._client().request(method, url, **kwargs)

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeper(min(seconds, MAX_SLEEP_SECONDS))

    def _wait_for_min_interval(self, source: str) -> None:
        if not self.respect_min_interval:
            return
        decision = self.quota.check(source, now=self.now())
        if not decision.allowed and decision.tightest == "min_interval":
            self._sleep(decision.retry_after_seconds)

    def _record(self, blob: dict) -> None:
        if self.recorder is not None:
            try:
                self.recorder(blob)
            except Exception:  # noqa: BLE001 - a recorder must never break a cycle
                return

    # -- the one public entry point ----------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        source: str,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any = None,
        content: str | bytes | None = None,
        ttl_seconds: int | None = None,
        force: bool = False,
        cache_discriminator: str | None = None,
        cost_from_response: Callable[[dict], float] | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | None = None,
        allow_statuses: Iterable[int] = (),
        use_cache: bool = True,
    ) -> TransportResponse:
        """Perform one call inside its budget, or answer from the cache.

        Raises :class:`~agentoquant.data.quota_manager.QuotaExceeded` when the budget is exhausted
        (before any request is sent) and :class:`SourceError` when the call itself fails. A status in
        ``allow_statuses`` is returned to the caller instead of raising, for endpoints whose 4xx is a
        fact to be recorded rather than an error (CryptoRank's 403 on the paid vesting endpoint).
        """
        policy = retry or self.retry
        key = cache_key(method, url, params if params is not None else json_body, cache_discriminator)
        ttl = int(ttl_seconds) if ttl_seconds is not None else 0
        if use_cache and not force and ttl > 0:
            entry = self.cache.get_fresh(source, key, now=self.now(), ttl_seconds=ttl)
            if entry is not None:
                self.cache_hits += 1
                self._record({"source": source, "url": url, "from_cache": True})
                return TransportResponse(
                    status_code=int(entry.meta.get("status_code", 200)),
                    payload=entry.value,
                    url=url,
                    source=source,
                    from_cache=True,
                    cost_usd=0.0,
                    attempts=0,
                )

        self._wait_for_min_interval(source)
        allowed = set(allow_statuses)
        started = time.monotonic()
        last_error: str = "no attempt made"

        for attempt in range(max(1, policy.attempts)):
            self.quota.acquire(source, now=self.now(), detail=f"{method} {url}")
            self.calls += 1
            try:
                response = self._send(
                    method,
                    url,
                    source=source,
                    params=params,
                    headers=headers,
                    json_body=json_body,
                    content=content,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}"
                if attempt + 1 < policy.attempts:
                    self._sleep(policy.delay_for(attempt))
                    continue
                self.quota.mark_failure(source, last_error, now=self.now())
                raise SourceError(source, last_error, retryable=True) from exc

            status = response.status_code
            self._record(
                {
                    "source": source,
                    "url": url,
                    "params": params,
                    "method": method,
                    "status_code": status,
                    "text": response.text,
                }
            )
            if status in RETRY_STATUSES and attempt + 1 < policy.attempts:
                last_error = f"HTTP {status}"
                self._sleep(policy.delay_for(attempt))
                continue
            if status >= 400 and status not in allowed:
                self.quota.mark_failure(source, f"HTTP {status}", now=self.now())
                raise SourceError(source, f"HTTP {status}", status_code=status)

            payload = self._decode(response)
            cost = 0.0
            if cost_from_response is not None and status < 400:
                cost = float(cost_from_response(payload))
                self.quota.add_cost(source, cost, now=self.now())
                self.costs[source] = self.costs.get(source, 0.0) + cost
            if use_cache and ttl > 0 and status < 400:
                self.cache.set(
                    source,
                    key,
                    payload,
                    ttl_seconds=ttl,
                    now=self.now(),
                    meta={"status_code": status, "url": url},
                )
            return TransportResponse(
                status_code=status,
                payload=payload,
                url=url,
                source=source,
                from_cache=False,
                cost_usd=cost,
                attempts=attempt + 1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                text=response.text,
            )
        self.quota.mark_failure(source, last_error, now=self.now())
        raise SourceError(source, last_error, retryable=True)

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """Parse a JSON body, tolerating an empty or non-JSON body (GDELT answers ``{}``)."""
        text = response.text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def get_json(self, url: str, **kwargs: Any) -> TransportResponse:
        return self.request("GET", url, **kwargs)

    def post_json(self, url: str, **kwargs: Any) -> TransportResponse:
        return self.request("POST", url, **kwargs)

    def stats(self) -> dict:
        """Counters for the run report: live calls, cache hits, per-source spend."""
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cost_usd": round(sum(self.costs.values()), 6),
            "cost_by_source": {key: round(value, 6) for key, value in sorted(self.costs.items())},
        }


class RecordingTransport(Transport):
    """A :class:`Transport` that writes every live response to a JSONL file.

    Used once to capture a real hourly snapshot; the accelerated 24-hour dry run replays that file
    with :class:`ReplayTransport`, so a day of cycles can be checked against the real response shapes
    without a day of waiting or a day of API spend.
    """

    def __init__(self, *args: Any, record_path: Path | str, **kwargs: Any) -> None:
        self.record_path = Path(record_path)
        super().__init__(*args, **kwargs)

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = super()._send(method, url, **kwargs)
        blob = {
            "source": kwargs.get("source"),
            "method": method,
            "url": url,
            "params": kwargs.get("params"),
            "status_code": response.status_code,
            "text": response.text,
        }
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(blob, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return response


class ReplayTransport(Transport):
    """A :class:`Transport` that answers from a recording instead of the network.

    Quota accounting, the cache, retries and backoff all still run for real: only the socket is
    replaced. That is what makes the accelerated dry run a quota test rather than a mock.
    """

    def __init__(self, *args: Any, recordings: list[dict], **kwargs: Any) -> None:
        self.recordings = list(recordings)
        self.served = 0
        super().__init__(*args, **kwargs)

    @classmethod
    def from_file(cls, path: Path | str, *args: Any, **kwargs: Any) -> ReplayTransport:
        rows: list[dict] = []
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return cls(*args, recordings=rows, **kwargs)

    def _lookup(self, method: str, url: str, params: dict | None) -> dict | None:
        for row in self.recordings:
            if (
                row.get("method") == method
                and row.get("url") == url
                and (row.get("params") or {}) == (params or {})
            ):
                return row
        return None

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        row = self._lookup(method, url, kwargs.get("params"))
        if row is None:
            request = httpx.Request(method, url, params=kwargs.get("params"))
            return httpx.Response(404, text='{"replay": "no recording for this request"}',
                                  request=request)
        self.served += 1
        request = httpx.Request(method, url, params=kwargs.get("params"))
        return httpx.Response(
            int(row.get("status_code", 200)),
            text=row.get("text") or "",
            request=request,
        )


__all__ = [
    "MAX_SLEEP_SECONDS",
    "RETRY_STATUSES",
    "SECRET_PARAM_NAMES",
    "Cache",
    "ConnectorResult",
    "QuotaExceeded",
    "QuotaManager",
    "RecordingTransport",
    "ReplayTransport",
    "RetryPolicy",
    "SafetyError",
    "SourceError",
    "Transport",
    "TransportResponse",
    "redact",
]
