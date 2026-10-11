"""Test doubles for a carrier and a voice engine. They place no call and play no audio.

They are real implementations of the contracts, not stubs: callbacks are HMAC-signed and
verified, failures raise the same normalized errors a carrier adapter must raise, and the
contract tests run every adapter — these first — through the same checks. A future Twilio
adapter passes those tests or it does not ship.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Optional

from nova.communications.contracts import (
    CallStatus,
    FailureReason,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    TelephonyProvider,
    VoiceSessionProvider,
    VoiceSessionRequest,
)

SIGNATURE_HEADER = "x-mock-signature"


class MockTelephony(TelephonyProvider):
    id = "mock-telephony"

    def __init__(self, *, secret: bytes = b"mock-secret") -> None:
        self._secret = secret
        self.placed: list[PlaceCall] = []
        self.ended: list[str] = []
        #: Set to make the next ``place_call`` fail with this reason.
        self.fail_next: Optional[FailureReason] = None

    def place_call(self, request: PlaceCall) -> str:
        if self.fail_next is not None:
            reason, self.fail_next = self.fail_next, None
            raise ProviderError(f"mock carrier refused: {reason.value}", reason=reason)
        self.placed.append(request)
        return f"mock-call-{len(self.placed)}"

    def end_call(self, provider_call_ref: str) -> None:
        if provider_call_ref not in self.ended:
            self.ended.append(provider_call_ref)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def verify(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        return hmac.compare_digest(given, self._sign(body))

    def normalize(self, payload: Mapping[str, Any]) -> ProviderEvent:
        try:
            status = CallStatus(payload["status"])
            event_id = str(payload["event_id"])
            ref = str(payload["call_ref"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"not a mock carrier callback: {exc}") from exc
        failure = payload.get("failure")
        return ProviderEvent(
            provider=self.id,
            provider_event_id=event_id,
            provider_call_ref=ref,
            status=status,
            occurred_at=str(payload.get("at", "")),
            failure=FailureReason(failure) if failure else None,
        )

    def callback(
        self, call_ref: str, status: CallStatus, *, event_id: str,
        failure: Optional[FailureReason] = None,
    ) -> tuple[dict[str, str], bytes]:
        """A signed callback as the carrier would send it: ``(headers, body)``."""
        payload: dict[str, Any] = {"event_id": event_id, "call_ref": call_ref, "status": status.value}
        if failure is not None:
            payload["failure"] = failure.value
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return {SIGNATURE_HEADER: self._sign(body)}, body


class MockVoice(VoiceSessionProvider):
    id = "mock-voice"

    def __init__(self) -> None:
        self.open: dict[str, VoiceSessionRequest] = {}
        self.closed: list[str] = []

    def open_session(self, request: VoiceSessionRequest) -> str:
        session_id = f"mock-session-{request.call_id}"
        self.open[session_id] = request
        return session_id

    def close_session(self, session_id: str) -> None:
        if self.open.pop(session_id, None) is not None:
            self.closed.append(session_id)
