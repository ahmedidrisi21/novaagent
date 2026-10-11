"""Twilio Programmable Voice as a carrier.

Dials through the REST API with answering-machine detection and a hard time limit, verifies
every callback with Twilio's request signature, and answers the call with TwiML: the
tenant's disclosure, then a SIP bridge to the voice engine. A machine-answered call is hung
up before anything is said.

The credentials are the gateway's, read from the gateway's own environment — never an
agent's. ``docs/platform/COMMUNICATIONS.md`` explains why that distinction is the control.

Vendor contract: https://www.twilio.com/docs/voice/api/call-resource and
https://www.twilio.com/docs/usage/webhooks/webhooks-security.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from typing import Mapping, Optional
from xml.sax.saxutils import escape, quoteattr

from nova.communications.contracts import (
    AnswerPlan,
    CallStatus,
    ConsentReply,
    FailureReason,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    RecordingEvent,
    StreamTarget,
    TelephonyProvider,
)
from nova.communications.http import Transport, urllib_transport

API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
SIGNATURE_HEADER = "x-twilio-signature"

_STATUS: Mapping[str, tuple[CallStatus, Optional[FailureReason]]] = {
    "queued": (CallStatus.INITIATED, None),
    "initiated": (CallStatus.INITIATED, None),
    "ringing": (CallStatus.RINGING, None),
    "in-progress": (CallStatus.ANSWERED, None),
    "completed": (CallStatus.COMPLETED, None),
    "busy": (CallStatus.FAILED, FailureReason.BUSY),
    "no-answer": (CallStatus.FAILED, FailureReason.NO_ANSWER),
    "failed": (CallStatus.FAILED, FailureReason.CARRIER_REJECTED),
    "canceled": (CallStatus.CANCELLED, None),
}

#: ``AnsweredBy`` values that mean nobody is listening. ``unknown`` is not among them: a
#: person who answers and stays silent is common, and is still a person.
_MACHINE = frozenset(
    {"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}
)

#: Twilio error codes, mapped to the reason retry rules key on. Anything else is the
#: provider's failure, which is retried by a human, not by a loop.
_ERRORS: Mapping[int, FailureReason] = {
    21211: FailureReason.INVALID_NUMBER,   # invalid 'To' number
    21214: FailureReason.INVALID_NUMBER,   # 'To' cannot be reached
    21217: FailureReason.INVALID_NUMBER,   # number does not appear valid
    21215: FailureReason.CARRIER_REJECTED, # geographic permission not enabled
    21216: FailureReason.CARRIER_REJECTED, # number blocked
}


def signature(auth_token: str, url: str, params: Mapping[str, list[str]]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL then each sorted key and value."""
    payload = url
    for name in sorted(params):
        for value in sorted(set(params[name])):
            payload += name + value
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _port_variants(url: str) -> tuple[str, ...]:
    """The URL with and without an explicit port. Twilio's own validator tries both — it
    strips any port and adds the scheme's default — so a proxy that adds or drops one
    does not break verification."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        other = parts._replace(netloc=host)
    else:
        default = {"https": 443, "http": 80}.get(parts.scheme)
        if default is None:
            return (url,)
        other = parts._replace(netloc=f"{host}:{default}")
    return (url, urllib.parse.urlunsplit(other))


def _with_call(url: str, call_id: str) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{urllib.parse.urlencode({'call': call_id})}"


class TwilioTelephony(TelephonyProvider):
    id = "twilio"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        answer_url: str,
        status_url: str,
        transport: Transport = urllib_transport,
        ring_seconds: int = 30,
        consent_url: str = "",
        recording_url: str = "",
    ) -> None:
        if not (account_sid and auth_token):
            raise ValueError("Twilio needs an account SID and an auth token")
        for url in (answer_url, status_url):
            if not url.startswith("https://"):
                raise ValueError(f"Twilio callback URLs must be public HTTPS, got {url!r}")
        self._sid = account_sid
        self._token = auth_token
        self._answer_url = answer_url
        self._status_url = status_url
        self._transport = transport
        self._ring_seconds = ring_seconds
        base = answer_url.rsplit("/", 1)[0]
        self._consent_url = consent_url or f"{base}/consent"
        self._recording_url = recording_url or f"{base}/recording"

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, answer_url: str, status_url: str) -> "TwilioTelephony":
        return cls(
            env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", ""),
            answer_url=answer_url, status_url=status_url,
        )

    def _post(self, path: str, form: list[tuple[str, str]], method: str = "POST") -> tuple[int, dict]:
        auth = base64.b64encode(f"{self._sid}:{self._token}".encode()).decode("ascii")
        try:
            status, body = self._transport(
                method,
                f"{API_BASE}/{self._sid}/{path}",
                {"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
                urllib.parse.urlencode(form).encode("utf-8") if form else None,
            )
        except OSError as exc:
            raise ProviderError(f"Twilio could not be reached: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    def place_call(self, request: PlaceCall) -> str:
        form = [
            ("To", request.to_number),
            ("From", request.from_number),
            ("Url", _with_call(self._answer_url, request.call_id)),
            ("StatusCallback", _with_call(self._status_url, request.call_id)),
            ("StatusCallbackMethod", "POST"),
            *[("StatusCallbackEvent", e) for e in ("initiated", "ringing", "answered", "completed")],
            ("TimeLimit", str(request.max_seconds)),
            ("Timeout", str(self._ring_seconds)),
        ]
        if request.detect_machine:
            form.append(("MachineDetection", "Enable"))
        status, data = self._post("Calls.json", form)
        if 200 <= status < 300 and data.get("sid"):
            return str(data["sid"])
        code = data.get("code")
        reason = _ERRORS.get(code, FailureReason.PROVIDER_FAILURE) if isinstance(code, int) \
            else FailureReason.PROVIDER_FAILURE
        raise ProviderError(
            f"Twilio refused the call ({status}, code {code}): {data.get('message', '')}".strip(),
            reason=reason,
        )

    def end_call(self, provider_call_ref: str) -> None:
        status, data = self._post(f"Calls/{provider_call_ref}.json", [("Status", "completed")])
        # 404 is a call Twilio no longer has; 400 is one no longer in progress. Both mean
        # "already ended", which is the state this method exists to reach.
        if not (200 <= status < 300 or status in (400, 404)):
            raise ProviderError(f"Twilio could not end the call ({status}): {data.get('message', '')}")

    def fetch_status(self, provider_call_ref: str) -> Optional[ProviderEvent]:
        auth = base64.b64encode(f"{self._sid}:{self._token}".encode()).decode("ascii")
        try:
            status, body = self._transport(
                "GET", f"{API_BASE}/{self._sid}/Calls/{provider_call_ref}.json",
                {"Authorization": f"Basic {auth}"}, None,
            )
        except OSError as exc:
            raise ProviderError(f"Twilio could not be reached: {exc}") from exc
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise ProviderError(f"Twilio could not report the call ({status})")
        try:
            data = json.loads(body.decode("utf-8"))
            raw = str(data["status"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(f"unreadable call resource: {exc}") from exc
        if raw not in _STATUS:
            raise ProviderError(f"unknown Twilio call status {raw!r}")
        mapped, failure = _STATUS[raw]
        if str(data.get("answered_by") or "") in _MACHINE:
            mapped, failure = CallStatus.FAILED, FailureReason.MACHINE_DETECTED
        return ProviderEvent(
            provider=self.id,
            # Keyed by the status it reports: asking twice about the same state is one event.
            provider_event_id=f"{provider_call_ref}:fetched:{raw}",
            provider_call_ref=provider_call_ref,
            status=mapped,
            failure=failure,
        )

    def start_recording(self, call_id: str, provider_call_ref: str) -> str:
        status, data = self._post(f"Calls/{provider_call_ref}/Recordings.json", [
            ("RecordingStatusCallback", _with_call(self._recording_url, call_id)),
            ("RecordingStatusCallbackEvent", "in-progress completed absent"),
            ("RecordingChannels", "dual"),
            ("RecordingTrack", "both"),
        ])
        if 200 <= status < 300 and data.get("sid"):
            return str(data["sid"])
        raise ProviderError(f"Twilio could not start recording ({status}): {data.get('message', '')}")

    def set_recording_paused(self, provider_call_ref: str, recording_ref: str, paused: bool) -> None:
        form = [("Status", "paused"), ("PauseBehavior", "skip")] if paused \
            else [("Status", "in-progress")]
        status, data = self._post(f"Calls/{provider_call_ref}/Recordings/{recording_ref}.json", form)
        if not 200 <= status < 300:
            raise ProviderError(f"Twilio could not {'pause' if paused else 'resume'} the "
                                f"recording ({status}): {data.get('message', '')}")

    def delete_recording(self, recording_ref: str) -> None:
        status, data = self._post(f"Recordings/{recording_ref}.json", [], method="DELETE")
        if not (200 <= status < 300 or status == 404):
            raise ProviderError(f"Twilio could not delete the recording ({status}): "
                                f"{data.get('message', '')}")

    @staticmethod
    def _form(body: bytes) -> dict[str, str]:
        return {k: v[-1] for k, v in urllib.parse.parse_qs(
            body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    @staticmethod
    def _hint(url: str) -> str:
        return (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("call") or [""])[0]

    def parse_consent(self, headers: Mapping[str, str], body: bytes, url: str) -> ConsentReply:
        form = self._form(body)
        if not form.get("CallSid"):
            raise ValueError("not a Twilio gather callback")
        return ConsentReply(form["CallSid"], self._hint(url), digits=form.get("Digits", ""),
                            speech=form.get("SpeechResult", ""))

    def parse_recording(self, headers: Mapping[str, str], body: bytes, url: str) -> RecordingEvent:
        form = self._form(body)
        if not (form.get("RecordingSid") and form.get("CallSid") and form.get("RecordingStatus")):
            raise ValueError("not a Twilio recording callback")
        try:
            duration = int(form.get("RecordingDuration") or 0)
        except ValueError:
            duration = 0
        return RecordingEvent(form["RecordingSid"], form["CallSid"], self._hint(url),
                              form["RecordingStatus"], duration)

    def verify(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        if not given:
            return False
        params = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return any(
            hmac.compare_digest(given, signature(self._token, variant, params))
            for variant in _port_variants(url)
        )

    def parse(self, headers: Mapping[str, str], body: bytes, url: str) -> ProviderEvent:
        form = {k: v[-1] for k, v in urllib.parse.parse_qs(
            body.decode("utf-8", "replace"), keep_blank_values=True).items()}
        call_sid, raw_status = form.get("CallSid", ""), form.get("CallStatus", "")
        if not call_sid or raw_status not in _STATUS:
            raise ValueError(f"not a Twilio call callback (CallStatus={raw_status!r})")
        status, failure = _STATUS[raw_status]
        answered_by = form.get("AnsweredBy", "")
        if answered_by in _MACHINE:
            status, failure = CallStatus.FAILED, FailureReason.MACHINE_DETECTED

        # Status callbacks carry a sequence number; the answer request does not, and Twilio
        # may retry it, so it is keyed as the one answer it is.
        sequence = form.get("SequenceNumber")
        event_id = f"{call_sid}:{sequence}" if sequence is not None else f"{call_sid}:answer"

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return ProviderEvent(
            provider=self.id,
            provider_event_id=event_id,
            provider_call_ref=call_sid,
            status=status,
            occurred_at=form.get("Timestamp", ""),
            failure=failure,
            call_id_hint=(query.get("call") or [""])[0],
            detail={"answered_by": answered_by} if answered_by else {},
        )

    def render_answer(self, plan: AnswerPlan) -> tuple[str, bytes]:
        parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"]
        language = f" language={quoteattr(plan.language)}" if plan.language else ""
        if plan.consent is not None and not plan.hang_up:
            if plan.say:
                parts.append(f"<Say{language}>{escape(plan.say)}</Say>")
            action = quoteattr(_with_call(self._consent_url, plan.consent.call_id))
            # actionOnEmptyResult: silence is reported too, and recorded as no answer —
            # never skipped past into a recorded call.
            parts.append(
                f'<Gather input="dtmf speech" numDigits="1" timeout="6" speechTimeout="auto" '
                f'actionOnEmptyResult="true" method="POST" action={action}{language}>'
                f"<Say{language}>{escape(plan.consent.prompt)}</Say></Gather>"
            )
        elif plan.hang_up or plan.connect is None:
            if plan.say:
                parts.append(f"<Say{language}>{escape(plan.say)}</Say>")
            parts.append("<Hangup/>")
        else:
            if plan.say:
                parts.append(f"<Say{language}>{escape(plan.say)}</Say>")
            if isinstance(plan.connect, StreamTarget):
                params = "".join(
                    f"<Parameter name={quoteattr(name)} value={quoteattr(value)}/>"
                    for name, value in plan.connect.parameters.items()
                )
                parts.append(
                    f"<Connect><Stream url={quoteattr(plan.connect.url)}>{params}</Stream></Connect>"
                )
            else:
                query = urllib.parse.urlencode(dict(plan.connect.headers))
                uri = f"{plan.connect.uri}?{query}" if query else plan.connect.uri
                parts.append(f"<Dial><Sip>{escape(uri)}</Sip></Dial>")
        parts.append("</Response>")
        return "text/xml", "".join(parts).encode("utf-8")
