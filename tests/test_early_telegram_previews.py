"""Per-module tests for ``agentoquant.data.early_signals.telegram_previews`` (Task 4, Phase 0).

Offline by construction: the page fetch goes through the canned stub fetcher from
``tests/test_early_signals.py`` (imported, never duplicated) and the ledger is a scratch DuckDB file
under ``tmp_path``. No decorators in this file (see the at-sign rule in ``.hermes.md``): the scratch
ledger, the page fixtures and the listener factory are plain helpers.

Nothing here speaks MTProto: the module only ever GETs the public ``t.me/s/<channel>`` web preview, and
these tests only ever hand it a canned page.

Coverage map: ``classify_event_type`` against the whole event vocabulary, the ``t.me/s`` parser on a
real-shaped page (message text nesting, the footer timestamp, entities, ``br``, photo-only messages,
messages with no ``data-post``), malformed / empty / wrong-type bodies, the no-preview path, per-channel
failure isolation, ``poll_once`` ledger writes, dedupe, failure accounting with backoff and recovery, and
close().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from agentoquant.data.early_signals import JsonlLog, SignalWriter, utcnow
from agentoquant.data.early_signals.telegram_previews import (
    BASE_URL,
    DEFAULT_CHANNELS,
    PREVIEW_PATH,
    TelegramPreviewsListener,
    classify_event_type,
    parse_telegram_preview,
)
from agentoquant.enums import SourceClass
from agentoquant.ledger.store import DB_PATH_ENV, LedgerStore
from tests.test_early_signals import StubFetcher, log_events, signal_rows

# ----------------------------------------------------------------------------------------------
# Plain helpers (no fixtures: this file must not emit a decorator)
# ----------------------------------------------------------------------------------------------

OBSERVED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
MESSAGE_AT = datetime(2026, 9, 16, 5, 1, 58, tzinfo=UTC)
CHANNEL = "binance_announcements"
CHANNEL_PATH = PREVIEW_PATH.format(channel=CHANNEL)
OTHER_CHANNEL = "bybit_announcements"
OTHER_CHANNEL_PATH = PREVIEW_PATH.format(channel=OTHER_CHANNEL)

#: A page in the shape ``t.me/s/<channel>`` serves: the text block comes before the footer ``<time>``,
#: so a parser that finalized on the text div's close would lose every timestamp.
PAGE = f"""<html><body><main>
<div class="tgme_widget_message js-widget_message" data-post="{CHANNEL}/8948">
  <div class="tgme_widget_message_text js-message_text" dir="auto">
    <b>Binance will list PENGU (PENGU) for spot trading</b><br>Deposits open now.
  </div>
  <div class="tgme_widget_message_footer">
    <a class="tgme_widget_message_date" href="https://t.me/{CHANNEL}/8948">
      <time datetime="2026-09-16T05:01:58+00:00" class="time">05:01</time></a>
  </div>
</div>
<div class="tgme_widget_message js-widget_message" data-post="{CHANNEL}/8949">
  <div class="tgme_widget_message_body">
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      Binance will delist XYZUSDT perpetual contracts &amp; options
    </div>
  </div>
</div>
<div class="tgme_widget_message js-widget_message" data-post="{CHANNEL}/8950">
  <a class="tgme_widget_message_photo_wrap" href="https://t.me/{CHANNEL}/8950">photo</a>
</div>
<div class="tgme_widget_message js-widget_message">
  <div class="tgme_widget_message_text js-message_text" dir="auto">no data-post, so not addressable</div>
</div>
</main></body></html>"""


class ExplodingFetcher:
    """A fetcher that raises a non-``FetchError``: the failure the listener does not isolate."""

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


def make_listener(writer: SignalWriter, fetcher: Any, **kwargs: Any) -> TelegramPreviewsListener:
    """A preview listener over one canned channel, logging to the writer's log."""
    kwargs.setdefault("channels", (CHANNEL,))
    kwargs.setdefault("log", writer.log)
    return TelegramPreviewsListener(writer, fetcher=fetcher, **kwargs)


# ----------------------------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------------------------


def test_classify_event_type_covers_the_ledger_vocabulary() -> None:
    cases: list[tuple[str, str]] = [
        ("Binance will list PENGU (PENGU) for spot trading", "listing"),
        ("New pair listed for spot trading", "listing"),
        ("Binance will delist XYZUSDT perpetual contracts", "policy"),
        ("Token unlock schedule for RENDER published", "unlock"),
        ("Vesting cliff for the team allocation", "unlock"),
        ("A whale moved 5,000,000 tokens to exchange", "large_transfer"),
        ("Mainnet upgrade v2.1 is live", "release"),
        ("The launch of the new chain", "release"),
        ("SEC charges filed against the project", "policy"),
        ("Weekly market recap", "headline"),
        ("", "headline"),
    ]
    for text, expected in cases:
        assert classify_event_type(text) == expected, text


def test_a_delisting_is_not_read_as_a_listing() -> None:
    """Order matters: 'will delist' must be classified before the listing keywords are consulted."""
    assert classify_event_type("We will delist the ABC token") == "policy"
    assert classify_event_type("We will list the ABC token") == "listing"


# ----------------------------------------------------------------------------------------------
# The t.me/s parser: real-shaped, malformed, empty, wrong-type
# ----------------------------------------------------------------------------------------------


def test_parse_telegram_preview_reads_the_documented_page_shape() -> None:
    events = parse_telegram_preview(PAGE, channel=CHANNEL, observed_at=OBSERVED_AT)

    assert len(events) == 2  # the photo-only message and the post-less div are dropped
    first, second = events
    assert first.source_class is SourceClass.TELEGRAM_PREVIEW
    assert first.event_type == "listing"
    assert first.ticker == "PENGU"
    assert first.detected_at == MESSAGE_AT
    assert first.published_at == MESSAGE_AT
    assert first.observed_at == OBSERVED_AT
    assert first.raw_text_or_ref == f"{BASE_URL}/{CHANNEL}/8948"
    assert first.source == "telegram_previews"

    assert second.event_type == "policy"
    assert second.ticker == "XYZ"
    assert second.raw_text_or_ref == f"{BASE_URL}/{CHANNEL}/8949"
    assert second.detected_at == OBSERVED_AT  # no <time> in that message: observed, not guessed
    assert second.published_at is None


def test_a_preview_is_its_own_source_class_not_an_exchange_announcement() -> None:
    """A web preview of a channel is weaker evidence than the owner's own API."""
    events = parse_telegram_preview(PAGE, channel=CHANNEL, observed_at=OBSERVED_AT)
    assert {event.source_class for event in events} == {SourceClass.TELEGRAM_PREVIEW}
    assert SourceClass.TELEGRAM_PREVIEW is not SourceClass.EXCHANGE_ANNOUNCEMENT


def test_parse_telegram_preview_handles_malformed_empty_and_wrong_type_bodies() -> None:
    for label, body in [
        ("empty string", ""),
        ("no markup", "Forbidden"),
        ("empty page", "<html><body></body></html>"),
        ("json body", json.dumps({"ok": True})),
        ("message div with no post id", '<div class="tgme_widget_message js-widget_message">'
         '<div class="tgme_widget_message_text js-message_text">orphan</div></div>'),
        ("message with a time but no text", '<div class="tgme_widget_message js-widget_message" '
         'data-post="c/1"><time datetime="2026-09-16T05:01:58+00:00"></time></div>'),
    ]:
        assert parse_telegram_preview(body, channel=CHANNEL, observed_at=OBSERVED_AT) == [], label


def test_an_unparseable_message_timestamp_falls_back_to_the_observation_time() -> None:
    page = ('<div class="tgme_widget_message js-widget_message" data-post="c/2">'
            '<div class="tgme_widget_message_text js-message_text">Token unlock next week</div>'
            '<time datetime="yesterday"></time></div>')
    events = parse_telegram_preview(page, channel="c", observed_at=OBSERVED_AT)
    assert len(events) == 1
    assert events[0].detected_at == OBSERVED_AT
    assert events[0].published_at is None
    assert events[0].event_type == "unlock"


def test_parse_telegram_preview_requires_a_string_page() -> None:
    """The page arrives as ``response.text``; a bytes body is a caller bug, not a silent empty parse."""
    with pytest.raises(TypeError):
        parse_telegram_preview(b"<html></html>", channel=CHANNEL, observed_at=OBSERVED_AT)


def test_parse_telegram_preview_collapses_whitespace_and_decodes_entities() -> None:
    page = ('<div class="tgme_widget_message js-widget_message" data-post="c/7">'
            '<div class="tgme_widget_message_text js-message_text">'
            "Binance will list   FOO &amp; BAR (FOO)</div></div>")
    events = parse_telegram_preview(page, channel="c", observed_at=OBSERVED_AT)
    assert len(events) == 1
    assert events[0].ticker == "FOO"
    assert events[0].event_type == "listing"


def test_parse_telegram_preview_uses_the_message_permalink_as_the_reference() -> None:
    """The permalink is stable across polls, which is what makes de-duplication work."""
    page = ('<div class="tgme_widget_message js-widget_message" data-post="c/8">'
            '<div class="tgme_widget_message_text js-message_text">Token unlock next week</div></div>')
    events = parse_telegram_preview(page, channel="c", observed_at=OBSERVED_AT)
    assert events[0].raw_text_or_ref == f"{BASE_URL}/c/8"
    assert events[0].dedupe_key == ("telegram_preview", f"{BASE_URL}/c/8")


# ----------------------------------------------------------------------------------------------
# The listener: ledger writes, the no-preview path, per-channel isolation
# ----------------------------------------------------------------------------------------------


def test_poll_once_writes_the_ledger_rows_for_every_channel(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fetcher = StubFetcher(
        texts={CHANNEL_PATH: PAGE, OTHER_CHANNEL_PATH: PAGE.replace(CHANNEL, OTHER_CHANNEL)}
    )
    listener = make_listener(writer, fetcher, channels=(CHANNEL, OTHER_CHANNEL))

    written = listener.poll_once()

    assert len(written) == 4
    rows = signal_rows(store)
    assert [row["source_class"] for row in rows] == [SourceClass.TELEGRAM_PREVIEW.value] * 4
    assert [row["producer_role"] for row in rows] == ["listener_telegram_previews"] * 4
    assert [row["event_type"] for row in rows] == ["listing", "policy", "listing", "policy"]
    assert [row["ticker"] for row in rows] == ["PENGU", "XYZ", "PENGU", "XYZ"]
    assert all(row["cycle_id"].endswith("-listen") for row in rows)
    assert [call[1] for call in fetcher.calls] == [CHANNEL_PATH, OTHER_CHANNEL_PATH]
    assert [call[2] for call in fetcher.calls] == [{}, {}]  # the preview URL takes no parameters
    assert DEFAULT_CHANNELS == ("binance_announcements", "bybit_announcements")


def test_a_channel_without_a_public_preview_is_logged_not_raised(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Kraken's and OKX's channels 302 to their join page: no messages, and that is not an error."""
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(
        writer, StubFetcher(texts={CHANNEL_PATH: "<html><body>Please open Telegram to view</body></html>"})
    )

    assert listener.poll_once() == []
    assert listener.stats.failures == 0
    assert signal_rows(store) == []
    no_messages = log_events(writer.log, "no_messages")
    assert [record["channel"] for record in no_messages] == [CHANNEL]


def test_one_dead_channel_does_not_hide_a_live_one(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    fetcher = StubFetcher(texts={OTHER_CHANNEL_PATH: PAGE.replace(CHANNEL, OTHER_CHANNEL)})
    listener = make_listener(writer, fetcher, channels=(CHANNEL, OTHER_CHANNEL))

    written = listener.poll_once()

    assert len(written) == 2
    assert {row["raw_text_or_ref"] for row in signal_rows(store)} == {
        f"{BASE_URL}/{OTHER_CHANNEL}/8948",
        f"{BASE_URL}/{OTHER_CHANNEL}/8949",
    }
    assert listener.stats.failures == 0, "a dead channel is isolated, not a poll failure"
    failed = log_events(writer.log, "channel_failed")
    assert [record["channel"] for record in failed] == [CHANNEL]
    assert "HTTP 404" in failed[0]["error"]


def test_poll_once_deduplicates_a_repeated_poll(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, StubFetcher(texts={CHANNEL_PATH: PAGE}))

    assert len(listener.poll_once()) == 2
    assert listener.poll_once() == []

    assert len(signal_rows(store)) == 2
    assert listener.stats.events == 4
    assert listener.stats.written == 2
    assert listener.stats.duplicates == 2
    assert log_events(writer.log, "poll_ok")[1]["duplicates"] == 2


# ----------------------------------------------------------------------------------------------
# Failure accounting: failures, consecutive_failures, backoff, recovery
# ----------------------------------------------------------------------------------------------


def test_an_unexpected_failure_is_counted_backed_off_and_recovered(
    tmp_path: Any, monkeypatch: Any
) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    writer = writer_for(store, tmp_path)
    listener = make_listener(writer, ExplodingFetcher(RuntimeError("html parser exploded")))

    assert listener.poll_once() == []
    first_backoff = listener.stats.backoff_seconds
    assert listener.stats.failures == 1
    assert listener.stats.consecutive_failures == 1
    assert first_backoff > 0
    assert listener.stats.last_error is not None
    assert "RuntimeError" in listener.stats.last_error
    assert signal_rows(store) == []

    assert listener.poll_once() == []
    assert listener.stats.failures == 2
    assert listener.stats.consecutive_failures == 2
    assert listener.stats.backoff_seconds > first_backoff
    assert listener.stats.backoff_seconds <= listener.max_backoff_seconds * 1.25
    assert [record["consecutive_failures"] for record in log_events(writer.log, "poll_failed")] == [1, 2]

    listener.fetcher = StubFetcher(texts={CHANNEL_PATH: PAGE})
    assert len(listener.poll_once()) == 2
    assert listener.stats.consecutive_failures == 0
    assert listener.stats.backoff_seconds == 0.0
    assert listener.stats.last_error is None
    assert listener.stats.last_success_at is not None
    assert listener.stats.last_success_at <= utcnow()


def test_close_releases_the_fetcher(tmp_path: Any, monkeypatch: Any) -> None:
    store = scratch_ledger(tmp_path, monkeypatch)
    stub = StubFetcher(texts={CHANNEL_PATH: PAGE})
    listener = make_listener(writer_for(store, tmp_path), stub)
    listener.close()
    assert stub.closed is True


def test_the_preview_url_is_the_public_web_preview() -> None:
    """No MTProto, no Telegram account: the only thing this listener may fetch is the web preview."""
    assert BASE_URL == "https://t.me"
    assert PREVIEW_PATH == "/s/{channel}"
    assert CHANNEL_PATH == f"/s/{CHANNEL}"
