"""Test doubles for a carrier, a voice engine and an email provider. Nothing leaves the process.

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

from nova.communications.booking import (
    Booked,
    BookingChange,
    BookingEvent,
    BookingRequest,
    BookingUnconfirmed,
    CalendarProvider,
    SlotUnavailable,
)
from nova.communications.contracts import (
    AnswerPlan,
    Billing,
    CallStatus,
    ConsentReply,
    FailureReason,
    IncomingSession,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    RecordingEvent,
    SipTarget,
    SipVoiceEngine,
    StreamTarget,
    TelephonyProvider,
    VoiceSessionRequest,
)
from nova.communications.mail import (
    EmailEvent,
    EmailProvider,
    EmailStatus,
    InboundEmail,
    OutboundEmail,
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
        #: recording ref -> "recording" | "paused" | "deleted"
        self.recordings: dict[str, str] = {}
        self.fail_recording = False
        #: (call ref, plan) for every live call moved to another engine.
        self.redirected: list = []
        self.fail_redirect = False
        #: What ``fetch_billing`` reports per call reference; absent means "not settled yet".
        self.billing: dict[str, Billing] = {}

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

    def redirect(self, provider_call_ref: str, plan: AnswerPlan) -> None:
        if self.fail_redirect:
            raise ProviderError("mock carrier could not redirect")
        self.redirected.append((provider_call_ref, plan))

    def fetch_billing(self, provider_call_ref: str) -> Optional[Billing]:
        if self.unreachable:
            raise ProviderError("mock carrier unreachable")
        return self.billing.get(provider_call_ref)

    def parse_consent(self, headers: Mapping[str, str], body: bytes, url: str) -> ConsentReply:
        try:
            payload = json.loads(body.decode("utf-8"))
            return ConsentReply(str(payload["call_ref"]), str(payload.get("call_id", "")),
                                digits=str(payload.get("digits", "")),
                                speech=str(payload.get("speech", "")))
        except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"not a mock consent callback: {exc}") from exc

    def start_recording(self, call_id: str, provider_call_ref: str) -> str:
        if self.fail_recording:
            raise ProviderError("mock carrier could not record")
        ref = f"rec-{len(self.recordings) + 1}"
        self.recordings[ref] = "recording"
        return ref

    def set_recording_paused(self, provider_call_ref: str, recording_ref: str, paused: bool) -> None:
        self.recordings[recording_ref] = "paused" if paused else "recording"

    def delete_recording(self, recording_ref: str) -> None:
        self.recordings[recording_ref] = "deleted"

    def parse_recording(self, headers: Mapping[str, str], body: bytes, url: str) -> RecordingEvent:
        try:
            payload = json.loads(body.decode("utf-8"))
            return RecordingEvent(str(payload["recording_ref"]), str(payload["call_ref"]),
                                  str(payload.get("call_id", "")), str(payload["status"]),
                                  int(payload.get("duration", 0)))
        except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"not a mock recording callback: {exc}") from exc

    def signed(self, payload: Mapping[str, Any]) -> tuple[dict[str, str], bytes]:
        """Any payload, signed as this carrier signs callbacks."""
        body = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body

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
            detail={"duration_seconds": int(payload["duration"])} if payload.get("duration") else {},
        )

    def render_answer(self, plan: AnswerPlan) -> tuple[str, bytes]:
        if plan.consent is not None and not plan.hang_up:
            return "application/json", json.dumps(
                {"hang_up": False, "say": plan.say, "ask_consent": plan.consent.prompt},
                sort_keys=True).encode("utf-8")
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
        failure: Optional[FailureReason] = None, call_id: str = "", duration: int = 0,
    ) -> tuple[dict[str, str], bytes]:
        """A signed callback as the carrier would send it: ``(headers, body)``."""
        payload: dict[str, Any] = {"event_id": event_id, "call_ref": call_ref, "status": status.value}
        if duration:
            payload["duration"] = duration
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

    def attach(self, session_id: str, hooks) -> None:
        self.attached[session_id] = hooks

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


class MockEmail(EmailProvider):
    """An email provider that sends nothing. Idempotent on the key, as a real one must be."""

    id = "mock-email"

    def __init__(self, *, secret: bytes = b"mock-secret") -> None:
        self._secret = secret
        #: idempotency key -> (provider ref, message). One entry per distinct send.
        self.sent: dict[str, tuple[str, OutboundEmail]] = {}
        self.fail_next = False
        self.received: dict[str, InboundEmail] = {}

    def send(self, message: OutboundEmail) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ProviderError("mock email provider refused")
        if message.idempotency_key not in self.sent:
            self.sent[message.idempotency_key] = (f"mock-email-{len(self.sent) + 1}", message)
        return self.sent[message.idempotency_key][0]

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return _signed(self._secret, headers, body)

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[EmailEvent]:
        data = json.loads(body.decode("utf-8"))
        if data.get("type") == "inbound":
            return []
        return [EmailEvent(provider_event_id=str(data["event_id"]), provider_ref=str(data["ref"]),
                           status=EmailStatus(data["status"]))]

    def parse_inbound(self, headers: Mapping[str, str], body: bytes) -> Optional[InboundEmail]:
        data = json.loads(body.decode("utf-8"))
        if data.get("type") != "inbound":
            return None
        return InboundEmail(str(data["ref"]), str(data["from"]), tuple(data["to"]),
                            str(data.get("subject", "")))

    def fetch_inbound(self, provider_ref: str) -> InboundEmail:
        return self.received[provider_ref]

    def inbound(self, ref: str, *, sender: str, to: str, subject: str,
                text: str) -> tuple[dict, bytes]:
        """A signed received-mail webhook, with the message held for :meth:`fetch_inbound`."""
        self.received[ref] = InboundEmail(ref, sender, (to,), subject, text)
        body = json.dumps({"type": "inbound", "ref": ref, "from": sender, "to": [to],
                           "subject": subject}).encode()
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body

    def event(self, ref: str, status: EmailStatus, *, event_id: str) -> tuple[dict, bytes]:
        """A signed delivery webhook, as the provider would send it."""
        body = json.dumps({"event_id": event_id, "ref": ref, "status": status.value}).encode()
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body


class MockCalendar(CalendarProvider):
    """A calendar that holds a list of open times and books against it, one booking per
    request id, so a contract test can crash it between booking and answering."""

    id = "mock-calendar"

    def __init__(self, secret: str = "mock-calendar-secret", *, open_times: Optional[list] = None):
        self._secret = secret.encode("utf-8")
        self.open_times = list(open_times if open_times is not None else [])
        #: request id -> (booking ref, BookingRequest)
        self.bookings: dict[str, tuple[str, BookingRequest]] = {}
        self.cancelled: list[str] = []
        self.book_calls = 0
        #: "unconfirmed": book, then fail to say so. "refuse": a definite refusal.
        self.fail_next = ""
        self.find_fails = False

    def slots(self, event_type_ref: str, *, start: str, end: str, time_zone: str,
              duration_minutes: int) -> list[str]:
        taken = {b.start for _, b in self.bookings.values()}
        return [s for s in self.open_times if start <= s <= end and s not in taken]

    def find(self, request: BookingRequest) -> Optional[Booked]:
        if self.find_fails:
            raise ProviderError("mock calendar could not search")
        found = self.bookings.get(request.request_id)
        return Booked(found[0]) if found and found[0] not in self.cancelled else None

    def book(self, request: BookingRequest) -> Booked:
        self.book_calls += 1
        if self.fail_next == "refuse":
            self.fail_next = ""
            raise ProviderError("mock calendar refused")
        if request.start not in self.open_times or any(
                b.start == request.start for _, b in self.bookings.values()):
            raise SlotUnavailable("mock calendar: that time is taken")
        ref = f"mock-booking-{len(self.bookings) + 1}"
        self.bookings[request.request_id] = (ref, request)
        if self.fail_next == "unconfirmed":
            self.fail_next = ""
            raise BookingUnconfirmed("mock calendar did not answer")
        return Booked(ref)

    def cancel(self, ref: str, reason: str) -> None:
        self.cancelled.append(ref)

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return _signed(self._secret, headers, body)

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[BookingEvent]:
        data = json.loads(body.decode("utf-8"))
        return [BookingEvent(
            provider_event_id=str(data["event_id"]), booking_ref=str(data["ref"]),
            change=BookingChange(data["change"]), start=str(data.get("start", "")),
            previous_ref=str(data.get("previous_ref", "")))]

    def event(self, ref: str, change: BookingChange, *, event_id: str, start: str = "",
              previous_ref: str = "") -> tuple[dict, bytes]:
        """A signed calendar webhook, as the provider would send it."""
        body = json.dumps({"event_id": event_id, "ref": ref, "change": change.value,
                           "start": start, "previous_ref": previous_ref}).encode()
        return {SIGNATURE_HEADER: _sign(self._secret, body)}, body
