"""GDELT connector: coded conflict events for the standing regime prior.

Standing prior 5 reads the regime from "wars, gold and the dollar": escalation with gold and the
dollar rising together reads as risk-off for crypto beta. GDELT's DOC 2.0 API is where the "wars"
half of that comes from -- a query over global news with a Middle East conflict filter, in
``artlist`` mode, returning the matching articles with their country, domain and language.

It is a **confirmation layer only, never a trigger**. Three verified properties force that:

* this host gets **HTTP 429** from GDELT with the message "one request every 5 seconds", and
  ``QuotaManager.MIN_INTERVAL_SECONDS`` encodes a stricter 60 s gap measured live from this IP
  (5 s still 429s), so the connector cannot poll faster than that;
* latency is 10-13 s per call, so it must never sit on the critical path of an hourly cycle;
* ``config/sources.yaml`` caps it at ``calls_per_minute: 1`` with a 900 s cache TTL, and the
  snapshot polls it every 15 minutes at most.

429 is treated as retryable (``RETRY_STATUSES``) with a longer base delay than the default
(:data:`RETRY`), and the exhausted case becomes a missing reading rather than an exception.

Verified-live response quirk this module encodes: an empty result set comes back as a **plain-text
body, not JSON**, so a non-JSON or empty body is an empty event list, never an error. ``artlist``
mode carries ``url``, ``url_mobile``, ``title``, ``seendate``, ``socialimage``, ``domain``,
``language`` and ``sourcecountry`` per article and **no tone field**, so ``tone`` is recorded as
``None`` unless the response actually carries one -- it is never invented.
"""

from __future__ import annotations

from typing import Any

from agentoquant.data import ConnectorResult, RetryPolicy, SourceError, Transport, redact
from agentoquant.data.quota_manager import QuotaExceeded

#: The key this connector's budget and cache are filed under in ``config/sources.yaml``.
SOURCE = "gdelt"

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

#: The DOC 2.0 query endpoint is the base path itself.
ENDPOINT = f"{BASE_URL}/"

#: The ledger series name for the conflict-event reading.
SERIES = "conflict_events"

#: The standing query for prior 5: Middle East conflict and escalation terms, OR'd, so the reading is
#: the conflict picture rather than a generic news firehose.
DEFAULT_CONFLICT_QUERY = (
    '(middle east OR israel OR iran OR gaza OR lebanon OR houthi OR "red sea" OR hormuz OR syria) '
    "(conflict OR strike OR attack OR escalation OR ceasefire OR missile OR sanctions)"
)

#: How far back the reading looks. The snapshot runs hourly and GDELT is polled every 15 minutes.
DEFAULT_TIMESPAN = "1d"

#: ``maxrecords`` -- GDELT's own ceiling is 250 per request.
DEFAULT_MAX_RECORDS = 50

#: 429 (and 5xx) is retryable here; GDELT wants seconds, not milliseconds, between attempts.
RETRY = RetryPolicy(attempts=3, base_delay=5.0, max_delay=30.0)

#: The per-article fields the ledger keeps.
EVENT_FIELDS = ("title", "url", "seendate", "domain", "sourcecountry", "language")


class GdeltConnector:
    """Coded conflict events. Confirmation layer only, never a trigger."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _ttl(self) -> int:
        return self.transport.quota.ttl_seconds(SOURCE, 900)

    def conflict_events(
        self,
        *,
        query: str = DEFAULT_CONFLICT_QUERY,
        timespan: str = DEFAULT_TIMESPAN,
        max_records: int = DEFAULT_MAX_RECORDS,
        force: bool = False,
    ) -> dict:
        """``mode=artlist`` for the standing conflict query.

        Raises :class:`~agentoquant.data.SourceError` when the response is neither an object with an
        ``articles`` list nor the plain-text body GDELT uses for an empty result set.
        """
        response = self.transport.get_json(
            ENDPOINT,
            source=SOURCE,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max_records,
                "timespan": timespan,
            },
            ttl_seconds=self._ttl(),
            force=force,
            retry=RETRY,
        )
        events = _events(response.payload)
        return {
            "query": query,
            "timespan": timespan,
            "conflict_index": len(events),
            "events": events,
            "tone": _tone(events),
            "body_was_json": isinstance(response.payload, (dict, list)),
            "from_cache": response.from_cache,
        }

    def fetch(self, *, force: bool = False) -> ConnectorResult:
        """The conflict-event reading as one ledger-bound record. Never raises."""
        try:
            fields = self.conflict_events(force=force)
        except (SourceError, QuotaExceeded) as exc:
            return self.missing(SERIES, str(exc))
        return ConnectorResult(
            source=SOURCE,
            coin_or_series=SERIES,
            fields=fields,
            quota_remaining=self.transport.quota.remaining(SOURCE),
            is_stale=False,
            calls=0 if fields.get("from_cache") else 1,
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


def _events(payload: Any) -> list[dict]:
    """The article rows from an ``artlist`` body, tolerating GDELT's non-JSON empty result set.

    An empty or plain-text body (which is what GDELT returns when nothing matches, and also when it
    refuses with an HTML page) is an empty event list rather than an error. A JSON object whose
    ``articles`` is present but not a list, or a payload of any other type, is malformed and raises.
    """
    if payload is None or isinstance(payload, str):
        return []
    if isinstance(payload, dict):
        articles = payload.get("articles")
        if articles is None:
            return []
        if not isinstance(articles, list):
            raise SourceError(SOURCE, "artlist 'articles' was not a list")
        rows = [dict(row) for row in articles if isinstance(row, dict)]
        if articles and not rows:
            raise SourceError(SOURCE, "artlist carried no usable article rows")
        return rows
    raise SourceError(SOURCE, f"unexpected artlist payload: {redact(type(payload).__name__)}")


def _tone(events: list[dict]) -> dict | None:
    """A tone summary from whatever tone field the response carries, else ``None``.

    ``artlist`` mode does not return tone, so this is normally ``None``. A number is never invented:
    when no row carries a numeric tone the field is recorded as absent.
    """
    values: list[float] = []
    for row in events:
        for name in ("tone", "tonevalue", "tone_value"):
            if name in row:
                value = _float(row[name])
                if value is not None:
                    values.append(value)
                break
    if not values:
        return None
    return {
        "mean": round(sum(values) / len(values), 6),
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BASE_URL",
    "DEFAULT_CONFLICT_QUERY",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_TIMESPAN",
    "ENDPOINT",
    "EVENT_FIELDS",
    "RETRY",
    "SERIES",
    "SOURCE",
    "GdeltConnector",
]
