"""Standard Webhooks signatures, as GPT-Live and Resend (through Svix) both send them.

``v1`` is HMAC-SHA256 over ``<id>.<timestamp>.<raw body>``, keyed with the base64 part of
a ``whsec_`` secret, sent base64-encoded. The header may list several space-separated
signatures while a secret is being rotated. The only difference between vendors is the
header prefix: ``webhook-`` (the specification) or ``svix-``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Mapping

#: Seconds a signed timestamp may differ from our clock before the request is refused.
TOLERANCE = 300


def sign(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def verify(secret: str, headers: Mapping[str, str], body: bytes, *, now: float,
           prefix: str = "webhook") -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    message_id = lowered.get(f"{prefix}-id", "")
    timestamp = lowered.get(f"{prefix}-timestamp", "")
    given = lowered.get(f"{prefix}-signature", "")
    if not (secret and message_id and timestamp.isdigit() and given):
        return False
    if abs(now - int(timestamp)) > TOLERANCE:
        return False
    expected = sign(secret, message_id, timestamp, body)
    return any(part.startswith("v1,") and hmac.compare_digest(part[3:], expected)
               for part in given.split())
