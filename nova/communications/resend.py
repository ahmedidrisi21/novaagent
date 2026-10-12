"""Resend as the email provider.

Sending is one ``POST /emails`` with an ``Idempotency-Key``: Resend returns the original
message for a repeated key with the same body for 24 hours, so a send retried after a crash
sends once. Delivery webhooks are signed through Svix, which is Standard Webhooks with
``svix-`` headers, and :mod:`nova.communications.signing` verifies them.

Vendor contract: https://resend.com/docs/api-reference/emails/send-email,
https://resend.com/docs/dashboard/emails/idempotency-keys and
https://resend.com/docs/dashboard/webhooks/event-types.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Mapping

from nova.communications import signing
from nova.communications.contracts import ProviderError
from nova.communications.http import Transport, urllib_transport
from nova.communications.mail import EmailEvent, EmailProvider, EmailStatus, OutboundEmail

DEFAULT_BASE_URL = "https://api.resend.com"
#: The tag carrying our email id, so an event names its message even before we stored
#: Resend's id for it.
EMAIL_TAG = "nova_email_id"

EVENTS: Mapping[str, EmailStatus] = {
    "email.sent": EmailStatus.SENT,
    "email.delivered": EmailStatus.DELIVERED,
    "email.delivery_delayed": EmailStatus.DELAYED,
    "email.bounced": EmailStatus.BOUNCED,
    "email.complained": EmailStatus.COMPLAINED,
    "email.failed": EmailStatus.FAILED,
    "email.suppressed": EmailStatus.FAILED,
}


class ResendEmail(EmailProvider):
    id = "resend"

    def __init__(self, *, api_key: str, webhook_secret: str, base_url: str = DEFAULT_BASE_URL,
                 transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time) -> None:
        if not (api_key and webhook_secret):
            raise ValueError("Resend needs an API key and a webhook signing secret")
        self._key = api_key
        self._secret = webhook_secret
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._clock = clock

    @classmethod
    def from_env(cls, env: Mapping[str, str], **options) -> "ResendEmail":
        return cls(api_key=env.get("RESEND_API_KEY", ""),
                   webhook_secret=env.get("RESEND_WEBHOOK_SECRET", ""), **options)

    def send(self, message: OutboundEmail) -> str:
        payload = {
            "from": message.sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "tags": [{"name": EMAIL_TAG, "value": message.email_id}],
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if message.headers:
            payload["headers"] = dict(message.headers)
        try:
            status, body = self._transport(
                "POST", f"{self._base}/emails",
                {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                 "Idempotency-Key": message.idempotency_key[:256]},
                json.dumps(payload).encode("utf-8"),
            )
        except OSError as exc:
            raise ProviderError(f"Resend could not be reached: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        if 200 <= status < 300 and data.get("id"):
            return str(data["id"])
        name = str(data.get("name") or "")
        raise ProviderError(f"Resend refused the email ({status} {name}): {data.get('message', '')}")

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return signing.verify(self._secret, headers, body, now=self._clock(), prefix="svix")

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[EmailEvent]:
        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"unreadable webhook: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError("the webhook is not an object")
        status = EVENTS.get(str(event.get("type") or ""))
        if status is None:
            return []
        data = event.get("data") or {}
        bounce = data.get("bounce") or {}
        if status is EmailStatus.BOUNCED and str(bounce.get("type") or "").lower() != "permanent":
            # A soft bounce is a delay: the address still works.
            status = EmailStatus.DELAYED
        tags = data.get("tags") if isinstance(data.get("tags"), dict) else {}
        event_id = {k.lower(): v for k, v in headers.items()}.get("svix-id", "")
        return [EmailEvent(
            provider_event_id=event_id or f"{data.get('email_id')}:{event.get('type')}",
            provider_ref=str(data.get("email_id") or ""),
            status=status,
            email_id_hint=str(tags.get(EMAIL_TAG) or ""),
            detail=str(bounce.get("message") or "")[:300],
        )]
