"""On-chain webhook receiver for Alchemy and Helius (large transfers, unlock transactions).

Push, not poll: the provider POSTs a payload at us. Hard requirements implemented here:

* **Verify the signature and fail closed.** Alchemy signs the raw body with HMAC-SHA256 in
  ``X-Alchemy-Signature``; Helius echoes the ``Authorization`` header configured on the webhook. A
  missing, malformed or wrong signature gets ``401`` and **nothing is written** - not a partial event,
  not an unattributed event. Comparison is :func:`hmac.compare_digest`; the secret and the presented
  signature are never logged.
* **Loopback only.** The default bind is ``127.0.0.1`` and any non-loopback host raises. A public
  ingress or tunnel is Task 6's job; this process must not be reachable from the network.
* **Attributed, or recorded as unattributed - never dropped.** Addresses are matched against a token
  registry (``data/early_signals/token_registry.json``, gitignored, format documented below). A
  transfer whose address is not in the registry is still written, with ``ticker=None``.
* **Attribution happens inside the request**, before the ``200`` is returned, so the ledger learns of
  the event in the same block the provider reported. The response body carries the block number and
  the measured block lag, which is how "attributed to a token within two blocks" is evidenced.
* **Standard library only** (``http.server``): starlette/fastapi are not pinned, and adding one would
  be a new dependency.

Registry format (JSON, addresses lowercased, values may be a plain ticker or an object)::

    {
      "0x1111111111111111111111111111111111111111": "VVV",
      "0x2222222222222222222222222222222222222222": {"ticker": "TAO", "kind": "unlock"},
      "So11111111111111111111111111111111111111112": {"ticker": "RENDER", "kind": "token"}
    }

``kind`` is ``token`` (default) or ``unlock``; an ``unlock`` match records ``event_type="unlock"``
instead of ``large_transfer``. The registry ships empty: inventing token addresses would mis-attribute
real transfers, so an operator fills it in (Task 6 deployment step, noted in the Task 4 report).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from agentoquant.config_loader import credentials, repo_root
from agentoquant.data.early_signals import (
    LOGGER,
    JsonlLog,
    ListenerStats,
    SignalEvent,
    SignalWriter,
    as_utc,
    utcnow,
)
from agentoquant.enums import SourceClass

#: Loopback binds only. Anything else is refused (see the module docstring).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
LOOPBACK_HOSTS: frozenset[str] = frozenset({DEFAULT_HOST, "::1", "localhost"})

#: Webhook paths. Alchemy is configured with the full URL; Helius likewise.
ALCHEMY_PATH = "/webhooks/alchemy"
HELIUS_PATH = "/webhooks/helius"
HEALTH_PATH = "/healthz"

ALCHEMY_SIGNATURE_HEADER = "x-alchemy-signature"
HELIUS_AUTH_HEADER = "authorization"

#: Provider -> (env var name, credentials-file key name) for the signing secret. Both are new names
#: that ``config/sources.yaml`` does not carry; ``config_loader.credentials()`` reads any KEY=VALUE
#: from the credentials file, so nothing in the frozen config surface needs to change.
SECRET_SOURCES: dict[str, tuple[str, str]] = {
    "alchemy": ("AGENTOQUANT_ALCHEMY_WEBHOOK_SIGNING_KEY", "ALCHEMY_WEBHOOK_SIGNING_KEY"),
    "helius": ("AGENTOQUANT_HELIUS_WEBHOOK_AUTH_TOKEN", "HELIUS_WEBHOOK_AUTH_TOKEN"),
}

DEFAULT_REGISTRY_PATH = "data/early_signals/token_registry.json"

#: A body larger than this is refused (413) rather than buffered.
MAX_BODY_BYTES = 2_000_000

#: Registry kinds.
KIND_TOKEN = "token"
KIND_UNLOCK = "unlock"


class SignatureError(RuntimeError):
    """A webhook payload did not carry a valid signature. Always fails closed."""


# ----------------------------------------------------------------------------------------------
# Signature verification
# ----------------------------------------------------------------------------------------------


def alchemy_signature(body: bytes, signing_key: str) -> str:
    """The HMAC-SHA256 hex digest Alchemy sends in ``X-Alchemy-Signature``."""
    return hmac.new(signing_key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_alchemy_signature(body: bytes, header_value: str | None, signing_key: str) -> bool:
    """Constant-time check of Alchemy's signature. A missing header is a failure, not a skip."""
    if not header_value or not signing_key:
        return False
    presented = header_value.strip().lower()
    return hmac.compare_digest(alchemy_signature(body, signing_key), presented)


def verify_helius_auth(header_value: str | None, secret: str) -> bool:
    """Constant-time check of the ``Authorization`` header Helius echoes back."""
    if not header_value or not secret:
        return False
    presented = header_value.strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    return hmac.compare_digest(presented, secret)


def verify_signature(
    provider: str, body: bytes, headers: Mapping[str, str], secret: str | None
) -> bool:
    """Dispatch to the provider's check. An unknown provider or a missing secret fails closed."""
    if not secret:
        return False
    lowered = {key.lower(): value for key, value in headers.items()}
    if provider == "alchemy":
        return verify_alchemy_signature(body, lowered.get(ALCHEMY_SIGNATURE_HEADER), secret)
    if provider == "helius":
        return verify_helius_auth(lowered.get(HELIUS_AUTH_HEADER), secret)
    return False


def resolve_webhook_secret(provider: str) -> str | None:
    """The signing secret for ``provider``: environment first, then the credentials file.

    Never logs or returns a value in an error message. Returns ``None`` when nothing is configured,
    which makes the receiver refuse to start rather than run unauthenticated.
    """
    names = SECRET_SOURCES.get(provider)
    if names is None:
        return None
    env_name, file_key = names
    value = os.environ.get(env_name)
    if value:
        return value.strip()
    try:
        stored = credentials().get(file_key)
    except Exception:  # noqa: BLE001 - a missing credentials file is a normal, safe state here
        return None
    return stored.strip() if stored else None


# ----------------------------------------------------------------------------------------------
# Token registry
# ----------------------------------------------------------------------------------------------


@dataclass
class TokenRegistry:
    """Address -> (ticker, kind). Empty by default; see the module docstring for the file format."""

    entries: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> TokenRegistry:
        entries: dict[str, dict[str, str]] = {}
        for address, value in mapping.items():
            key = str(address).strip().lower()
            if not key:
                continue
            if isinstance(value, str):
                entries[key] = {"ticker": value.strip().upper(), "kind": KIND_TOKEN}
            elif isinstance(value, dict):
                ticker = str(value.get("ticker") or "").strip().upper()
                kind = str(value.get("kind") or KIND_TOKEN).strip().lower()
                if ticker:
                    entries[key] = {"ticker": ticker, "kind": kind}
        return cls(entries=entries)

    @classmethod
    def load(cls, path: Path | str | None = None) -> TokenRegistry:
        """Read the registry file. A missing or unreadable file yields an empty registry."""
        resolved = Path(path) if path is not None else repo_root() / DEFAULT_REGISTRY_PATH
        if not resolved.exists():
            return cls()
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("token registry %s could not be read: %s", resolved, exc)
            return cls()
        if not isinstance(data, dict):
            return cls()
        entries = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
        return cls.from_mapping(entries)

    def lookup(self, address: Any) -> dict[str, str] | None:
        """Registry entry for an address, or ``None`` (the event is then recorded unattributed)."""
        if not isinstance(address, str):
            return None
        return self.entries.get(address.strip().lower())

    def attribute(self, *addresses: Any) -> dict[str, str] | None:
        """First matching entry among the given addresses."""
        for address in addresses:
            found = self.lookup(address)
            if found is not None:
                return found
        return None

    def __len__(self) -> int:
        return len(self.entries)


# ----------------------------------------------------------------------------------------------
# Payload parsing
# ----------------------------------------------------------------------------------------------


def _hex_int(value: Any) -> int | None:
    """Alchemy sends block numbers as hex strings (``0x140a1b2``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _iso_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return as_utc(parsed)


def _shorten(value: Any, length: int = 42) -> str:
    text = str(value or "")
    return text if len(text) <= length else text[:length] + "..."


def _ref(provider: str, network: str, block: int | None, event_id: str, detail: str) -> str:
    """The stable ledger reference for a webhook event (also the de-duplication key)."""
    block_part = f" block={block}" if block is not None else ""
    return f"{provider}:{network}:{event_id}{block_part} {detail}".strip()


def _event_type_for(matched: dict[str, str] | None, *, unlock_hint: bool = False) -> str:
    """``unlock`` when the registry (or the transaction type) says so, else ``large_transfer``."""
    if unlock_hint or (matched is not None and matched.get("kind") == KIND_UNLOCK):
        return "unlock"
    return "large_transfer"


def parse_alchemy_webhook(
    payload: Any,
    *,
    observed_at: datetime | None = None,
    registry: TokenRegistry | None = None,
    source: str = "onchain_webhooks",
) -> list[SignalEvent]:
    """Parse an Alchemy webhook body (Address Activity, and a generic fallback for anything else).

    Alchemy Address Activity shape::

        {"type": "ADDRESS_ACTIVITY", "event": {"network": "ETH_MAINNET",
          "activity": [{"fromAddress": "0x..", "toAddress": "0x..", "blockNum": "0x140a1b2",
                        "hash": "0x..", "value": 1.5, "asset": "USDC", "category": "token",
                        "rawContract": {"address": "0x..", "decimal": "0x6", "value": "0x.."}}]}}

    An unrecognised body is **not** dropped: it becomes one unattributed ``large_transfer`` event
    whose reference carries the raw JSON (truncated), so a provider schema change shows up in the
    ledger instead of vanishing.
    """
    observed_at = observed_at or utcnow()
    registry = registry or TokenRegistry()
    if not isinstance(payload, dict):
        return []

    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    network = str(event.get("network") or payload.get("network") or "unknown").upper()
    webhook_type = str(payload.get("type") or event.get("type") or "").upper()
    activity = event.get("activity")

    events: list[SignalEvent] = []
    if isinstance(activity, list) and activity:
        for item in activity:
            if not isinstance(item, dict):
                continue
            contract = item.get("rawContract")
            contract_address = contract.get("address") if isinstance(contract, dict) else None
            matched = registry.attribute(
                item.get("toAddress"), item.get("fromAddress"), contract_address
            )
            block = _hex_int(item.get("blockNum"))
            tx_hash = str(item.get("hash") or "")
            detail = (
                f"from={_shorten(item.get('fromAddress'))} to={_shorten(item.get('toAddress'))} "
                f"asset={item.get('asset') or 'native'} value={item.get('value')} "
                f"category={item.get('category') or 'external'}"
                f"{' attributed=' + matched['ticker'] if matched else ' attributed=unattributed'}"
            )
            events.append(
                SignalEvent(
                    source_class=SourceClass.ONCHAIN,
                    event_type=_event_type_for(matched),
                    raw_text_or_ref=_ref(
                        "alchemy", network, block, tx_hash or "activity", detail
                    ),
                    detected_at=_iso_or_none(payload.get("createdAt")) or observed_at,
                    ticker=(matched or {}).get("ticker"),
                    source=source,
                    observed_at=observed_at,
                    block_number=block,
                )
            )
        return events

    if webhook_type == "ADDRESS_ACTIVITY" or not payload:
        return events

    # Unknown shape: record it rather than losing it.
    block = _hex_int(
        (event.get("data") or {}).get("block", {}).get("number")
        if isinstance(event.get("data"), dict)
        else None
    )
    events.append(
        SignalEvent(
            source_class=SourceClass.ONCHAIN,
            event_type="large_transfer",
            raw_text_or_ref=_ref(
                "alchemy",
                network,
                block,
                f"unparsed-{webhook_type or 'unknown'}",
                json.dumps(payload, default=str)[:600],
            ),
            detected_at=_iso_or_none(payload.get("createdAt")) or observed_at,
            ticker=None,
            source=source,
            observed_at=observed_at,
            block_number=block,
        )
    )
    return events


def parse_helius_webhook(
    payload: Any,
    *,
    observed_at: datetime | None = None,
    registry: TokenRegistry | None = None,
    source: str = "onchain_webhooks",
) -> list[SignalEvent]:
    """Parse a Helius webhook body: a JSON array of enhanced transactions.

    Each item carries ``signature``, ``slot``, ``type``, ``description``, ``tokenTransfers`` (with
    ``mint``, ``fromUserAccount``, ``toUserAccount``) and ``nativeTransfers``. An item whose accounts
    are not in the registry is recorded unattributed.
    """
    observed_at = observed_at or utcnow()
    registry = registry or TokenRegistry()
    items = payload if isinstance(payload, list) else [payload]
    events: list[SignalEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        transfers = item.get("tokenTransfers")
        transfers = transfers if isinstance(transfers, list) else []
        native = item.get("nativeTransfers")
        native = native if isinstance(native, list) else []

        candidates: list[Any] = []
        for transfer in [*transfers, *native]:
            if not isinstance(transfer, dict):
                continue
            candidates.extend(
                [
                    transfer.get("mint"),
                    transfer.get("tokenAddress"),
                    transfer.get("toUserAccount"),
                    transfer.get("fromUserAccount"),
                    transfer.get("toAccount"),
                    transfer.get("fromAccount"),
                ]
            )
        matched = registry.attribute(*candidates)

        slot = _hex_int(item.get("slot"))
        tx_type = str(item.get("type") or "UNKNOWN").upper()
        signature = str(item.get("signature") or item.get("transactionHash") or "tx")
        detail = (
            f"type={tx_type} description={_shorten(item.get('description'), 160)} "
            f"transfers={len(transfers) + len(native)}"
            f"{' attributed=' + matched['ticker'] if matched else ' attributed=unattributed'}"
        )
        events.append(
            SignalEvent(
                source_class=SourceClass.ONCHAIN,
                event_type=_event_type_for(
                    matched, unlock_hint=("UNLOCK" in tx_type or "VEST" in tx_type)
                ),
                raw_text_or_ref=_ref("helius", "SOLANA", slot, signature, detail),
                detected_at=observed_at,
                ticker=(matched or {}).get("ticker"),
                source=source,
                observed_at=observed_at,
                block_number=slot,
            )
        )
    return events


def parse_webhook(
    provider: str,
    payload: Any,
    *,
    observed_at: datetime | None = None,
    registry: TokenRegistry | None = None,
    source: str = "onchain_webhooks",
) -> list[SignalEvent]:
    """Parse a body for ``provider``. An unknown provider yields no events (never guesses)."""
    if provider == "alchemy":
        return parse_alchemy_webhook(
            payload, observed_at=observed_at, registry=registry, source=source
        )
    if provider == "helius":
        return parse_helius_webhook(
            payload, observed_at=observed_at, registry=registry, source=source
        )
    return []


# ----------------------------------------------------------------------------------------------
# The receiver
# ----------------------------------------------------------------------------------------------


@dataclass
class WebhookResult:
    """What the receiver did with one POST: the status to return and the body to send."""

    status: int
    body: dict[str, Any]


class WebhookReceiver:
    """Loopback HTTP receiver for Alchemy/Helius webhooks.

    ``handle`` is a pure function of (raw body, headers) so the signature and attribution behaviour is
    testable without opening a socket; ``run`` serves it with the standard library's
    ``ThreadingHTTPServer`` until the stop event is set. It has the same ``name`` / ``run(stop_event)``
    / ``stats`` surface as a :class:`~agentoquant.data.early_signals.Listener`, so the runner starts it
    like any other listener.
    """

    name: ClassVar[str] = "onchain_webhooks"

    def __init__(
        self,
        writer: SignalWriter,
        *,
        provider: str = "alchemy",
        secret: str | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        registry: TokenRegistry | None = None,
        registry_path: Path | str | None = None,
        log: JsonlLog | None = None,
        block_height_provider: Callable[[], int] | None = None,
        max_body_bytes: int = MAX_BODY_BYTES,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if provider not in SECRET_SOURCES:
            raise ValueError(f"unknown webhook provider {provider!r}; expected alchemy or helius")
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"refusing to bind {host!r}: this receiver is loopback only "
                f"(a public ingress is Task 6's job)"
            )
        resolved_secret = secret if secret is not None else resolve_webhook_secret(provider)
        if not resolved_secret:
            # Fail closed: an unauthenticated webhook receiver accepts forged events.
            raise RuntimeError(
                f"no signing secret configured for the {provider} webhook receiver; refusing to "
                f"start unauthenticated (set the environment variable or the credentials-file key "
                f"named in SECRET_SOURCES)"
            )
        self.writer = writer
        self.provider = provider
        self.host = host
        self.port = port
        self.log = log
        self.clock = clock
        self.registry = (
            registry if registry is not None else TokenRegistry.load(registry_path)
        )
        self.block_height_provider = block_height_provider
        self.max_body_bytes = max_body_bytes
        self.stats = ListenerStats()
        self._secret = resolved_secret
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return ALCHEMY_PATH if self.provider == "alchemy" else HELIUS_PATH

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, listener=self.name, provider=self.provider, **fields)

    # -- the request path ---------------------------------------------------------------------

    def handle(self, body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        """Verify, parse, attribute and write. Fail closed on anything that is not authentic."""
        self.stats.polls += 1
        self.stats.last_poll_at = self.clock()
        if len(body) > self.max_body_bytes:
            self.stats.failures += 1
            self._log("webhook_rejected", reason="body_too_large", bytes=len(body))
            return WebhookResult(413, {"error": "body too large"})

        if not verify_signature(self.provider, body, headers, self._secret):
            # Nothing is parsed and nothing is written. The presented value is never logged.
            self.stats.failures += 1
            self.stats.consecutive_failures += 1
            self.stats.last_error = "signature_missing_or_invalid"
            self.stats.last_error_at = self.clock()
            presented = any(
                key.lower() in (ALCHEMY_SIGNATURE_HEADER, HELIUS_AUTH_HEADER) for key in headers
            )
            self._log(
                "webhook_rejected",
                reason="signature_present_but_invalid" if presented else "signature_missing",
                bytes=len(body),
            )
            LOGGER.warning("%s: rejected a %s webhook (bad signature)", self.name, self.provider)
            return WebhookResult(401, {"error": "invalid signature"})

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.stats.failures += 1
            self._log("webhook_rejected", reason="malformed_json", error=type(exc).__name__)
            return WebhookResult(400, {"error": "malformed JSON"})

        observed_at = self.clock()
        events = parse_webhook(
            self.provider,
            payload,
            observed_at=observed_at,
            registry=self.registry,
            source=self.name,
        )
        written = self.writer.write_all(events)
        self.stats.events += len(events)
        self.stats.written += len(written)
        self.stats.duplicates += len(events) - len(written)
        self.stats.consecutive_failures = 0
        self.stats.last_error = None
        self.stats.last_success_at = observed_at

        block_lags: list[int] = []
        if self.block_height_provider is not None:
            try:
                head = int(self.block_height_provider())
            except Exception:  # noqa: BLE001 - a height probe must never fail the webhook
                head = None
            if head is not None:
                block_lags = [
                    head - event.block_number
                    for event in events
                    if event.block_number is not None
                ]
        attributed = sum(1 for event in events if event.ticker)
        self._log(
            "webhook_accepted",
            events=len(events),
            written=len(written),
            attributed=attributed,
            unattributed=len(events) - attributed,
            block_numbers=[event.block_number for event in events],
            block_lags=block_lags,
        )
        return WebhookResult(
            200,
            {
                "provider": self.provider,
                "events": len(events),
                "written": len(written),
                "attributed": attributed,
                "unattributed": len(events) - attributed,
                "block_lags": block_lags,
            },
        )

    def handle_health(self) -> WebhookResult:
        """Liveness for the operator, without touching the ledger or any source."""
        return WebhookResult(
            200,
            {
                "status": "ok",
                "provider": self.provider,
                "registry_entries": len(self.registry),
                "uptime_since": (
                    self.stats.started_at.isoformat() if self.stats.started_at else None
                ),
            },
        )

    # -- the server ---------------------------------------------------------------------------

    def _make_server(self) -> ThreadingHTTPServer:
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "AgentoquantWebhook/0.1"

            def _respond(self, result: WebhookResult) -> None:
                encoded = json.dumps(result.body).encode("utf-8")
                self.send_response(result.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
                if self.path.rstrip("/") != receiver.path:
                    self._respond(WebhookResult(404, {"error": "unknown path"}))
                    return
                length_header = self.headers.get("Content-Length") or "0"
                try:
                    length = int(length_header)
                except ValueError:
                    self._respond(WebhookResult(400, {"error": "bad Content-Length"}))
                    return
                body = self.rfile.read(length) if length > 0 else b""
                self._respond(receiver.handle(body, dict(self.headers.items())))

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") == HEALTH_PATH.rstrip("/"):
                    self._respond(receiver.handle_health())
                    return
                self._respond(WebhookResult(404, {"error": "unknown path"}))

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                LOGGER.debug("%s - %s", self.address_string(), format % args)

        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        return server

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Serve until ``stop_event`` is set. Never raises out of the loop."""
        stop_event = stop_event or threading.Event()
        self.stats.started_at = self.clock()
        try:
            server = self._make_server()
        except OSError as exc:
            self.stats.failures += 1
            self.stats.last_error = f"bind failed: {exc}"
            LOGGER.error("%s could not bind %s:%s: %s", self.name, self.host, self.port, exc)
            self._log("listener_start_failed", error=str(exc)[:300])
            return
        with self._lock:
            self._server = server
        self.port = int(server.server_address[1])
        self._log("listener_start", host=self.host, port=self.port, registry_entries=len(self.registry))
        LOGGER.info(
            "%s listening on http://%s:%s%s (registry entries: %d)",
            self.name,
            self.host,
            self.port,
            self.path,
            len(self.registry),
        )

        def _shutdown() -> None:
            stop_event.wait()
            server.shutdown()

        watchdog = threading.Thread(target=_shutdown, name=f"{self.name}-watchdog", daemon=True)
        watchdog.start()
        try:
            server.serve_forever(poll_interval=0.25)
        except Exception as exc:  # noqa: BLE001 - the runner must never see a raise from a listener
            self.stats.failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
            LOGGER.error("%s serve_forever failed: %s", self.name, exc)
        finally:
            server.server_close()
            with self._lock:
                self._server = None
            self._log("listener_stop", **self.stats.snapshot())

    def stop(self) -> None:
        """Shut the server down from another thread (used by tests and by the runner)."""
        with self._lock:
            server = self._server
        if server is not None:
            server.shutdown()

    @property
    def bound_port(self) -> int:
        """The port actually bound (``port=0`` asks the OS for a free one, which tests use)."""
        return self.port


__all__ = [
    "ALCHEMY_PATH",
    "ALCHEMY_SIGNATURE_HEADER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_REGISTRY_PATH",
    "HELIUS_AUTH_HEADER",
    "HELIUS_PATH",
    "HEALTH_PATH",
    "KIND_TOKEN",
    "KIND_UNLOCK",
    "LOOPBACK_HOSTS",
    "MAX_BODY_BYTES",
    "SECRET_SOURCES",
    "SignatureError",
    "TokenRegistry",
    "WebhookReceiver",
    "WebhookResult",
    "alchemy_signature",
    "parse_alchemy_webhook",
    "parse_helius_webhook",
    "parse_webhook",
    "resolve_webhook_secret",
    "verify_alchemy_signature",
    "verify_helius_auth",
    "verify_signature",
]
