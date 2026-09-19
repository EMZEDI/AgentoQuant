"""Outbound-only Telegram notification for the paper loop. Owned by Task 6.

Phase 0 sends and never receives. This module has no polling loop, no webhook receiver and no
command handler: the Hermes gateway owns the inbound path for the bot token (recorded decision 3), and
a second poller on one token would break it. The test suite asserts that this module contains no
``getUpdates`` call and no webhook.

Two message shapes are rendered here: the Decision Card, in exactly the addendum's template order so
``/why`` and the daily audit show the same layout, and a compact paper-cycle result. Funding Request
cards are rendered by ``agentoquant.risk.funding_floor.render_card``, which owns that shape.

Credentials come from, in order: ``~/.config/agentoquant/credentials.env`` (mode 600, outside the
repo), ``~/.hermes/.env`` (the existing Hermes bot, per ``.hermes.md``), and the process environment.
A value is never printed, logged or echoed; the resolved pair is reported as a handle only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agentoquant.config_loader import load_settings
from agentoquant.ledger.schema import DecisionCard
from agentoquant.risk.funding_floor import render_card as render_funding_request

#: The Telegram bot API root. Outbound sends only.
API_ROOT = "https://api.telegram.org"

#: Variable names, in resolution order per source. Never their values.
TOKEN_VARS: tuple[str, ...] = ("TELEGRAM_BOT_TOKEN", "AGENTOQUANT_TELEGRAM_BOT_TOKEN")
CHAT_VARS: tuple[str, ...] = ("TELEGRAM_HOME_CHANNEL", "AGENTOQUANT_TELEGRAM_CHAT_ID")

#: The switch. Notifications are OFF unless this is set to one of ENABLE_VALUES.
ENABLE_ENV = "AGENTOQUANT_TELEGRAM"

#: The only values that turn notifications ON. Anything else, including an unset variable, is off.
ENABLE_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: The Hermes environment file that already holds the bot token.
HERMES_ENV_PATH = Path.home() / ".hermes" / ".env"

#: How long a send may take before it is abandoned.
SEND_TIMEOUT_S = 10.0


class TelegramError(RuntimeError):
    """A notification could not be delivered. The message never carries a token."""


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a KEY=VALUE file. Values stay in the returned mapping; nothing is logged."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _from_credentials_file(name: str) -> str | None:
    """Look one variable up in the project's credentials file, without ever echoing it."""
    try:
        from agentoquant.config_loader import credentials
    except ImportError:  # pragma: no cover - the loader is always present
        return None
    try:
        values = credentials()
    except Exception:
        return None
    value = values.get(name)
    return str(value) if value else None


def resolve_telegram_credentials(
    *,
    token: str | None = None,
    chat_id: str | None = None,
    environ: dict[str, str] | None = None,
    hermes_env: Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the bot token and the home channel from the three documented sources.

    Order per value: the explicit argument, the process environment, the Hermes environment file, then
    the project credentials file. The returned values are never logged by this module.
    """
    env = environ if environ is not None else dict(os.environ)
    hermes = _read_env_file(Path(hermes_env) if hermes_env is not None else HERMES_ENV_PATH)

    def pick(names: tuple[str, ...], explicit: str | None) -> str | None:
        if explicit:
            return explicit
        for name in names:
            if env.get(name):
                return env[name]
        for name in names:
            if hermes.get(name):
                return hermes[name]
        for name in names:
            found = _from_credentials_file(name)
            if found:
                return found
        return None

    return pick(TOKEN_VARS, token), pick(CHAT_VARS, chat_id)


def notifications_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether outbound notifications are switched on. **Opt-in, and unset means silent.**

    This used to be enabled whenever ``AGENTOQUANT_TELEGRAM`` was unset, which meant any process on
    the box that could reach the bot token posted to Shahrad's phone - including the test suite,
    which runs real cycles: deleting the variable in a test *enabled* sending, and 90 Decision Cards
    arrived in half an hour. A notification path that fires when nobody asked is worse than one that
    stays quiet, so the two places that want it turn it on explicitly: the soak's systemd unit, and
    Task 21's veto gate.
    """
    env = environ if environ is not None else dict(os.environ)
    return str(env.get(ENABLE_ENV, "")).strip().lower() in ENABLE_VALUES


class SendTransport(Protocol):
    """What a notifier needs to deliver one message. Tests inject a recorder."""

    def __call__(self, token: str, chat_id: str, text: str) -> dict[str, Any]: ...


def http_send_message(token: str, chat_id: str, text: str) -> dict[str, Any]:
    """Send one message through the Telegram bot API. Outbound only: ``sendMessage`` and nothing else.

    The token is part of the request URL, so it is never included in a raised error's message.
    """
    url = f"{API_ROOT}/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TelegramError(f"send failed: {type(exc).__name__}") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise TelegramError("the bot API did not confirm the send")
    return payload


# ----------------------------------------------------------------------------------------------
# Rendering. The Decision Card order is fixed by the addendum, so /why can re-render a stored card.
# ----------------------------------------------------------------------------------------------


def _value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def veto_window_minutes() -> int:
    """The auto-execute window, from ``config/settings.yaml`` (10 minutes)."""
    try:
        return int(load_settings().human_in_the_loop.veto_window_minutes)
    except Exception:  # pragma: no cover - a broken config fails closed at CLI startup
        return 10


def render_decision_card(card: DecisionCard, *, window_minutes: int | None = None) -> str:
    """The Decision Card message, in the addendum's fixed order."""
    objection = card.strongest_objection or {}
    severity = objection.get("severity", "n/a")
    text = objection.get("text", "none recorded")
    evidence = card.evidence_split or {}
    minutes = veto_window_minutes() if window_minutes is None else int(window_minutes)
    return "\n".join(
        [
            f"[{_value(card.action)}] {card.coin or '-'} · {_value(card.sleeve) if card.sleeve else '-'}"
            f" · size {card.size_pct if card.size_pct is not None else '-'}%",
            f"Confidence: {card.confidence} ({_value(card.confidence_band)})   "
            f"P(up): {_number(card.p_up, 2)} [{_number(card.interval_low, 2)}, "
            f"{_number(card.interval_high, 2)}]",
            f"EV after fees ({card.fee_tier_assumed}): {_number(card.ev_after_fees, 6)}",
            f"Evidence: {evidence.get('primary', 0)} primary / {evidence.get('verified', 0)} verified"
            f" / {evidence.get('unverified', 0)} unverified",
            f"Strongest objection (sev {severity}): {text}",
            f"Flips if: {card.flip_condition}",
            f"Auto-executes in {minutes} min unless /veto",
        ]
    )


def render_paper_result(result: dict[str, Any]) -> str:
    """A compact digest of one paper cycle, for the operator's phone."""
    lines = [
        f"PAPER CYCLE {result.get('cycle_id', '?')}",
        f"action:   {result.get('action', '?')}   verdict: {result.get('verdict', '?')}"
        + (f" ({result['rule_fired']})" if result.get("rule_fired") else ""),
        f"path:     {result.get('path', '?')}",
        f"orders:   {result.get('orders', 0)} placed, {result.get('fills', 0)} filled",
        f"fees:     {_number(result.get('fees_paid'), 8)} USD at {result.get('fee_tier', '?')}",
        f"cost:     {_number(result.get('cost_usd'), 6)} USD (LLM)",
        f"ack:      {result.get('ack', 'none')}",
        f"status:   {result.get('status', '?')}",
    ]
    return "\n".join(lines)


class TelegramNotifier:
    """Sends the Decision Card and the paper result. Outbound only, and silent when disabled."""

    def __init__(
        self,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        transport: SendTransport | None = None,
        enabled: bool | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        env = environ if environ is not None else dict(os.environ)
        self.token, self.chat_id = resolve_telegram_credentials(
            token=token, chat_id=chat_id, environ=env
        )
        self.transport: SendTransport = transport if transport is not None else http_send_message
        self.enabled = bool(enabled) if enabled is not None else notifications_enabled(env)
        self.sent: list[str] = []

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return f"TelegramNotifier(enabled={self.enabled}, token=<redacted>, chat_id=<redacted>)"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        """Deliver one message. Returns whether it went out; never raises for a missing credential."""
        if not self.enabled or not self.configured:
            return False
        try:
            self.transport(self.token or "", self.chat_id or "", text)
        except Exception:  # a failed notification never fails the cycle
            return False
        self.sent.append(text)
        return True

    def send_card(self, card: DecisionCard, *, window_minutes: int | None = None) -> bool:
        return self.send(render_decision_card(card, window_minutes=window_minutes))

    def send_funding_request(self, payload: Any, *, record_id: str | None = None) -> bool:
        return self.send(render_funding_request(payload, record_id=record_id))

    def send_paper_result(self, result: dict[str, Any]) -> bool:
        return self.send(render_paper_result(result))

    def send_many(self, texts: list[str]) -> int:
        return sum(1 for text in texts if self.send(text))


#: Type alias for the injected transport, kept for callers that want to annotate it.
TransportCallable = Callable[[str, str, str], dict[str, Any]]


__all__ = [
    "API_ROOT",
    "CHAT_VARS",
    "ENABLE_ENV",
    "HERMES_ENV_PATH",
    "SEND_TIMEOUT_S",
    "TOKEN_VARS",
    "SendTransport",
    "TelegramError",
    "TelegramNotifier",
    "TransportCallable",
    "http_send_message",
    "notifications_enabled",
    "render_decision_card",
    "render_funding_request",
    "render_paper_result",
    "resolve_telegram_credentials",
    "veto_window_minutes",
]


