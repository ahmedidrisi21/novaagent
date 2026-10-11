"""Test doubles for a carrier and a voice engine. They place no call and play no audio.

They are real implementations of the contracts, not stubs: callbacks and webhooks are
HMAC-signed and verified, failures raise the same normalized errors a real adapter must
raise, and the contract tests run every adapter — these and the vendor ones — through the
same checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Optional

from nova.communications.contracts import (
    AnswerPlan,
    CallStatus,
    FailureReason,
    IncomingSession,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    SipTarget,
    SipVoiceEngine,
    StreamTarget,
    TelephonyProvider,
    VoiceSessionRequest,
)

SIGNATURE_HEADER = "x-mock-signature"


def _sign(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _signed(secret: bytes, headers: Mapping[str, str], body: bytes) -> bool:
    given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
    return hmac.compare_digest(given, _sign(secret, body))


class MockTelephony(TelephonyProvider):
    id = "mock-telephony"

    def __init__(self, *, secret: bytes = b"mock-secret") -> None:
        self._secret = secret
        self.placed: list[PlaceCall] = []
        self.ended: list[str] = []
        #: Set to make the next ``place_call`` fail with this reason.
        self.fail_next: Optional[FailureReason] = None
        #: What ``fetch_status`` reports per call reference; absent means "no such call".
        self.carrier_status: dict[str, CallStatus] = {}
        #: Set to make ``fetch_status`` fail as an unreachable carrier would.
        self.unreachable = False
        #: Every reference ``fetch_status`` was asked about, in order.
        self.fetched: list[str] = []

    def place_call(self, request: PlaceCall) -> str:
        if self.fail_next is not None:
            reason, self.fail_next = self.fail_next, None
            raise ProviderError(f"mock carrier refused: {reason.value}", reason=reason)
        self.placed.append(request)
        return f"mock-call-{len(self.placed)}"

    def end_call(self, provider_call_ref: str) -> None:
        if provider_call_ref not in self.ended:
            self.ended.append(provider_call_ref)

    def verify(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        return _signed(self._secret, headers, body)

    def fetch_status(self, provider_call_ref: str) -> Optional[ProviderEvent]:
        self.fetched.append(provider_call_ref)
        if self.unreachable:
            raise ProviderError("mock carrier unreachable")
        status = self.carrier_status.get(provider_call_ref)
        if status is None:
            return None
        return ProviderEvent(self.id, f"{provider_call_ref}:fetched:{status.value}",
                             provider_call_ref, status)

    def parse(self, headers: Mapping[str, str], body: bytes, url: str) -> ProviderEvent:
        try:
            payload = json.loads(body.decode("utf-8"))
            status = CallStatus(payload["status"])
            event_id = str(payload["event_id"])
            ref = str(payload["call_ref"])
        except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"not a mock carrier callback: {exc}") from exc
        failure = payload.get("failure")
        return ProviderEvent(
            provider=self.id,
            provider_event_id=event_id,
            provider_call_ref=ref,
            status=status,
            failure=FailureReason(failure) if failure else None,
            call_id_hint=str(payload.get("call_id", "")),
        )

    def render_answer(self, plan: AnswerPlan) -> tuple[str, bytes]:
        out: dict[str, Any] = {"hang_up": plan.hang_up or plan.connect is None}
        if not out["hang_up"]:
            out["say"] = plan.say
            if isinstance(plan.connect, StreamTarget):
                out.update(stream=plan.connect.url, parameters=dict(plan.connect.parameters))
            else:
                out.update(uri=plan.connect.uri, headers=dict(plan.connect.headers))
        return "application/json", json.dumps(out, sort_keys=True).encode("utf-8")

    def callback(
        self, call_ref: str, status: CallStatus, *, event_id: str,
        failure: Optional[FailureReason] = None, call_id: str = "",
    ) -> tuple[dict[str, str], bytes]:
        """A signed callback as the carrier would send it: ``(headers, body)``."""
        payload: dict[str, Any] = {"event_id": event_id, "call_ref": call_ref, "status": status.value}
        if failure is not None:
            payload["failure"] = failure.value
        if call_id:
            payload["call_id"] = call_id
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body


class MockVoice(SipVoiceEngine):
    id = "mock-voice"

    def __init__(self, *, secret: bytes = b"mock-voice-secret") -> None:
        self._secret = secret
        self.accepted: dict[str, VoiceSessionRequest] = {}
        self.rejected: list[str] = []
        self.hung_up: list[str] = []
        self.attached: dict[str, object] = {}
        self.fail_next = False

    def bridge_target(self, call_id: str, token: str) -> SipTarget:
        return SipTarget("sip:agent@voice.test", {"X-Call-Id": call_id, "X-Call-Token": token})

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return _signed(self._secret, headers, body)

    def parse_incoming(self, headers: Mapping[str, str], body: bytes) -> Optional[IncomingSession]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"unreadable: {exc}") from exc
        if payload.get("type") != "incoming":
            return None
        return IncomingSession(
            session_id=str(payload["session_id"]),
            event_id=str(payload["event_id"]),
            headers={k.lower(): v for k, v in payload.get("headers", {}).items()},
        )

    def accept(self, session_id: str, request: VoiceSessionRequest) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ProviderError("mock voice engine refused")
        self.accepted[session_id] = request

    def reject(self, session_id: str) -> None:
        self.rejected.append(session_id)

    def attach(self, session_id: str, answer) -> None:
        self.attached[session_id] = answer

    def hang_up(self, session_id: str) -> None:
        if session_id not in self.hung_up:
            self.hung_up.append(session_id)

    def incoming(
        self, *, session_id: str, event_id: str, sip_headers: Mapping[str, str]
    ) -> tuple[dict[str, str], bytes]:
        """A signed incoming-session webhook: ``(headers, body)``."""
        body = json.dumps({"type": "incoming", "session_id": session_id, "event_id": event_id,
                           "headers": dict(sip_headers)}).encode("utf-8")
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body
