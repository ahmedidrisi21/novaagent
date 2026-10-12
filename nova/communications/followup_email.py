"""Sending a follow-up email a call earned, and hearing back about it.

**Where it goes.** Only to the address on the contact's record, and only if it is still
the address that was shown when the email was proposed (and, for an approved one, when a
person approved it). An address spoken on a call is never used.

**What it says.** The tenant's template, filled when the plan was made, exactly as shown to
the approver, plus an unsubscribe footer. Plain text only, so nothing from a call can
become markup.

**Unsubscribing.** Every email carries ``List-Unsubscribe`` with one-click POST (RFC 8058)
and the same link in its footer. The link is signed per contact, so it cannot be forged
for someone else. Opening it shows a confirmation button rather than unsubscribing on
GET: mail scanners follow links, and a scanner should not unsubscribe anyone. It stops
email only. A spoken "stop calling me" stops everything.

**Hearing back.** Delivery webhooks are verified and deduplicated like carrier callbacks.
A hard bounce or a spam complaint stops email to that contact.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import urllib.parse
from datetime import timedelta
from typing import Mapping

from nova.audit import new_correlation_id
from nova.communications.mail import OutboundEmail

EMAIL_CHANNEL = "email"
_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
         "<title>{title}</title><body style='font-family:system-ui;max-width:32rem;"
         "margin:3rem auto;padding:0 1rem'>{body}</body>")


def _iso(gateway, delta: timedelta = timedelta(0)) -> str:
    from nova.communications.actions import _iso as iso

    return iso(gateway.now() + delta)


def unsubscribe_token(gateway, contact_id: str) -> str:
    message = f"{gateway.store.tenant_id}:{contact_id}:{EMAIL_CHANNEL}".encode()
    return hmac.new(gateway.link_secret, message, hashlib.sha256).hexdigest()[:32]


def unsubscribe_url(gateway, contact_id: str) -> str:
    query = urllib.parse.urlencode({"c": contact_id, "t": unsubscribe_token(gateway, contact_id)})
    return f"{gateway.public_url.rstrip('/')}/email/{gateway.email.id}/unsubscribe?{query}"


def reply_address(gateway, email_id: str) -> str:
    """Where a reply to this email goes: the tenant's reply domain, tagged with the email it
    answers and signed, so a reply can be matched to its email and cannot be forged onto
    another. Without a reply domain, the tenant's own ``reply_to`` mailbox."""
    domain = gateway.spec.email.reply_domain
    if not domain or len(gateway.link_secret) < 16:
        return gateway.spec.email.reply_to
    return f"reply+{email_id}.{_reply_signature(gateway, email_id)}@{domain}"


def _reply_signature(gateway, email_id: str) -> str:
    message = f"{gateway.store.tenant_id}:reply:{email_id}".encode()
    return hmac.new(gateway.link_secret, message, hashlib.sha256).hexdigest()[:16]


def replied_email_id(gateway, address: str) -> str:
    """The email a reply address answers, if its signature holds; else ''."""
    local, _, domain = address.strip().lower().rpartition("@")
    if not local.startswith("reply+") or domain != gateway.spec.email.reply_domain.lower():
        return ""
    email_id, _, signature = local[len("reply+"):].partition(".")
    if not email_id or not hmac.compare_digest(signature, _reply_signature(gateway, email_id)):
        return ""
    return email_id


def deliver(gateway, *, key: str, call_id: str, contact_id: str, to: str, subject: str,
            body: str) -> str:
    """Send one email to a contact, once per ``key``. Returns our email id.

    Every email leaves the same way: plain text, an unsubscribe footer and one-click
    ``List-Unsubscribe``, a signed reply address, and an idempotency key at the provider,
    so a re-run after a crash sends nothing twice. Raises ``ProviderError``."""
    store = gateway.store
    keep = gateway.spec.email.keep_body_days
    email, _ = store.create_email(
        action_id=key, call_id=call_id, contact_id=contact_id, to=to, subject=subject,
        body=body if keep else "", provider=gateway.email.id,
        body_until=_iso(gateway, timedelta(days=keep)) if keep else "", at=_iso(gateway))
    if email["provider_ref"]:
        return email["email_id"]  # sent before a crash; not again
    link = unsubscribe_url(gateway, contact_id)
    ref = gateway.email.send(OutboundEmail(
        email_id=email["email_id"],
        idempotency_key=f"nova-email-{store.tenant_id}-{key}"[:256],
        sender=gateway.spec.email.sender, to=to, subject=subject,
        text=f"{body.rstrip()}\n\n--\nTo stop these emails: {link}\n",
        reply_to=reply_address(gateway, email["email_id"]),
        headers={"List-Unsubscribe": f"<{link}>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    ))
    store.set_email_ref(email["email_id"], ref, at=_iso(gateway))
    return email["email_id"]


def can_send(gateway) -> bool:
    return gateway.email is not None and len(gateway.link_secret) >= 16 and bool(gateway.public_url)


def send(gateway, action: dict):
    from nova.communications.actions import Outcome

    store, params = gateway.store, action["params"]
    contact_id = action["contact_id"]
    if store.is_suppressed(contact_id) or store.channel_suppressed(contact_id, EMAIL_CHANNEL):
        return Outcome("cancelled", error="the contact opted out of email")
    contact = store.contact(contact_id)
    if contact is None or contact.email != params["to"]:
        return Outcome("failed", error="the address on file changed after this email was "
                                       "proposed; it was not sent")
    if not can_send(gateway):
        return Outcome("release")
    return Outcome("done", result=deliver(
        gateway, key=action["action_id"], call_id=action["call_id"], contact_id=contact_id,
        to=params["to"], subject=params["subject"], body=params["body"]))


def ingest(gateway, provider_id: str, headers: Mapping[str, str], body: bytes) -> tuple[int, str]:
    """A delivery webhook. ``(http status, outcome)``."""
    provider = gateway.email
    if provider is None or provider.id != provider_id:
        return 404, "no such provider"
    if not provider.verify(headers, body):
        return 403, "rejected"
    announced = provider.parse_inbound(headers, body)
    if announced is not None:
        from nova.communications.contracts import ProviderError
        from nova.communications.replies import handle

        try:
            return 200, handle(gateway, provider, announced)
        except ProviderError:
            return 502, "could not fetch the message; retry"  # the provider redelivers
    try:
        events = provider.parse(headers, body)
    except ValueError:
        return 400, "rejected"
    store, at = gateway.store, _iso(gateway)
    for event in events:
        email = store.email_by_ref(provider.id, event.provider_ref)
        if email is None and event.email_id_hint:
            email = store.email(event.email_id_hint)
        if email is None:
            continue
        if not store.record_email_event(provider=provider.id,
                                        provider_event_id=event.provider_event_id,
                                        email_id=email["email_id"], status=event.status, at=at):
            continue
        store.advance_email(email["email_id"], event.status, at=at, detail=event.detail)
        if event.status.stops_mail and store.suppress_channel(
                email["contact_id"], EMAIL_CHANNEL, reason=f"email {event.status.value}", at=at):
            gateway.audit.record("communications.contact.email_stopped",
                                 correlation_id=new_correlation_id(), subject=email["contact_id"],
                                 detail={"email_id": email["email_id"],
                                         "because": event.status.value})
    return 200, "applied"


def _page(title: str, body: str) -> bytes:
    return _PAGE.format(title=html.escape(title), body=body).encode("utf-8")


def unsubscribe(gateway, provider_id: str, url: str, *, method: str) -> tuple[int, str, bytes]:
    """The link in every email. GET asks; POST (the button, or a one-click client) acts."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    contact_id = (query.get("c") or [""])[0]
    token = (query.get("t") or [""])[0]
    if gateway.email is None or gateway.email.id != provider_id or not contact_id or \
            len(gateway.link_secret) < 16 or \
            not hmac.compare_digest(token, unsubscribe_token(gateway, contact_id)):
        return 404, "text/html", _page("Link not recognised",
                                       "<p>This unsubscribe link is not recognised.</p>")
    if method == "GET":
        form = (f"<form method=post action='?{html.escape(urllib.parse.urlsplit(url).query)}'>"
                "<p>Stop follow-up emails to this address?</p>"
                "<button type=submit>Unsubscribe</button></form>")
        return 200, "text/html", _page("Unsubscribe", form)
    at = _iso(gateway)
    if gateway.store.suppress_channel(contact_id, EMAIL_CHANNEL, reason="unsubscribed", at=at):
        cancelled = gateway.store.cancel_outreach(contact_id, reason="unsubscribed from email",
                                                  at=at, kinds=("follow_up_email",))
        gateway.audit.record("communications.contact.email_stopped",
                             correlation_id=new_correlation_id(), subject=contact_id,
                             detail={"because": "unsubscribed", "cancelled": cancelled})
    return 200, "text/html", _page("Unsubscribed", "<p>You will not receive these emails again.</p>")


def expire_bodies(gateway) -> int:
    return gateway.store.expire_email_bodies(_iso(gateway))

