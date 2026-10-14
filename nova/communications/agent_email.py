"""Emails an agent writes itself: requested through its signed tool, decided here.

An agent names a contact, a purpose, a subject and a body; it never supplies an address.
The gate below — the email twin of the call gate — decides, in order: the agent's email
grant, the purpose, the contact and its group, an address on file, opt-outs (every channel,
then email), marketing consent for a marketing purpose, the agent's daily cap, the
tenant's budget, and the grant's approval mode.

**Approval is bound to the email.** A person approves the exact address, subject and text
(a digest of the three); if the contact's address changed since, the approval is refused.
Approving re-runs the gate, so consent withdrawn in the meantime still stops it.

**Once.** The request id is the idempotency key: picked up twice it is one request; claimed
for sending by one conditional update; sent with the provider's idempotency key. A request
left mid-send by a crash is sent again by the sweep, which the provider makes a no-op.

The agent's text goes out as plain text, with the same unsubscribe footer and signed reply
address as every other email (:func:`nova.communications.followup_email.deliver`).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Callable

from nova.audit import new_correlation_id
from nova.communications.contracts import ProviderError
from nova.communications.followup_email import EMAIL_CHANNEL, can_send, deliver
from nova.communications.spec import CONSENT_EMAIL_MARKETING

MAX_SUBJECT = 200
MAX_BODY = 10_000
#: A request left ``sending`` this long belongs to a process that died.
STUCK_AFTER = timedelta(minutes=10)

DENY, APPROVE, ALLOW = "deny", "approval_required", "allow"


def _iso(gateway, delta: timedelta = timedelta(0)) -> str:
    from nova.communications.actions import _iso as iso

    return iso(gateway.now() + delta)


def digest(to: str, subject: str, body: str) -> str:
    return "sha256:" + hashlib.sha256(f"{to}\n{subject}\n{body}".encode()).hexdigest()


def decide(gateway, agent: str, contact_id: str, purpose_id: str) -> tuple[str, str]:
    """``(effect, reason)``. Fail-closed: the first check that fails decides."""
    from nova.communications.usage import exhausted

    spec, store = gateway.spec, gateway.store
    grant = spec.email.grant(agent)
    contact = store.contact(contact_id)
    purpose = spec.purposes.get(purpose_id)
    since = _iso(gateway, -timedelta(days=1))
    checks: list[tuple[Callable[[], bool], str]] = [
        (lambda: grant is None, f"agent {agent!r} has no email grant"),
        (lambda: purpose_id not in grant.purposes or purpose is None,
         f"agent {agent!r} may not email for purpose {purpose_id!r}"),
        (lambda: contact is None, "the contact does not exist in this tenant"),
        (lambda: not set(contact.groups) & set(grant.contact_groups),
         "the contact is in none of the groups this agent may email"),
        (lambda: not contact.email, "no email address on file for this contact"),
        (lambda: store.is_suppressed(contact_id) or store.channel_suppressed(contact_id,
                                                                             EMAIL_CHANNEL),
         "the contact opted out of email"),
        (lambda: purpose.marketing and CONSENT_EMAIL_MARKETING not in store.consents(contact_id),
         "a marketing purpose needs email_marketing consent on record"),
        (lambda: store.count_agent_emails_since(agent, since) >= grant.max_per_day,
         "the agent has sent its daily limit of emails"),
        (lambda: bool(exhausted(gateway)), "the monthly communications budget is reached"),
    ]
    try:
        reason = next((message for failed, message in checks if failed()), "")
    except Exception as exc:
        return DENY, f"the email gate failed and refused by default: {exc}"
    if reason:
        return DENY, reason
    if grant.approval == "always" or (grant.approval == "first_contact" and
                                      not store.emailed_before(agent, contact_id)):
        return APPROVE, "this agent's emails need a person's approval"
    return ALLOW, "every check passed"


def view(request: dict) -> dict[str, Any]:
    """What the agent's tool reads back. Never the address."""
    return {"request_id": request["request_id"], "status": request["status"],
            "reason": request["reason"], "email_id": request["email_id"]}


def request_email(gateway, *, agent: str, request_id: str, contact_id: str, purpose: str,
                  subject: str, body: str, task_id: str = "") -> dict:
    subject = " ".join(str(subject or "").split())[:MAX_SUBJECT]
    body = str(body or "").strip()[:MAX_BODY]
    if not (subject and body):
        raise ValueError("an email needs a subject and a body")
    effect, reason = decide(gateway, agent, contact_id, purpose)
    contact = gateway.store.contact(contact_id)
    to = contact.email if contact else ""
    status = {DENY: "blocked", APPROVE: "awaiting_approval"}.get(effect, "new")
    request, created = gateway.store.create_email_request(
        request_id=request_id, agent=agent, contact_id=contact_id, purpose=purpose,
        task_id=task_id[:64], to_addr=to, subject=subject, body=body,
        digest=digest(to, subject, body), status=status, reason=reason, created_at=_iso(gateway))
    if created:
        gateway.audit.record("communications.email.requested", correlation_id=new_correlation_id(),
                             subject=request_id,
                             detail={"agent": agent, "contact_id": contact_id, "purpose": purpose,
                                     "status": status, "reason": reason})
        if status == "new":
            request = _send(gateway, request, actor="")
    return view(request)


def _send(gateway, request: dict, *, actor: str) -> dict:
    store, at = gateway.store, _iso(gateway)
    if request["status"] != "sending" and not store.claim_email_request(
            request["request_id"], at=at, actor=actor):
        return store.email_request(request["request_id"])  # someone else took it
    if not can_send(gateway):
        store.settle_email_request(request["request_id"], "failed", at=at, only_from=("sending",),
                                   reason="no email sender is configured on the gateway")
        return store.email_request(request["request_id"])
    try:
        email_id = deliver(gateway, key=f"agent-email:{request['request_id']}", call_id="",
                           contact_id=request["contact_id"], to=request["to_addr"],
                           subject=request["subject"], body=request["body"])
    except ProviderError as exc:
        store.settle_email_request(request["request_id"], "failed", at=_iso(gateway),
                                   reason=str(exc), only_from=("sending",))
    else:
        store.settle_email_request(request["request_id"], "sent", at=_iso(gateway),
                                   email_id=email_id, only_from=("sending",))
    record = store.email_request(request["request_id"])
    gateway.audit.record("communications.email.sent" if record["status"] == "sent" else
                         "communications.email.failed", correlation_id=new_correlation_id(),
                         subject=request["request_id"],
                         detail={"agent": request["agent"], "approved_by": actor},
                         error=record["reason"] if record["status"] == "failed" else "")
    return record


def approve(gateway, request_id: str, *, actor: str) -> dict:
    """A person approved what they were shown. ``ValueError`` when it may not go."""
    if not actor:
        raise ValueError("an approval with no identity is not an approval")
    store = gateway.store
    request = store.email_request(request_id)
    if request is None:
        raise KeyError(f"no email request {request_id!r} in this tenant")
    if request["status"] != "awaiting_approval":
        raise ValueError(f"the email is {request['status']}, not waiting for approval")
    contact = store.contact(request["contact_id"])
    current = digest(contact.email if contact else "", request["subject"], request["body"])
    effect, reason = decide(gateway, request["agent"], request["contact_id"], request["purpose"])
    refusal = ("the contact's address changed since this email was written; it was not sent"
               if current != request["digest"] else reason if effect == DENY else "")
    if refusal:
        store.settle_email_request(request_id, "blocked", at=_iso(gateway), reason=refusal,
                                   decided_by=actor)
        gateway.audit.record("communications.email.approval_refused",
                             correlation_id=new_correlation_id(), subject=request_id,
                             detail={"actor": actor, "reason": refusal})
        raise ValueError(refusal)
    return view(_send(gateway, request, actor=actor))


def reject(gateway, request_id: str, *, actor: str, reason: str = "") -> dict:
    if not actor:
        raise ValueError("a rejection with no identity is not a rejection")
    if gateway.store.email_request(request_id) is None:
        raise KeyError(f"no email request {request_id!r} in this tenant")
    if not gateway.store.settle_email_request(request_id, "rejected", at=_iso(gateway),
                                              reason=reason or "rejected", decided_by=actor):
        raise ValueError("the email is not waiting for approval")
    gateway.audit.record("communications.email.rejected", correlation_id=new_correlation_id(),
                         subject=request_id, detail={"actor": actor, "reason": reason})
    return view(gateway.store.email_request(request_id))


def resume_sending(gateway) -> int:
    """Send again what a dead process left mid-send; the provider makes a repeat a no-op."""
    stuck = [r for r in gateway.store.email_requests(status="sending")
             if r["updated_at"] < _iso(gateway, -STUCK_AFTER)]
    for request in stuck:
        _send(gateway, request, actor=request["decided_by"])
    return len(stuck)


def pending(gateway) -> list[dict]:
    """Emails waiting for a person, with what they would send."""
    return [{k: r[k] for k in ("request_id", "agent", "contact_id", "purpose", "to_addr",
                               "subject", "body", "reason", "created_at")}
            for r in gateway.store.email_requests(status="awaiting_approval")]

