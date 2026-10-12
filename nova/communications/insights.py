"""The weekly communications digest: what calls achieved, what they cost, what needs a person.

Built from counts only — no contact, no number, nothing anyone said — so it can go to a
team inbox or a channel without becoming a second place customer data lives. On demand from
``nova calls insights`` and ``GET /communications/insights``; and, when the tenant declares
``communications.insights``, delivered once per week by the sweep as work for an agent
and/or an email to staff addresses, on the configured weekday, for the week that ended.

Delivered at most once per week: the record is written only after delivery succeeded, and
both deliveries are idempotent on the week, so a crash in between sends nothing twice.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from nova.audit import new_correlation_id
from nova.communications.spec_insights import WEEKDAYS

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
#: Statuses that mean the call reached a person who talked to the agent.
_CONNECTED = ("session_connected", "completed", "transferred")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def last_week(now: datetime) -> tuple[str, str, str]:
    """``(since, until, period)`` for the last complete ISO week, Monday to Monday UTC."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    until = today - timedelta(days=today.weekday())
    since = until - timedelta(days=7)
    year, week, _ = since.isocalendar()
    return _iso(since), _iso(until), f"{year}-W{week:02d}"


def _blocked(rows: list[tuple]) -> list[dict[str, Any]]:
    """The gate's reasons, grouped by their shape. Its wording quotes the contact, the agent
    and the purpose; every quoted value is removed, so no contact reaches the digest."""
    counts: dict[str, int] = {}
    for reason, count in rows:
        shape = _QUOTED.sub("…", reason or "unspecified")
        counts[shape] = counts.get(shape, 0) + count
    return [{"reason": r, "count": c}
            for r, c in sorted(counts.items(), key=lambda item: -item[1])[:5]]


def digest(gateway, *, since: str, until: str) -> dict[str, Any]:
    store, budget = gateway.store, gateway.spec.budget
    calls = {"requested": 0, "connected": 0, "completed": 0, "minutes": 0, "failed": {},
             "blocked": 0}
    agents: dict[str, dict[str, int]] = {}
    purposes: dict[str, dict[str, int]] = {}
    consent = {"granted": 0, "refused": 0, "unclear": 0}
    engines: dict[str, int] = {}
    spend = 0.0
    for agent, purpose, status, failure, carrier, engine, recorded, count, minutes in \
            store.call_outcomes(since, until):
        calls["requested"] += count
        connected = status in _CONNECTED
        calls["connected"] += count if connected else 0
        calls["completed"] += count if status == "completed" else 0
        calls["minutes"] += minutes
        if status == "failed":
            calls["failed"][failure or "unknown"] = calls["failed"].get(failure or "unknown", 0) + count
        if status == "policy_blocked":
            calls["blocked"] += count
        for table, key in ((agents, agent), (purposes, purpose)):
            row = table.setdefault(key, {"requested": 0, "connected": 0})
            row["requested"] += count
            row["connected"] += count if connected else 0
        if recorded in consent:
            consent[recorded] += count
        if engine:
            engines[engine] = engines.get(engine, 0) + count
        spend += minutes * (budget.rate(carrier).per_minute +
                            (budget.rate(engine).per_minute if engine else 0.0))
    actions: dict[str, dict[str, int]] = {}
    for kind, tier, status, count in store.action_outcomes(since, until):
        entry = actions.setdefault(kind, {})
        entry[status] = entry.get(status, 0) + count
    asked = sum(consent.values())
    return {
        "since": since, "until": until,
        "calls": {**calls, "connect_rate": round(calls["connected"] / calls["requested"], 3)
                  if calls["requested"] else None},
        "blocked_reasons": _blocked(store.blocked_reasons(since, until)),
        "by_agent": agents, "by_purpose": purposes,
        "recording_consent": {**consent, "yes_rate": round(consent["granted"] / asked, 3)
                              if asked else None},
        "opt_outs": store.opt_outs(since, until),
        "actions": actions,
        "waiting_for_people": {
            "actions": len(store.actions(status="awaiting_approval", limit=1000)),
            "reviews": store.pending_reviews(),
            "calls": len(store.calls_with_status(["approval_required"])),
        },
        "delegations": dict(store.delegation_outcomes(since, until)),
        "engines": {"calls": engines,
                    "failures": dict(store.engine_failure_counts(since, until))},
        "spend": {"estimated": round(spend, 2), "currency": budget.currency,
                  "monthly_limit": budget.monthly_limit or None},
    }


def render(report: dict[str, Any], tenant: str) -> str:
    """The digest as plain text, for a work item or an email."""
    c = report["calls"]
    lines = [
        f"Communications, {report['since'][:10]} to {report['until'][:10]} ({tenant})",
        "",
        f"Calls: {c['requested']} requested, {c['connected']} connected"
        + (f" ({c['connect_rate']:.0%})" if c["connect_rate"] is not None else "")
        + f", {c['completed']} completed, {c['minutes']} minutes.",
    ]
    if c["failed"]:
        lines.append("Did not connect: " + ", ".join(f"{k.replace('_', ' ')} {v}"
                                                      for k, v in sorted(c["failed"].items())))
    if c["blocked"]:
        lines.append(f"Refused by the gate: {c['blocked']}"
                     + "".join(f"\n  - {r['reason']} ({r['count']})"
                               for r in report["blocked_reasons"]))
    for agent, row in sorted(report["by_agent"].items()):
        lines.append(f"  {agent}: {row['connected']} of {row['requested']} connected")
    consent = report["recording_consent"]
    if consent["yes_rate"] is not None:
        lines.append(f"Recording consent: {consent['granted']} yes, "
                     f"{consent['refused'] + consent['unclear']} no ({consent['yes_rate']:.0%} yes)")
    opt = report["opt_outs"]
    lines.append(f"Opt-outs: {opt['all_channels']} stopped all contact"
                 + "".join(f", {v} {k[6:].replace('_', ' ')}" for k, v in opt.items()
                           if k.startswith("email_")))
    if report["actions"]:
        lines.append("After-call actions: " + "; ".join(
            f"{kind} " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(st.items()))
            for kind, st in sorted(report["actions"].items())))
    if report["delegations"]:
        lines.append("Handed between agents: " + ", ".join(
            f"{n} {s}" for s, n in sorted(report["delegations"].items())))
    if report["engines"]["failures"]:
        lines.append("Voice engine failures: " + ", ".join(
            f"{e} {n}" for e, n in sorted(report["engines"]["failures"].items())))
    spend = report["spend"]
    lines.append(f"Estimated spend: {spend['estimated']:.2f} {spend['currency']}"
                 + (f" (monthly limit {spend['monthly_limit']:.2f})" if spend["monthly_limit"]
                    else ""))
    waiting = report["waiting_for_people"]
    lines += ["", f"Waiting for a person now: {waiting['calls']} call approval(s), "
                  f"{waiting['actions']} action(s), {waiting['reviews']} field review(s)."]
    return "\n".join(lines)


def deliver_weekly(gateway) -> str:
    """Send last week's digest if today is the day and it has not gone out. The period, or ''."""
    spec = gateway.spec.insights
    now = gateway.now()
    if not spec.declared or WEEKDAYS[now.astimezone(timezone.utc).weekday()] != spec.weekday:
        return ""
    since, until, period = last_week(now)
    store = gateway.store
    if store.digest_sent(period):
        return ""
    text = render(digest(gateway, since=since, until=until), store.tenant_id)
    if spec.assignee:
        if gateway.tasks is None:
            return ""  # no board yet: try again next sweep
        from nova.runtime.base import WorkItem

        gateway.tasks(WorkItem(key=f"insights:{period}", assignee=spec.assignee, priority=3,
                               title=f"Weekly communications digest {period}", body=text,
                               tenant_id=store.tenant_id))
    if spec.email_to:
        if gateway.email is None:
            return ""
        from nova.communications.mail import OutboundEmail

        for n, address in enumerate(spec.email_to):
            gateway.email.send(OutboundEmail(
                email_id=f"insights-{period}-{n}",
                idempotency_key=f"nova-insights-{store.tenant_id}-{period}-{n}",
                sender=gateway.spec.email.sender, to=address,
                subject=f"Weekly communications digest {period}", text=text))
    store.mark_digest(period, at=_iso(now))
    gateway.audit.record("communications.insights.delivered", correlation_id=new_correlation_id(),
                         subject=period, detail={"assignee": spec.assignee,
                                                 "emails": len(spec.email_to)})
    return period
