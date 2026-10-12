"""What this month's calls and emails cost, whether the budget allows more, and the bill.

**Estimated** cost is billed minutes (rounded up per call, as carriers bill) times the
tenant's per-minute rate for the carrier and for the voice engine, plus emails sent times
the email rate. **Billed** cost is what the carrier itself reports once it settles a call;
when it is known (and in the budget's currency) it replaces the carrier's estimate. Engine
and email costs stay estimates: those vendors bill monthly, not per call.

**The hard limit counts what is already committed.** A call on the line has no duration yet,
so it is counted at its agent's maximum call length until it ends. That is what makes the
limit hold: a burst of calls placed together cannot overshoot it. New calls are refused at
the gate once spend plus commitments reach the limit; calls in progress are never cut off.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, UTC
from typing import Any, Optional

from nova.audit import new_correlation_id
from nova.communications.contracts import ACTIVE, CallStatus, ProviderError

_ACTIVE = {status.value for status in ACTIVE}
#: A finished call is asked about its price after this long: carriers settle late.
BILLING_AFTER = timedelta(minutes=10)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _minutes(seconds: int) -> int:
    return math.ceil(max(0, seconds) / 60)


def call_cost(spec, row: dict, *, seconds: Optional[int] = None) -> dict[str, float]:
    """One call's estimated and best-known cost. ``seconds`` overrides its duration."""
    budget = spec.budget
    minutes = _minutes(row["duration_seconds"] if seconds is None else seconds)
    carrier = minutes * budget.rate(row["telephony"]).per_minute
    engine = minutes * budget.rate(row["voice_engine"]).per_minute if row["voice_engine"] else 0.0
    billed = row.get("billed_price")
    known = billed if billed is not None and row.get("billed_currency") in ("", budget.currency) \
        else carrier
    return {"minutes": minutes, "estimated": round(carrier + engine, 6),
            "best_known": round(known + engine, 6)}


def month_to_date(gateway) -> dict[str, Any]:
    """This month's usage: totals, per agent and per provider, and against the limit."""
    spec, store, budget = gateway.spec, gateway.store, gateway.spec.budget
    since = _iso(month_start(gateway.now()))
    totals = {"calls": 0, "minutes": 0, "estimated": 0.0, "spend": 0.0, "billed": 0.0,
              "committed": 0.0}
    by_agent: dict[str, dict[str, float]] = {}
    by_provider: dict[str, dict[str, float]] = {}
    used: set[str] = set()
    for row in store.usage_rows(since):
        used.update(p for p in (row["telephony"], row["voice_engine"]) if p)
        if row["status"] in _ACTIVE:
            profile = spec.profile(row["agent"])
            reserve = profile.max_call_seconds if profile else 3600
            totals["committed"] += call_cost(spec, row, seconds=reserve)["estimated"]
            continue
        cost = call_cost(spec, row)
        totals["calls"] += 1
        totals["minutes"] += cost["minutes"]
        totals["estimated"] += cost["estimated"]
        totals["spend"] += cost["best_known"]
        totals["billed"] += row["billed_price"] or 0.0
        agent = by_agent.setdefault(row["agent"], {"calls": 0, "minutes": 0, "spend": 0.0})
        agent["calls"] += 1
        agent["minutes"] += cost["minutes"]
        agent["spend"] += cost["best_known"]
        for provider in (row["telephony"], row["voice_engine"]):
            if provider:
                entry = by_provider.setdefault(provider, {"minutes": 0, "estimated": 0.0})
                entry["minutes"] += cost["minutes"]
                entry["estimated"] += cost["minutes"] * budget.rate(provider).per_minute
    emails = store.emails_sent_since(since)
    if spec.email.provider and emails:
        used.add(spec.email.provider)
        email_cost = emails * budget.rate(spec.email.provider).per_email
        totals["estimated"] += email_cost
        totals["spend"] += email_cost
        by_provider[spec.email.provider] = {"emails": emails, "estimated": email_cost}
    limit = budget.monthly_limit
    exposure = totals["spend"] + totals["committed"]
    return {
        "month": since[:7], "currency": budget.currency, "limit": limit,
        "emails": emails,
        **{k: round(v, 4) if isinstance(v, float) else v for k, v in totals.items()},
        "remaining": round(max(limit - exposure, 0.0), 4) if limit else None,
        "percent": round(100 * exposure / limit, 1) if limit else None,
        "by_agent": by_agent, "by_provider": by_provider,
        # Used this month with no rate: the estimate leaves them out, and says so.
        "unpriced": sorted(p for p in used if p not in budget.rates),
    }


def exhausted(gateway) -> str:
    """Why the budget allows nothing new, or '' when it does."""
    limit = gateway.spec.budget.monthly_limit
    if not limit:
        return ""
    usage = month_to_date(gateway)
    if usage["spend"] + usage["committed"] >= limit:
        return (f"the monthly communications budget is reached ({usage['spend']:.2f} spent and "
                f"{usage['committed']:.2f} committed of {limit:.2f} {usage['currency']})")
    return ""


def raise_alerts(gateway) -> list[int]:
    """Alert, once a month each, on every threshold the month's spend has crossed."""
    budget = gateway.spec.budget
    if not budget.monthly_limit:
        return []
    usage = month_to_date(gateway)
    raised = []
    for percent in budget.alert_at:
        if usage["percent"] < percent:
            continue
        if not gateway.store.record_budget_alert(usage["month"], percent, spend=usage["spend"],
                                                 at=_iso(gateway.now())):
            continue
        raised.append(percent)
        gateway.audit.record(
            "communications.budget.alert", correlation_id=new_correlation_id(),
            subject=usage["month"],
            detail={"percent": percent, "spend": usage["spend"],
                    "committed": usage["committed"], "limit": budget.monthly_limit,
                    "currency": budget.currency})
        if budget.alert_assignee and gateway.tasks is not None:
            from nova.runtime.base import WorkItem

            gateway.tasks(WorkItem(
                key=f"budget-alert:{usage['month']}:{percent}",
                title=f"Communications budget at {percent}% for {usage['month']}",
                assignee=budget.alert_assignee, priority=8 if percent >= 100 else 5,
                body=(f"Spend {usage['spend']:.2f} plus {usage['committed']:.2f} committed, of "
                      f"a {budget.monthly_limit:.2f} {budget.currency} monthly limit. New calls "
                      "stop at 100%; calls in progress do not."),
                tenant_id=gateway.store.tenant_id))
    return raised


def reconcile_billing(gateway, *, limit: int = 50) -> int:
    """Ask carriers what finished calls cost. Records the bill; audits a large gap."""
    store, spec = gateway.store, gateway.spec
    settled = 0
    for call_id, telephony, ref in store.unbilled_calls(
            ended_before=_iso(gateway.now() - BILLING_AFTER), limit=limit):
        carrier = gateway.carriers.get(telephony)
        if carrier is None:
            continue
        try:
            bill = carrier.fetch_billing(ref)
        except ProviderError:
            continue  # asked again next sweep; an unreachable carrier is not a zero bill
        if bill is None:
            continue
        estimate = call_cost(spec, {"telephony": telephony, "voice_engine": "",
                                    "duration_seconds": bill.duration_seconds})["estimated"]
        store.set_billing(call_id, seconds=bill.duration_seconds, price=bill.price,
                          currency=bill.currency)
        settled += 1
        if spec.budget.rate(telephony).per_minute and abs(bill.price - estimate) > \
                max(0.01, 0.2 * estimate):
            gateway.audit.record(
                "communications.billing.mismatch", correlation_id=new_correlation_id(),
                subject=call_id,
                detail={"carrier": telephony, "billed": bill.price, "estimated": estimate,
                        "currency": bill.currency})
    return settled


def finished_cost(spec, store, record) -> Optional[dict[str, float]]:
    """A call's cost for the history: None while it is on the line or was never dialled."""
    if record.status.value in _ACTIVE or record.status in (
            CallStatus.REQUESTED, CallStatus.APPROVAL_REQUIRED):
        return None
    usage = store.call_usage(record.call_id) or {"duration_seconds": 0, "billed_price": None,
                                                 "billed_currency": ""}
    return call_cost(spec, {"telephony": record.telephony, "voice_engine": record.voice_engine,
                            **usage})
