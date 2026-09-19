"""The funding floor and the Funding Request flow.

Owned by Task 5. Two jobs:

1. :func:`free_cash_floor_pct_of_book` turns ``config/risk_limits.yaml``'s
   ``funding.free_cash_floor_pct`` (a percent of that sleeve's *target*) into a percent of **book
   value**, which is the unit the Risk Gate sizes in.
2. :func:`raise_funding_request` writes a ``funding_request`` ledger record and
   :func:`render_card` renders the Telegram card. The gate calls the first; the
   ``agentoquant fund request`` CLI calls both.

Safety: this module **never moves funds** and never calls an exchange write endpoint. It imports no
exchange client at all. A Funding Request is a card a human answers (``/fund approve`` or
``/fund decline``); Shahrad moves the money himself. That is a plan rule, not a convenience.

The monthly cap (``funding.monthly_request_cap``, "so the bot cannot ask repeatedly") is enforced by
counting the ``funding_request`` rows already written in the current calendar month.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agentoquant.config_loader import RiskLimits, Sleeves, load_risk_limits, load_settings
from agentoquant.enums import Sleeve, Stage
from agentoquant.ledger.schema import FundingRequestPayload
from agentoquant.ledger.store import LedgerStore

#: The addendum's two funding-request reasons.
FUNDING_REASON_FLOOR_BREACH = "funding_floor_breach"
FUNDING_REASON_UNSIZEABLE = "unsizeable_high_confidence_card"
FUNDING_REASONS: tuple[str, ...] = (FUNDING_REASON_FLOOR_BREACH, FUNDING_REASON_UNSIZEABLE)

#: The addendum's funding-request statuses.
STATUS_REQUESTED = "requested"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"
STATUS_ARRIVED = "arrived"
STATUS_EXPIRED = "expired"

#: Default time a Funding Request card stays open.
DEFAULT_DEADLINE_HOURS = 48

#: Currencies a request may be denominated in (settings.capital.currency is CAD).
FUNDING_CURRENCIES: tuple[str, ...] = ("CAD", "USD")


def _settings_timezone() -> ZoneInfo:
    """``settings.timezone`` (``America/Toronto``); UTC if the config cannot be read."""
    try:
        return ZoneInfo(load_settings().timezone)
    except Exception:  # pragma: no cover - a broken config is a startup failure elsewhere
        return ZoneInfo("UTC")


def _aware(moment: datetime) -> datetime:
    """Aware UTC. A naive datetime is read as UTC rather than silently reinterpreted."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def free_cash_floor_pct_of_book(sleeve: Sleeve, limits: RiskLimits, sleeves: Sleeves) -> float:
    """The sleeve's free-cash floor expressed as a percent of **book value**.

    ``config/risk_limits.yaml`` states ``funding.free_cash_floor_pct`` as a percent of that sleeve's
    target; the sleeve's ``cap_pct`` in ``config/sleeves.yaml`` is that target. With the committed
    config that is 10 percent of A's 100, 10 percent of B's 35 and 10 percent of C's 20, so the floor
    is 10, 3.5 and 2 percent of book value respectively.
    """
    floor_pct_of_sleeve = limits.funding.free_cash_floor_pct[sleeve]
    sleeve_target_pct = sleeves.get(sleeve).cap_pct
    return floor_pct_of_sleeve * sleeve_target_pct / 100.0


def requests_this_month(ledger: LedgerStore, *, now: datetime) -> int:
    """How many Funding Requests have already been raised in the current calendar month.

    The month boundary is the settings timezone's (``America/Toronto``), the same clock the daily
    halt and the daily review use.
    """
    moment = _aware(now).astimezone(_settings_timezone())
    start_local = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    rows = ledger.query(
        'SELECT COUNT(*) AS n FROM "funding_request" WHERE "requested_at" >= ? AND "requested_at" < ?',
        [start_local.astimezone(UTC), end_local.astimezone(UTC)],
    )
    return int(rows[0]["n"]) if rows else 0


def build_funding_request(
    *,
    sleeve: Sleeve,
    amount: float,
    reason: str,
    requested_at: datetime,
    currency: str = "CAD",
    deadline_hours: int = DEFAULT_DEADLINE_HOURS,
) -> FundingRequestPayload:
    """The addendum's ``funding_request`` payload. Status is always ``requested`` on creation."""
    moment = _aware(requested_at)
    return FundingRequestPayload(
        sleeve=sleeve,
        amount=float(amount),
        currency=currency,
        reason=reason,
        deadline=moment + timedelta(hours=int(deadline_hours)),
        status=STATUS_REQUESTED,
        requested_at=moment,
        responded_at=None,
        arrived_at=None,
    )


def raise_funding_request(
    ledger: LedgerStore,
    *,
    sleeve: Sleeve,
    amount: float,
    reason: str,
    cycle_id: str,
    now: datetime,
    currency: str = "CAD",
    deadline_hours: int = DEFAULT_DEADLINE_HOURS,
    monthly_cap: int | None = None,
) -> str | None:
    """Write a ``funding_request`` record and return its ``record_id``.

    Returns ``None`` when the monthly cap is already reached, so the bot cannot ask repeatedly. It
    never moves funds and never calls an exchange: it writes one ledger row and nothing else.
    """
    if monthly_cap is not None and requests_this_month(ledger, now=now) >= monthly_cap:
        return None
    payload = build_funding_request(
        sleeve=sleeve,
        amount=amount,
        reason=reason,
        requested_at=now,
        currency=currency,
        deadline_hours=deadline_hours,
    )
    return ledger.write(
        Stage.FUNDING_REQUEST, cycle_id, payload, producer_role="funding_floor"
    )


def render_card(payload: FundingRequestPayload, *, record_id: str | None = None) -> str:
    """The Telegram Funding Request card: amount, sleeve, reason, deadline. Nothing else."""
    lines = [
        "FUNDING REQUEST",
        f"sleeve:   {payload.sleeve.value if isinstance(payload.sleeve, Sleeve) else payload.sleeve}",
        f"amount:   {payload.amount:.2f} {payload.currency}",
        f"reason:   {payload.reason}",
        f"deadline: {payload.deadline.isoformat(timespec='minutes')}",
        f"status:   {payload.status}",
        "",
        "The agent never moves funds and holds no withdrawal permission.",
        "Reply /fund approve or /fund decline",
    ]
    if record_id:
        lines.insert(1, f"id:       {record_id}")
    return "\n".join(lines)


def _coerce_sleeve(value: Any) -> Sleeve:
    if isinstance(value, Sleeve):
        return value
    return Sleeve(str(value).strip().upper())


def cli(
    sleeve: Sleeve | str,
    amount: float,
    currency: str = "CAD",
    reason: str = FUNDING_REASON_FLOOR_BREACH,
    deadline_hours: int = DEFAULT_DEADLINE_HOURS,
) -> dict[str, Any]:
    """``agentoquant fund request``: raise a Funding Request card and print it.

    Entry point named by ``cli.COMMAND_TARGETS["fund"]``. It writes one ledger row and prints the
    card; it never moves funds and never calls an exchange write endpoint.
    """
    resolved = _coerce_sleeve(sleeve)
    if reason not in FUNDING_REASONS:
        return {
            "status": "error",
            "message": f"unknown reason {reason!r}; expected one of {', '.join(FUNDING_REASONS)}",
        }
    if currency not in FUNDING_CURRENCIES:
        return {
            "status": "error",
            "message": f"unknown currency {currency!r}; expected one of {', '.join(FUNDING_CURRENCIES)}",
        }
    if amount <= 0:
        return {"status": "error", "message": "amount must be greater than zero"}

    now = datetime.now(UTC)
    cycle_id = f"fund-request-{now.strftime('%Y-%m-%dT%H:%MZ')}"
    ledger = LedgerStore()
    limits = _risk_limits_for_cap()
    monthly_cap = limits.funding.monthly_request_cap if limits is not None else None
    record_id = raise_funding_request(
        ledger,
        sleeve=resolved,
        amount=amount,
        reason=reason,
        cycle_id=cycle_id,
        now=now,
        currency=currency,
        deadline_hours=deadline_hours,
        monthly_cap=monthly_cap,
    )
    ledger.close()
    if record_id is None:
        return {
            "status": "error",
            "message": (
                f"monthly funding-request cap reached ({monthly_cap}); "
                "no new card raised, the floor still blocks the entry"
            ),
        }
    payload = build_funding_request(
        sleeve=resolved,
        amount=amount,
        reason=reason,
        requested_at=now,
        currency=currency,
        deadline_hours=deadline_hours,
    )
    card = render_card(payload, record_id=record_id)
    print(card)
    return {
        "status": "ok",
        "message": f"funding request {record_id} raised for sleeve {resolved.value}",
        "record_id": record_id,
        "sleeve": resolved.value,
        "amount": payload.amount,
        "currency": payload.currency,
        "reason": payload.reason,
        "deadline": payload.deadline.isoformat(),
        "status_field": payload.status,
        "card": card,
        "moved_funds": False,
    }


def _risk_limits_for_cap() -> RiskLimits | None:
    """``config/risk_limits.yaml`` for the monthly cap; ``None`` if it cannot be read."""
    try:
        return load_risk_limits()
    except Exception:  # pragma: no cover - a broken config is a startup failure elsewhere
        return None


__all__ = [
    "DEFAULT_DEADLINE_HOURS",
    "FUNDING_CURRENCIES",
    "FUNDING_REASONS",
    "FUNDING_REASON_FLOOR_BREACH",
    "FUNDING_REASON_UNSIZEABLE",
    "STATUS_APPROVED",
    "STATUS_ARRIVED",
    "STATUS_DECLINED",
    "STATUS_EXPIRED",
    "STATUS_REQUESTED",
    "build_funding_request",
    "cli",
    "free_cash_floor_pct_of_book",
    "raise_funding_request",
    "render_card",
    "requests_this_month",
]
