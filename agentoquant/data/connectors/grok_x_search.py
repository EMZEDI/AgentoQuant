"""Grok X Search through OpenRouter: the official-handle narrative check.

The plan's early-signal layer watches "official project X accounts through Grok X Search with handle
filters", and the Verifier treats an official-handle post as primary evidence for its own project
(still requiring a price or volume reaction before it moves sizing). There is **no raw post feed**:
X Search through OpenRouter's native web plugin returns a **cited summary**, so this connector stores
the citations -- that is the evidence -- and never pretends a post exists that the model did not cite.

Verified live 2026-09-19: ``POST https://openrouter.ai/api/v1/chat/completions`` with
``{"model": "x-ai/grok-4.3", "messages": [{"role": "user", "content": <prompt>}],
"plugins": [{"id": "web"}], "x_search_filter": {"allowed_x_handles": [<handles>]}}`` returns a cited
summary carrying x.com citations. No xAI key is needed; the project's own OpenRouter key is the
credential (``AGENTOQUANT_OPENROUTER_KEY``), sent only as an ``Authorization: Bearer`` header.

Cost is **never estimated**: the response's ``usage.cost`` field is read when present (roughly $0.0096
per call plus the pass-through xAI tool cost, $5 per 1k posts from Sept 21 2026) and the reading
carries exactly that number, or 0.0 when the field is absent. ``cost_from_response`` also hands the
same figure to the transport, so the quota journal and the cost meter see the real spend.

``config/sources.yaml`` caps this source at 20 calls an hour with a 300 s cache TTL: the prompt is part
of the cache key (it lives in the POST body), so an identical question inside the TTL is answered from
the cache and costs nothing.
"""

from __future__ import annotations

from typing import Any

from agentoquant.config_loader import credentials
from agentoquant.data import ConnectorResult, SourceError, Transport, redact
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "grok_x_search"

BASE_URL = "https://openrouter.ai/api/v1"

ENDPOINT = f"{BASE_URL}/chat/completions"

#: The model the plan pins for X Search.
MODEL = "x-ai/grok-4.3"

#: The ledger series name for a general search, and the prefix for a per-ticker one.
SERIES = "x_search_summary"
TICKER_SERIES_PREFIX = "x_sentiment_"

#: The plugin that turns on web/X search.
PLUGINS = [{"id": "web"}]

#: The prompt :meth:`GrokXSearchConnector.fetch` asks when the caller has nothing more specific.
DEFAULT_PROMPT = (
    "Search X for the last 24 hours of posts about the crypto market: the dominant narrative, any "
    "large moves, exchange or protocol incidents and the macro or policy news people are reacting "
    "to. Summarise it in a few sentences, name the posts you rely on, and say plainly when the "
    "evidence is thin."
)


class GrokXSearchConnector:
    """Cited X summaries through OpenRouter's web plugin. Never a raw post feed."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _api_key(self) -> str:
        """The OpenRouter key, read fresh from the credentials file. The value is never logged."""
        key = credentials().get("AGENTOQUANT_OPENROUTER_KEY")
        if not key:
            raise SourceError(SOURCE, "AGENTOQUANT_OPENROUTER_KEY is not configured")
        return key

    def _headers(self) -> dict[str, str]:
        """The credential header. It is never included in an error message."""
        return {"Authorization": f"Bearer {self._api_key()}"}

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 300)

    def search(
        self,
        prompt: str,
        *,
        handles: list[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        force: bool = False,
    ) -> dict:
        """One cited X search: the summary, its citations and the exact cost.

        Raises :class:`~agentoquant.data.SourceError` on an OpenRouter error body, on a response with
        no choices and on an empty summary. Citations are returned exactly as the response carries
        them: an empty list when the response carries none, never a fabricated URL.
        """
        key = self._api_key()
        body: dict[str, Any] = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "plugins": PLUGINS,
        }
        filters: dict[str, Any] = {}
        if handles:
            filters["allowed_x_handles"] = [handle.lstrip("@") for handle in handles if handle]
        if from_date:
            filters["from_date"] = from_date
        if to_date:
            filters["to_date"] = to_date
        if filters:
            body["x_search_filter"] = filters
        response = self.transport.post_json(
            ENDPOINT,
            source=SOURCE,
            headers=self._headers(),
            json_body=body,
            ttl_seconds=self._ttl(),
            force=force,
            cost_from_response=_usage_cost,
        )
        payload = response.payload
        if not isinstance(payload, dict):
            raise SourceError(SOURCE, "chat/completions did not return an object")
        error = payload.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise SourceError(SOURCE, f"OpenRouter error: {_scrub(str(message), key)}")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise SourceError(SOURCE, "chat/completions returned no choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            raise SourceError(SOURCE, "chat/completions choice carried no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SourceError(SOURCE, "chat/completions returned an empty summary")
        citations = _citations(message, payload)
        usage = payload.get("usage")
        return {
            "prompt": prompt,
            "model": payload.get("model", MODEL),
            "handles": list(handles or []),
            "from_date": from_date,
            "to_date": to_date,
            "summary": content.strip(),
            "citation_urls": [citation["url"] for citation in citations],
            "citations": citations,
            "x_citation_count": sum(
                1 for citation in citations if _is_x_url(citation["url"])
            ),
            "usage": usage if isinstance(usage, dict) else {},
            "cost_usd": _usage_cost(payload),
            "finish_reason": choice.get("finish_reason"),
            "from_cache": response.from_cache,
        }

    def ticker_sentiment(
        self,
        ticker: str,
        *,
        handles: list[str] | None = None,
        force: bool = False,
    ) -> ConnectorResult:
        """One ticker's X narrative as a ledger-bound reading. Never raises."""
        prompt = ticker_prompt(ticker)
        try:
            fields = self.search(prompt, handles=handles, force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(f"{TICKER_SERIES_PREFIX}{ticker.strip().upper()}", str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=f"{TICKER_SERIES_PREFIX}{ticker.strip().upper()}",
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
            cost_usd=fields.get("cost_usd", 0.0),
        )

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The default X narrative as one ledger-bound reading. Never raises."""
        try:
            fields = self.search(DEFAULT_PROMPT, force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
            cost_usd=fields.get("cost_usd", 0.0),
        )

    def missing(self, coin_or_series: str, error: str) -> ConnectorResult:
        """A missing reading for this source, shaped like a real one."""
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=coin_or_series,
            fields={"missing": True},
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=True,
            error=error,
        )


def ticker_prompt(ticker: str) -> str:
    """A sensible X-search prompt for one ticker: sentiment, catalysts, and the citations."""
    symbol = ticker.strip().upper()
    return (
        f"Search X for posts about the crypto asset {symbol} (ticker ${symbol}) from the last 48 "
        f"hours. Summarise what is driving the conversation, the dominant sentiment (bullish, "
        f"bearish or mixed) and any concrete catalyst named -- listing, unlock, release, partnership "
        f"or incident. Cite the posts you rely on, and say explicitly when the evidence is thin "
        f"rather than guessing."
    )


def _citations(message: dict, payload: dict) -> list[dict]:
    """Every citation the response carries, deduplicated by URL, in order of appearance.

    OpenRouter attaches them either as ``message.annotations`` (``url_citation`` objects) or as a
    ``citations`` list of URLs. When neither is present the result is an empty list: a URL is never
    invented.
    """
    found: list[dict] = []
    seen: set[str] = set()
    blocks = (message.get("annotations"), message.get("citations"), payload.get("citations"))
    for block in blocks:
        if not isinstance(block, list):
            continue
        for entry in block:
            url: Any = None
            title: Any = None
            if isinstance(entry, str):
                url = entry
            elif isinstance(entry, dict):
                inner = entry.get("url_citation")
                if isinstance(inner, dict):
                    url, title = inner.get("url"), inner.get("title")
                else:
                    url, title = entry.get("url"), entry.get("title")
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                found.append({"url": url, "title": title})
    return found


def _is_x_url(url: str) -> bool:
    return "//x.com/" in url or "//twitter.com/" in url


def _usage_cost(payload: Any) -> float:
    """The exact cost OpenRouter reported (``usage.cost``), or 0.0 when the field is absent."""
    if not isinstance(payload, dict):
        return 0.0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    try:
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _scrub(text: str, key: str) -> str:
    """Remove the credential value from a message, then apply the project's redaction rules."""
    if key and key in text:
        text = text.replace(key, "<redacted>")
    return redact(text)


__all__ = [
    "BASE_URL",
    "DEFAULT_PROMPT",
    "ENDPOINT",
    "MODEL",
    "PLUGINS",
    "SERIES",
    "SOURCE",
    "TICKER_SERIES_PREFIX",
    "GrokXSearchConnector",
    "ticker_prompt",
]
