"""Meetings an agent books itself: requested through its signed tool, decided here.

An agent names a contact, a purpose, an event type and a time; it never supplies an address.
The gate below — the meeting twin of the email gate — decides, in order: the agent's grant,
the purpose, the event type and that it is for this purpose, the contact and its group, an
address on file (the calendar provider sends the invitation there), opt-outs, marketing
consent for a marketing purpose, that the time is neither too soon nor too far ahead, the
agent's daily cap, and the grant's approval mode.

**Approval is bound to the meeting.** A person approves the exact address, event type, time
and length (a digest of them); if the contact's address changed since, the approval is
refused. Approving re-runs the gate, so consent withdrawn in the meantime still stops it.

**Once.** The request id is the key: picked up twice it is one request; claimed for booking
by one conditional update. The calendar provider has no idempotency key to lean on, so every
booking is preceded by a search for one already made for this request (``find``), and a
request left mid-booking by a crash, or by a provider that did not answer, is settled by the
sweep the same way: find first, book only if none exists.

**Afterwards.** The contact's own cancel or reschedule arrives as a signed webhook and is
applied once; a contact who opts out has their upcoming meetings cancelled.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nova.audit import new_correlation_id
from nova.communications.booking import (
    Booked,
    BookingChange,
    BookingEvent,
    BookingRequest,
    BookingUnconfirmed,
    SlotUnavailable,
)
from nova.communications.contracts import ProviderError
from nova.communications.followup_email import EMAIL_CHANNEL
from nova.communications.spec import CONSENT_EMAIL_MARKETING

#: A request left ``sending`` this long belongs to a process that died, or a provider that
#: did not answer; the sweep settles it. Past ``GIVE_UP_AFTER`` it is failed, not retried.
STUCK_AFTER = timedelta(minutes=10)
GIVE_UP_AFTER = timedelta(hours=24)
ALTERNATIVES = 5

DENY, APPROVE, ALLOW = "deny", "approval_required", "allow"


def _iso(gateway, delta: timedelta = timedelta(0)) -> str:
    from nova.communications.actions import _iso as iso

    return iso(gateway.now() + delta)


def digest(to: str, event_type: str, start: str, duration_minutes: int, purpose: str) -> str:
    text = f"{to}\n{event_type}\n{start}\n{duration_minutes}\n{purpose}"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_start(text: str, zone: str) -> str:
    """A time as UTC ``Z``. Without an offset it is read in ``zone``, the contact's own.
    ``ValueError`` for anything that is not one definite moment."""
    try:
        moment = datetime.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise ValueError("start must be an ISO 8601 time, e.g. 2026-10-13T10:30:00-05:00") from exc
    if moment.tzinfo is None:
        try:
            moment = moment.replace(tzinfo=ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError("start has no offset and the contact has no known time zone") from exc
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(start: str) -> datetime:
    return datetime.fromisoformat(start.replace("Z", "+00:00"))


def decide(gateway, agent: str, contact_id: str, purpose_id: str, event_type_id: str,
           start: str) -> tuple[str, str]:
    """``(effect, reason)``. Fail-closed: the first check that fails decides."""
    spec, store = gateway.spec, gateway.store
    grant = spec.meetings.grant(agent)
    contact = store.contact(contact_id)
    purpose = spec.purposes.get(purpose_id)
    event_type = spec.meetings.event_type(event_type_id)
    now = gateway.now()
    since = _iso(gateway, -timedelta(days=1))

    def moment() -> datetime:
        return _parse(start)

    checks: list[tuple[Callable[[], bool], str]] = [
        (lambda: grant is None, f"agent {agent!r} has no meeting grant"),
        (lambda: purpose_id not in grant.purposes or purpose is None,
         f"agent {agent!r} may not book for purpose {purpose_id!r}"),
        (lambda: event_type is None or event_type_id not in grant.event_types,
         f"agent {agent!r} may not book event type {event_type_id!r}"),
        (lambda: purpose_id not in event_type.purposes,
         f"event type {event_type_id!r} is not for purpose {purpose_id!r}"),
        (lambda: contact is None, "the contact does not exist in this tenant"),
        (lambda: not set(contact.groups) & set(grant.contact_groups),
         "the contact is in none of the groups this agent may book"),
        (lambda: not contact.email, "no email address on file for this contact"),
        (lambda: store.is_suppressed(contact_id) or store.channel_suppressed(contact_id,
                                                                             EMAIL_CHANNEL),
         "the contact opted out"),
        (lambda: purpose.marketing and CONSENT_EMAIL_MARKETING not in store.consents(contact_id),
         "a marketing purpose needs email_marketing consent on record"),
        (lambda: moment() < now + timedelta(hours=spec.meetings.min_notice_hours),
         f"that time is less than {spec.meetings.min_notice_hours} hours away"),
        (lambda: moment() > now + timedelta(days=spec.meetings.horizon_days),
         f"that time is more than {spec.meetings.horizon_days} days away"),
        (lambda: store.count_agent_meetings_since(agent, since) >= grant.max_per_day,
         "the agent has booked its daily limit of meetings"),
    ]
    try:
        reason = next((message for failed, message in checks if failed()), "")
    except Exception as exc:  # noqa: BLE001 — a gate that raises must still say no
        return DENY, f"the meeting gate failed and refused by default: {exc}"
    if reason:
        return DENY, reason
    if grant.approval == "always" or (grant.approval == "first_contact"
                                      and not store.met_before(agent, contact_id)):
        return APPROVE, "this agent's meetings need a person's approval"
    return ALLOW, "every check passed"


def view(request: dict) -> dict[str, Any]:
    """What the agent's tool reads back. Never the address."""
    return {"request_id": request["request_id"], "status": request["status"],
            "reason": request["reason"], "start": request["start"],
            "event_type": request["event_type"]}


def request_meeting(gateway, *, agent: str, request_id: str, contact_id: str, purpose: str,
                    event_type: str, start: str, task_id: str = "") -> dict:
    contact = gateway.store.contact(contact_id)
    start = normalize_start(start, contact.timezone if contact else "")
    kind = gateway.spec.meetings.event_type(event_type)
    effect, reason = decide(gateway, agent, contact_id, purpose, event_type, start)
    to = contact.email if contact else ""
    length = kind.duration_minutes if kind else 0
    status = {DENY: "blocked", APPROVE: "awaiting_approval"}.get(effect, "new")
    request, created = gateway.store.create_meeting_request(
        request_id=request_id, agent=agent, contact_id=contact_id, purpose=purpose,
        event_type=event_type, task_id=task_id[:64], to_addr=to, start=start,
        duration_minutes=length, digest=digest(to, event_type, start, length, purpose),
        status=status, reason=reason, created_at=_iso(gateway))
    if created:
        gateway.audit.record("communications.meeting.requested",
                             correlation_id=new_correlation_id(), subject=request_id,
                             detail={"agent": agent, "contact_id": contact_id, "purpose": purpose,
                                     "event_type": event_type, "status": status, "reason": reason})
        if status == "new":
            request = _book(gateway, request, actor="")
    return view(request)


def _alternatives(gateway, request: dict) -> str:
    """Open times near the one that failed, for the agent to try. Best effort."""
    kind = gateway.spec.meetings.event_type(request["event_type"])
    contact = gateway.store.contact(request["contact_id"])
    if kind is None or gateway.calendar is None:
        return ""
    around = _parse(request["start"])
    try:
        slots = gateway.calendar.slots(
            kind.provider_ref, start=(around - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=(around + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            time_zone=contact.timezone if contact else "UTC", duration_minutes=kind.duration_minutes)
    except ProviderError:
        return ""
    soonest = sorted(slots, key=lambda s: abs((_parse(s) - around).total_seconds()))
    return "; open times: " + ", ".join(sorted(soonest[:ALTERNATIVES])) if soonest else ""


def _book(gateway, request: dict, *, actor: str) -> dict:
    store, at = gateway.store, _iso(gateway)
    rid = request["request_id"]
    if request["status"] != "sending" and not store.claim_meeting_request(rid, at=at, actor=actor):
        return store.meeting_request(rid)  # someone else took it
    provider, contact = gateway.calendar, store.contact(request["contact_id"])
    kind = gateway.spec.meetings.event_type(request["event_type"])
    if provider is None or kind is None or contact is None or not request["to_addr"]:
        store.settle_meeting_request(rid, "failed", at=at, only_from=("sending",),
                                     reason="no calendar provider is configured on the gateway")
        return store.meeting_request(rid)
    booking = BookingRequest(
        request_id=rid, event_type_ref=kind.provider_ref, start=request["start"],
        duration_minutes=request["duration_minutes"],
        name=contact.name or request["to_addr"].split("@")[0], email=request["to_addr"],
        time_zone=contact.timezone or "UTC")
    outcome, detail = "", ""
    booked: Booked | None = None
    try:
        booked = provider.find(booking)
    except ProviderError as exc:
        # The provider could not say whether one exists, so booking again could double-book.
        outcome, detail = "unconfirmed", str(exc)
    else:
        if booked is None:
            try:
                booked = provider.book(booking)
            except SlotUnavailable as exc:
                outcome, detail = "failed", f"{exc}{_alternatives(gateway, request)}"
            except BookingUnconfirmed as exc:
                outcome, detail = "unconfirmed", str(exc)
            except ProviderError as exc:  # a definite refusal: wrong key, unknown event type
                outcome, detail = "failed", str(exc)
    if outcome == "unconfirmed":
        gateway.audit.record("communications.meeting.unconfirmed",
                             correlation_id=new_correlation_id(), subject=rid,
                             detail={"agent": request["agent"]}, error=detail)
        return store.meeting_request(rid)  # stays sending; the sweep settles it
    if outcome == "failed":
        store.settle_meeting_request(rid, "failed", at=_iso(gateway), reason=detail,
                                     only_from=("sending",))
    else:
        store.settle_meeting_request(
            rid, "booked", at=_iso(gateway), booking_ref=booked.ref, only_from=("sending",),
            reason="" if booked.status == "accepted" else "waiting for the host to confirm")
    record = store.meeting_request(rid)
    gateway.audit.record("communications.meeting.booked" if record["status"] == "booked" else
                         "communications.meeting.failed", correlation_id=new_correlation_id(),
                         subject=rid, detail={"agent": request["agent"], "approved_by": actor},
                         error=record["reason"] if record["status"] == "failed" else "")
    return record


def approve(gateway, request_id: str, *, actor: str) -> dict:
    """A person approved what they were shown. ``ValueError`` when it may not go."""
    if not actor:
        raise ValueError("an approval with no identity is not an approval")
    store = gateway.store
    request = store.meeting_request(request_id)
    if request is None:
        raise KeyError(f"no meeting request {request_id!r} in this tenant")
    if request["status"] != "awaiting_approval":
        raise ValueError(f"the meeting is {request['status']}, not waiting for approval")
    contact = store.contact(request["contact_id"])
    current = digest(contact.email if contact else "", request["event_type"], request["start"],
                     request["duration_minutes"], request["purpose"])
    effect, reason = decide(gateway, request["agent"], request["contact_id"], request["purpose"],
                            request["event_type"], request["start"])
    refusal = ("the contact's address changed since this meeting was requested; it was not booked"
               if current != request["digest"] else reason if effect == DENY else "")
    if refusal:
        store.settle_meeting_request(request_id, "blocked", at=_iso(gateway), reason=refusal,
                                     decided_by=actor)
        gateway.audit.record("communications.meeting.approval_refused",
                             correlation_id=new_correlation_id(), subject=request_id,
                             detail={"actor": actor, "reason": refusal})
        raise ValueError(refusal)
    return view(_book(gateway, request, actor=actor))


def reject(gateway, request_id: str, *, actor: str, reason: str = "") -> dict:
    if not actor:
        raise ValueError("a rejection with no identity is not a rejection")
    if gateway.store.meeting_request(request_id) is None:
        raise KeyError(f"no meeting request {request_id!r} in this tenant")
    if not gateway.store.settle_meeting_request(request_id, "rejected", at=_iso(gateway),
                                                reason=reason or "rejected", decided_by=actor):
        raise ValueError("the meeting is not waiting for approval")
    gateway.audit.record("communications.meeting.rejected", correlation_id=new_correlation_id(),
                         subject=request_id, detail={"actor": actor, "reason": reason})
    return view(gateway.store.meeting_request(request_id))


def cancel(gateway, request_id: str, *, actor: str, reason: str = "") -> dict:
    """Cancel a booked meeting at the provider, then here."""
    if not actor:
        raise ValueError("a cancellation with no identity is not a cancellation")
    store = gateway.store
    request = store.meeting_request(request_id)
    if request is None:
        raise KeyError(f"no meeting request {request_id!r} in this tenant")
    if request["status"] != "booked":
        raise ValueError(f"the meeting is {request['status']}, not booked")
    if gateway.calendar is None:
        raise ValueError("no calendar provider is configured on the gateway")
    gateway.calendar.cancel(request["booking_ref"], reason or "cancelled")  # ProviderError: still booked
    store.settle_meeting_request(request_id, "cancelled", at=_iso(gateway),
                                 reason=reason or "cancelled", decided_by=actor,
                                 only_from=("booked",))
    gateway.audit.record("communications.meeting.cancelled", correlation_id=new_correlation_id(),
                         subject=request_id, detail={"actor": actor, "reason": reason})
    return view(store.meeting_request(request_id))


def resume_sending(gateway) -> int:
    """Settle what a dead process or a silent provider left mid-booking: find first, then book."""
    store, count = gateway.store, 0
    for request in store.meeting_requests(status="sending"):
        if request["updated_at"] >= _iso(gateway, -STUCK_AFTER):
            continue
        count += 1
        if request["created_at"] < _iso(gateway, -GIVE_UP_AFTER):
            store.settle_meeting_request(
                request["request_id"], "failed", at=_iso(gateway), only_from=("sending",),
                reason="the calendar provider never confirmed whether this was booked")
            continue
        _book(gateway, request, actor=request["decided_by"])
    return count


def cancel_for_opted_out(gateway) -> int:
    """A contact who opted out gets no meeting they did not know they would be sent."""
    store, count = gateway.store, 0
    if gateway.calendar is None:
        return 0
    for request in store.upcoming_meetings(_iso(gateway)):
        contact_id = request["contact_id"]
        if not (store.is_suppressed(contact_id) or store.channel_suppressed(contact_id, EMAIL_CHANNEL)):
            continue
        try:
            gateway.calendar.cancel(request["booking_ref"], "the contact opted out")
        except ProviderError:
            continue  # still booked; the next sweep tries again
        if store.settle_meeting_request(request["request_id"], "cancelled", at=_iso(gateway),
                                        reason="the contact opted out", only_from=("booked",)):
            count += 1
            gateway.audit.record("communications.meeting.cancelled",
                                 correlation_id=new_correlation_id(), subject=request["request_id"],
                                 detail={"reason": "the contact opted out"})
    return count


def pending(gateway) -> list[dict]:
    """Meetings waiting for a person, with what they would book."""
    return [{k: r[k] for k in ("request_id", "agent", "contact_id", "purpose", "event_type",
                               "to_addr", "start", "duration_minutes", "reason", "created_at")}
            for r in gateway.store.meeting_requests(status="awaiting_approval")]


def open_times(gateway, *, agent: str, contact_id: str, event_type: str, day: str) -> list[str]:
    """Open times on one day for an event type this agent may book, as UTC. ``ValueError`` when
    the agent may not ask."""
    grant = gateway.spec.meetings.grant(agent)
    kind = gateway.spec.meetings.event_type(event_type)
    contact = gateway.store.contact(contact_id)
    if grant is None or kind is None or event_type not in grant.event_types:
        raise ValueError(f"agent {agent!r} may not book event type {event_type!r}")
    if contact is None or not set(contact.groups) & set(grant.contact_groups):
        raise ValueError("the contact is in none of the groups this agent may book")
    if gateway.calendar is None:
        raise ValueError("no calendar provider is configured on the gateway")
    zone = contact.timezone or "UTC"
    first = normalize_start(f"{day}T00:00:00", zone)
    last = (_parse(first) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    earliest = gateway.now() + timedelta(hours=gateway.spec.meetings.min_notice_hours)
    latest = gateway.now() + timedelta(days=gateway.spec.meetings.horizon_days)
    slots = gateway.calendar.slots(kind.provider_ref, start=first, end=last, time_zone=zone,
                                   duration_minutes=kind.duration_minutes)
    return [s for s in slots if earliest <= _parse(s) <= latest]


# -- what the calendar reports ---------------------------------------------------------------


def _work(gateway, request: dict, event: BookingEvent, title: str, body: str) -> None:
    if gateway.tasks is None or not request["agent"]:
        return
    from nova.runtime.base import WorkItem

    gateway.tasks(WorkItem(
        key=f"meeting-change:{request['request_id']}:{event.provider_event_id[:12]}",
        assignee=request["agent"], priority=6, title=title[:200], body=body,
        tenant_id=gateway.store.tenant_id))


def apply_event(gateway, event: BookingEvent) -> str:
    store, at = gateway.store, _iso(gateway)
    request = store.meeting_by_ref(event.booking_ref) or store.meeting_by_ref(event.previous_ref)
    if request is None:
        return "unmatched"  # a meeting NOVA did not book
    rid, who = request["request_id"], f"contact {request['contact_id']}"
    if event.change is BookingChange.CANCELLED:
        if not store.settle_meeting_request(rid, "cancelled", at=at, only_from=("booked",),
                                            reason="cancelled at the calendar"):
            return "ignored"
        _work(gateway, request, event, f"Meeting cancelled by {who}",
              f"The {request['event_type']} meeting with {who} planned for {request['start']} "
              "was cancelled at the calendar. Follow up only if it is still needed.")
    elif event.change is BookingChange.RESCHEDULED:
        if not store.reschedule_meeting(rid, booking_ref=event.booking_ref,
                                        start=event.start or request["start"], at=at,
                                        reason="moved at the calendar"):
            return "ignored"
        _work(gateway, request, event, f"Meeting moved by {who}",
              f"The {request['event_type']} meeting with {who} moved from {request['start']} "
              f"to {event.start or 'a new time'}.")
    elif event.change is BookingChange.REJECTED:
        if not store.settle_meeting_request(rid, "failed", at=at, only_from=("booked",),
                                            reason="the host declined the meeting"):
            return "ignored"
        _work(gateway, request, event, f"Meeting declined for {who}",
              f"The host declined the {request['event_type']} meeting with {who}.")
    else:
        return "ignored"
    gateway.audit.record("communications.meeting.changed", correlation_id=new_correlation_id(),
                         subject=rid, detail={"change": event.change.value})
    return event.change.value


def ingest(gateway, provider_id: str, headers: Mapping[str, str], body: bytes) -> tuple[int, str]:
    """A calendar webhook: verified, parsed, applied once per delivery."""
    provider = gateway.calendar
    if provider is None or provider.id != provider_id:
        return 404, "no such provider"
    if not provider.verify(headers, body):
        return 403, "rejected"
    try:
        events = provider.parse(headers, body)
    except ValueError:
        return 400, "unreadable"
    applied = 0
    for event in events:
        if gateway.store.record_meeting_event(event.provider_event_id, event.booking_ref,
                                              event.change.value, _iso(gateway)):
            applied += apply_event(gateway, event) not in ("unmatched", "ignored")
    return 200, f"applied {applied}"
