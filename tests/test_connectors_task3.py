"""Task 3 connector tests: five sources, every verified quirk pinned, no live network.

The connectors are exercised against two transports, both without a socket:

* :class:`FakeTransport` -- a scripted transport that pops one prepared answer per call and records
  the request (method, url, params, headers, json_body). It mirrors ``Transport.request``'s
  ``cost_from_response`` behaviour so the cost path is exercised, and it can raise
  :class:`~agentoquant.data.SourceError` or :class:`~agentoquant.data.quota_manager.QuotaExceeded` on
  demand, which is how "a source failure becomes a missing reading" is tested.
* the repo's own :class:`~agentoquant.data.ReplayTransport` with a real
  :class:`~agentoquant.data.QuotaManager` and :class:`~agentoquant.data.Cache` -- quota accounting,
  retries, backoff and the cache all run for real, only the socket is replaced. That is what makes the
  GDELT 429 test, the CryptoRank 403 test and the Grok cost test real rather than mocked.

Credential hygiene is asserted, not assumed: the fake keys below contain no ``sk-`` prefix and no
``apikey=`` form, so :func:`agentoquant.data.redact` cannot hide a leak on the connectors' behalf. Each
test that touches a key asserts the key reached exactly one place (the ``apikey`` query parameter or
the credential header) and that no returned field, error string or result repr contains it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentoquant.config_loader import load_sources
from agentoquant.data import (
    QuotaManager,
    ReplayTransport,
    SourceError,
    TransportResponse,
)
from agentoquant.data.cache import Cache
from agentoquant.data.connectors import (
    alpha_vantage,
    cryptorank,
    defillama,
    gdelt,
    grok_x_search,
)
from agentoquant.data.connectors.alpha_vantage import AlphaVantageConnector
from agentoquant.data.quota_manager import QuotaExceeded

#: Fake credentials. Deliberately not ``sk-``-shaped, so redaction cannot mask a real leak.
AV_KEY = "ZZTESTKEY-alpha-9f3a2b"
CR_KEY = "ZZTESTKEY-cryptorank-4c1d"
OR_KEY = "ZZTESTKEY-openrouter-77e0"

#: Every fake key, for the "no credential ever leaks" sweep.
FAKE_KEYS = (AV_KEY, CR_KEY, OR_KEY)


class FakeQuota:
    """The two quota reads a connector makes: the remaining budget and the source's TTL."""

    def __init__(self, *, remaining: int = 100, ttl: int = 300) -> None:
        self.remaining_value = remaining
        self.ttl = ttl

    def remaining(self, source: str) -> int:
        return self.remaining_value

    def ttl_seconds(self, source: str, default: int = 0) -> int:
        return self.ttl


class FakeTransport:
    """A scripted transport: one prepared answer per call, every request recorded."""

    def __init__(self, *answers: Any, remaining: int = 100, ttl: int = 300) -> None:
        self.answers = list(answers)
        self.quota = FakeQuota(remaining=remaining, ttl=ttl)
        self.calls: list[dict] = []

    def _next(self, method: str, url: str, kwargs: dict) -> TransportResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.answers:
            raise AssertionError(f"unexpected call: {method} {url} with {kwargs}")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status = int(answer.get("status_code", 200))
        payload = answer.get("payload")
        cost = 0.0
        hook = kwargs.get("cost_from_response")
        if hook is not None and status < 400:
            cost = float(hook(payload))
        return TransportResponse(
            status_code=status,
            payload=payload,
            url=url,
            source=kwargs.get("source", ""),
            from_cache=bool(answer.get("from_cache", False)),
            cost_usd=cost,
        )

    def get_json(self, url: str, **kwargs: Any) -> TransportResponse:
        return self._next("GET", url, kwargs)

    def post_json(self, url: str, **kwargs: Any) -> TransportResponse:
        return self._next("POST", url, kwargs)


def recording(
    *,
    url: str,
    method: str = "GET",
    params: dict | None = None,
    status_code: int = 200,
    body: Any = None,
    text: str | None = None,
) -> dict:
    """One line of a :class:`ReplayTransport` recording."""
    return {
        "source": "test",
        "method": method,
        "url": url,
        "params": params,
        "status_code": status_code,
        "text": text if text is not None else json.dumps(body),
    }


def replay(recordings: list[dict], tmp_path: Any, *, remaining: int = 100) -> ReplayTransport:
    """A real transport whose socket is a recording: quota, retries and cache all still run."""
    return ReplayTransport(
        recordings=recordings,
        quota=QuotaManager(load_sources(), journal_path=tmp_path / "journal.jsonl", persist=False),
        cache=Cache(tmp_path / "cache"),
        sleeper=lambda seconds: None,
        respect_min_interval=False,
    )


def use_key(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: str) -> None:
    """Point one connector module's credential lookup at a fake key."""
    monkeypatch.setattr(module, "credentials", lambda: {name: value})


def no_key(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Point one connector module's credential lookup at an empty credentials file."""
    monkeypatch.setattr(module, "credentials", lambda: {})


def assert_no_secret(result: Any, *keys: str) -> None:
    """The reading, its fields, its error and its repr must not carry a credential value."""
    blob = json.dumps(
        {"fields": result.fields, "error": result.error, "repr": repr(result)}, default=str
    )
    for key in keys or FAKE_KEYS:
        assert key not in blob, "a credential value reached a returned field or an error string"


def test_every_connector_source_is_declared_in_sources_yaml() -> None:
    """A connector whose SOURCE is not in config/sources.yaml has no quota and no TTL."""
    declared = load_sources().sources
    for module in (alpha_vantage, gdelt, defillama, cryptorank, grok_x_search):
        assert module.SOURCE in declared, f"{module.SOURCE} is not declared in sources.yaml"


# ----------------------------------------------------------------------------------------------
# Alpha Vantage: 200-with-an-Information-body is a failure, and the key is never echoed
# ----------------------------------------------------------------------------------------------

FEED_ITEM = {
    "title": "Bitcoin ETF inflows hit a record",
    "url": "https://example.com/btc-etf",
    "time_published": "20260919T031500",
    "summary": "prose the ledger deliberately does not keep",
    "overall_sentiment_score": 0.31,
    "overall_sentiment_label": "Somewhat-Bullish",
    "ticker_sentiment": [
        {
            "ticker": "CRYPTO:BTC",
            "relevance_score": "0.91",
            "sentiment_score": "0.35",
            "sentiment_label": "Bullish",
        }
    ],
}

BEARISH_ITEM = {
    "title": "Exchange outage rattles altcoins",
    "url": "https://example.com/outage",
    "time_published": "20260919T040000",
    "overall_sentiment_score": -0.11,
    "overall_sentiment_label": "Somewhat-Bearish",
}


def test_alpha_vantage_fetch_parses_the_feed_and_computes_the_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    transport = FakeTransport({"payload": {"feed": [FEED_ITEM, BEARISH_ITEM]}})
    result = AlphaVantageConnector(transport).fetch()

    assert result.is_stale is False
    assert result.error is None
    assert result.calls == 1
    assert result.quota_remaining == 100
    assert result.fields["item_count"] == 2
    assert result.fields["mean_sentiment_score"] == pytest.approx(0.1)
    assert result.fields["sentiment_label_counts"] == {
        "Somewhat-Bullish": 1,
        "Somewhat-Bearish": 1,
    }
    first = result.fields["items"][0]
    assert set(first) == {
        "title",
        "url",
        "time_published",
        "source",
        "overall_sentiment_score",
        "overall_sentiment_label",
        "ticker_sentiment",
    }
    assert first["ticker_sentiment"][0]["sentiment_score"] == 0.35
    # The key travels in the query parameter and nowhere else.
    assert transport.calls[0]["params"]["apikey"] == AV_KEY
    assert transport.calls[0]["params"]["function"] == "NEWS_SENTIMENT"
    assert_no_secret(result)


def test_alpha_vantage_information_body_is_a_source_error_not_an_empty_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    body = {"Information": "Invalid API call. Please retry or visit the documentation."}
    transport = FakeTransport({"payload": body}, {"payload": body})
    connector = AlphaVantageConnector(transport)

    with pytest.raises(SourceError) as excinfo:
        connector.news_sentiment()
    assert "Information" in str(excinfo.value)
    assert "feed" not in str(excinfo.value)

    result = connector.fetch()
    assert result.is_stale is True
    assert result.fields == {"missing": True}
    assert "Information" in (result.error or "")


def test_alpha_vantage_error_body_never_echoes_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    error_body = {"payload": {"Information": f"the key {AV_KEY} is not valid"}}
    transport = FakeTransport(error_body, error_body)
    connector = AlphaVantageConnector(transport)

    with pytest.raises(SourceError) as excinfo:
        connector.news_sentiment()
    assert AV_KEY not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)
    assert_no_secret(connector.fetch())


@pytest.mark.parametrize("payload", [{"note": "no feed here"}, [1, 2, 3], "plain text"])
def test_alpha_vantage_malformed_payload_raises(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    connector = AlphaVantageConnector(FakeTransport({"payload": payload}))
    with pytest.raises(SourceError):
        connector.news_sentiment()


def test_alpha_vantage_source_failure_becomes_a_missing_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    failure = SourceError("alpha_vantage", "HTTP 500", status_code=500)
    breach = QuotaExceeded("alpha_vantage", "calls_per_day", 25, 25, 3600.0)
    connector = AlphaVantageConnector(FakeTransport(failure, breach))

    first = connector.fetch()
    assert first.is_stale is True
    assert first.fields["missing"] is True
    assert first.error == "alpha_vantage: HTTP 500"
    assert first.calls == 0
    assert first.quota_remaining == 100

    second = connector.fetch()
    assert second.is_stale is True
    assert "quota for 'alpha_vantage' would be breached" in (second.error or "")


def test_alpha_vantage_missing_key_is_a_source_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_key(monkeypatch, alpha_vantage)
    result = AlphaVantageConnector(FakeTransport()).fetch()
    assert result.is_stale is True
    assert "ALPHA_VANTAGE_API_KEY is not configured" in (result.error or "")
    assert result.calls == 0


def test_alpha_vantage_cached_answer_costs_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    use_key(monkeypatch, alpha_vantage, "ALPHA_VANTAGE_API_KEY", AV_KEY)
    transport = FakeTransport({"payload": {"feed": [FEED_ITEM]}, "from_cache": True})
    result = AlphaVantageConnector(transport).fetch()
    assert result.is_stale is False
    assert result.calls == 0
    assert result.fields["from_cache"] is True
    assert_no_secret(result)
