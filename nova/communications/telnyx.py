"""Telnyx as a carrier, through Call Control v2.

Telnyx works by command, not by answer document. Every event of a call goes to one signed
webhook, and the call does what the application tells it through the REST API. So where
Twilio is handed TwiML, this adapter *carries out* the gateway's plan for the call:

* ``say`` is ``speak``. The step after it travels in ``client_state`` and runs when Telnyx
  reports ``call.speak.ended``, so the disclosure is always heard in full before anything
  else happens.
* a consent question is ``gather_using_speak`` (one key: 1 yes, 2 no). Telnyx's gather
  hears keys, not speech, so a spoken "yes" counts as no answer — which records nothing.
* a SIP voice engine is ``transfer`` with our call id and token as custom SIP headers.
* a stream engine is ``streaming_start``, bidirectional RTP in PCMU (8 kHz mu-law, the
  same audio the cascaded engine already speaks), with our call id and token as stream
  parameters.
* hanging up is ``hangup``.

Webhooks are verified with Ed25519 over ``<timestamp>|<raw body>`` against the account's
public key, and refused outside a five-minute window. Our call id rides in
``client_state`` on every command, so an event finds its call even before the carrier
reference is stored.

Vendor contract: https://developers.telnyx.com/api-reference/call-commands and the call
webhook reference.

**Recording** is dual-channel ``record_start`` after an explicit yes, paused and resumed by
the payment guard with ``record_pause`` / ``record_resume``, and deleted by listing the
call's saved files and deleting each. Consent is asked by key press: tenants using Telnyx
should word ``consent_prompt`` as a key-press question.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from nova.communications import ed25519
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

API_BASE = "https://api.telnyx.com/v2"
SIGNATURE_HEADER = "telnyx-signature-ed25519"
TIMESTAMP_HEADER = "telnyx-timestamp"
TOLERANCE = 300
#: How a call's recording is known before Telnyx names the saved file.
RECORDING_PREFIX = "telnyx-rec:"
DEFAULT_VOICE = "female"

#: ``call.hangup``'s cause -> how the call ended. Anything else is a provider failure.
_HANGUPS: Mapping[str, tuple[CallStatus, Optional[FailureReason]]] = {
    "normal_clearing": (CallStatus.COMPLETED, None),
    "time_limit": (CallStatus.COMPLETED, None),
    "originator_cancel": (CallStatus.CANCELLED, None),
    "user_busy": (CallStatus.FAILED, FailureReason.BUSY),
    "timeout": (CallStatus.FAILED, FailureReason.NO_ANSWER),
    "no_answer": (CallStatus.FAILED, FailureReason.NO_ANSWER),
    "not_found": (CallStatus.FAILED, FailureReason.INVALID_NUMBER),
    "unallocated_number": (CallStatus.FAILED, FailureReason.INVALID_NUMBER),
    "call_rejected": (CallStatus.FAILED, FailureReason.CARRIER_REJECTED),
}


def _state(data: Mapping[str, Any]) -> str:
    return base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode("ascii")


def _read_state(raw: Any) -> dict:
    try:
        data = json.loads(base64.b64decode(str(raw or ""), validate=True) or b"{}")
    except (ValueError, binascii.Error):
        return {}
    return data if isinstance(data, dict) else {}


def _call_id_of(plan: AnswerPlan) -> str:
    """Our call id, wherever the plan carries it, so every later webhook still names it."""
    if plan.consent is not None:
        return plan.consent.call_id
    if isinstance(plan.connect, StreamTarget):
        return str(plan.connect.parameters.get("call_id", ""))
    if plan.connect is not None:
        return str({k.lower(): v for k, v in plan.connect.headers.items()}.get("x-call-id", ""))
    return ""


def _seconds_between(start: str, end: str) -> int:
    try:
        return max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                           datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))
    except ValueError:
        return 0


class TelnyxTelephony(TelephonyProvider):
    id = "telnyx"
    #: The gateway carries out answer plans through :meth:`redirect`; nothing is rendered.
    follows_commands = True

    def __init__(self, *, api_key: str, public_key: str, connection_id: str, webhook_url: str,
                 voice: str = DEFAULT_VOICE, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time, ring_seconds: int = 30) -> None:
        if not (api_key and public_key and connection_id):
            raise ValueError("Telnyx needs an API key, the account's public key and a "
                             "Call Control connection id")
        if not webhook_url.startswith("https://"):
            raise ValueError(f"the Telnyx webhook URL must be public HTTPS, got {webhook_url!r}")
        self._key = api_key
        self._public_key = public_key
        self._connection = connection_id
        self._webhook = webhook_url
        self._voice = voice
        self._transport = transport
        self._clock = clock
        self._ring_seconds = ring_seconds

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, connection_id: str, webhook_url: str,
                 **options) -> "TelnyxTelephony":
        return cls(api_key=env.get("TELNYX_API_KEY", ""),
                   public_key=env.get("TELNYX_PUBLIC_KEY", ""), connection_id=connection_id,
                   webhook_url=webhook_url, **options)

    # -- REST -----------------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> tuple[int, dict]:
        try:
            status, body = self._transport(
                method, f"{API_BASE}/{path}",
                {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
                json.dumps(payload).encode("utf-8") if payload is not None else None,
            )
        except OSError as exc:
            raise ProviderError(f"Telnyx could not be reached: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    def _command(self, ref: str, action: str, payload: dict) -> None:
        status, data = self._request("POST", f"calls/{ref}/actions/{action}", payload)
        if not 200 <= status < 300:
            detail = ((data.get("errors") or [{}])[0] or {}).get("detail", "")
            raise ProviderError(f"Telnyx refused {action} ({status}): {detail}")

    def place_call(self, request: PlaceCall) -> str:
        payload = {
            "connection_id": self._connection,
            "to": request.to_number,
            "from": request.from_number,
            "webhook_url": self._webhook,
            "client_state": _state({"c": request.call_id, "amd": int(request.detect_machine)}),
            # Our call id is the dial's idempotency key: a retried dial is one call.
            "command_id": request.call_id,
            "time_limit_secs": max(30, request.max_seconds),
            "timeout_secs": self._ring_seconds,
            "answering_machine_detection": "detect" if request.detect_machine else "disabled",
        }
        status, data = self._request("POST", "calls", payload)
        ref = str((data.get("data") or {}).get("call_control_id") or "")
        if 200 <= status < 300 and ref:
            return ref
        error = (data.get("errors") or [{}])[0] or {}
        reason = FailureReason.INVALID_NUMBER if status == 422 and "number" in \
            str(error.get("detail", "")).lower() else FailureReason.PROVIDER_FAILURE
        raise ProviderError(f"Telnyx could not dial ({status}): {error.get('detail', '')}",
                            reason=reason)

    def end_call(self, provider_call_ref: str) -> None:
        status, _ = self._request("POST", f"calls/{provider_call_ref}/actions/hangup", {})
        # 422: the call already ended, which is what was asked.
        if not (200 <= status < 300 or status in (404, 422)):
            raise ProviderError(f"Telnyx could not end the call ({status})")

    def fetch_status(self, provider_call_ref: str) -> Optional[ProviderEvent]:
        status, data = self._request("GET", f"calls/{provider_call_ref}")
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise ProviderError(f"Telnyx could not report the call ({status})")
        alive = bool((data.get("data") or {}).get("is_alive"))
        return ProviderEvent(
            provider=self.id,
            provider_event_id=f"{provider_call_ref}:fetched:{'alive' if alive else 'ended'}",
            provider_call_ref=provider_call_ref,
            status=CallStatus.ANSWERED if alive else CallStatus.COMPLETED)

    # -- carrying out a plan --------------------------------------------------------------

    def _steps(self, plan: AnswerPlan) -> list[dict]:
        """The plan as a list of commands, run one after another."""
        steps: list[dict] = []
        if plan.say:
            steps.append({"op": "speak", "text": plan.say})
        if plan.consent is not None and not plan.hang_up:
            steps.append({"op": "gather", "text": plan.consent.prompt, "c": plan.consent.call_id})
        elif plan.hang_up or plan.connect is None:
            steps.append({"op": "hangup"})
        elif isinstance(plan.connect, StreamTarget):
            steps.append({"op": "stream", "url": plan.connect.url,
                          "params": dict(plan.connect.parameters)})
        else:
            steps.append({"op": "transfer", "to": plan.connect.uri,
                          "headers": dict(plan.connect.headers)})
        return steps

    def redirect(self, provider_call_ref: str, plan: AnswerPlan) -> None:
        self._run(provider_call_ref, self._steps(plan), plan.language,
                  {"c": _call_id_of(plan)} if _call_id_of(plan) else {})

    def _run(self, ref: str, steps: list[dict], language: str, state: dict) -> None:
        if not steps:
            return
        step, rest = steps[0], steps[1:]
        carried = {k: v for k, v in state.items() if k in ("c", "amd")}
        language = language or "en-US"
        if step["op"] == "speak":
            # What comes after the disclosure waits for it to finish.
            self._command(ref, "speak", {
                "payload": step["text"], "voice": self._voice, "language": language,
                "client_state": _state({**carried, "next": rest, "lang": language})})
        elif step["op"] == "gather":
            self._command(ref, "gather_using_speak", {
                "payload": step["text"], "voice": self._voice, "language": language,
                "valid_digits": "12", "minimum_digits": 1, "maximum_digits": 1,
                "timeout_millis": 6000,
                "client_state": _state({**carried, "c": step["c"] or carried.get("c", "")})})
        elif step["op"] == "hangup":
            self.end_call(ref)
        elif step["op"] == "stream":
            self._command(ref, "streaming_start", {
                "stream_url": step["url"], "stream_track": "inbound_track",
                "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
                "custom_parameters": [{"name": k, "value": v} for k, v in step["params"].items()],
                "client_state": _state(carried)})
        elif step["op"] == "transfer":
            self._command(ref, "transfer", {
                "to": step["to"],
                "custom_headers": [{"name": k, "value": v} for k, v in step["headers"].items()],
                "client_state": _state(carried)})
        else:
            raise ProviderError(f"unknown Telnyx step {step['op']!r}")

    def render_answer(self, plan: AnswerPlan) -> tuple[str, bytes]:
        """Telnyx is not handed a document; this is the plan as it would be carried out."""
        return "application/json", json.dumps(self._steps(plan), sort_keys=True).encode("utf-8")

    # -- webhooks -------------------------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature, timestamp = lowered.get(SIGNATURE_HEADER, ""), lowered.get(TIMESTAMP_HEADER, "")
        if not (signature and timestamp.isdigit()) or abs(self._clock() - int(timestamp)) > TOLERANCE:
            return False
        return ed25519.verify(self._public_key, signature, timestamp.encode() + b"|" + body)

    @staticmethod
    def _event(body: bytes) -> tuple[str, dict, dict, dict]:
        """``(event type, payload, client state, envelope)``."""
        try:
            data = json.loads(body.decode("utf-8")).get("data") or {}
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            raise ValueError(f"unreadable Telnyx webhook: {exc}") from exc
        payload = data.get("payload") or {}
        return (str(data.get("event_type") or ""), payload,
                _read_state(payload.get("client_state")), data)

    def event_kind(self, headers: Mapping[str, str], body: bytes) -> str:
        """Which of the gateway's handlers a verified webhook is for:
        ``answer``, ``consent``, ``status``, ``continue`` or ``ignore``."""
        kind, _, state, _ = self._event(body)
        if kind == "call.answered":
            # With machine detection on, the person is only known to be there once it ends.
            return "status" if state.get("amd") else "answer"
        if kind == "call.speak.ended":
            return "continue" if state.get("next") else "ignore"
        return {"call.machine.detection.ended": "answer", "call.gather.ended": "consent",
                "call.initiated": "status", "call.hangup": "status",
                "call.recording.saved": "recording",
                "call.recording.error": "recording"}.get(kind, "ignore")

    def continue_plan(self, headers: Mapping[str, str], body: bytes) -> None:
        """Run the steps a finished ``speak`` was holding back."""
        _, payload, state, _ = self._event(body)
        ref = str(payload.get("call_control_id") or "")
        steps = state.get("next")
        if ref and isinstance(steps, list):
            self._run(ref, steps, str(state.get("lang") or ""), state)

    def parse(self, headers: Mapping[str, str], body: bytes, url: str) -> ProviderEvent:
        kind, payload, state, envelope = self._event(body)
        event_id = str(envelope.get("id") or "")
        failure: Optional[FailureReason] = None
        detail: dict[str, Any] = {}
        if kind == "call.initiated":
            status = CallStatus.INITIATED
        elif kind == "call.answered":
            status = CallStatus.ANSWERED
        elif kind == "call.machine.detection.ended":
            # "not_sure" is treated as a person, as Twilio's "unknown" is: hanging up on a
            # person is worse than one disclosure spoken to a machine.
            status = CallStatus.ANSWERED
            if str(payload.get("result") or "") == "machine":
                status, failure = CallStatus.FAILED, FailureReason.MACHINE_DETECTED
            detail["answered_by"] = str(payload.get("result") or "")
        elif kind == "call.hangup":
            cause = str(payload.get("hangup_cause") or "")
            status, failure = _HANGUPS.get(cause, (CallStatus.FAILED,
                                                   FailureReason.PROVIDER_FAILURE))
            seconds = _seconds_between(str(payload.get("start_time") or ""),
                                       str(envelope.get("occurred_at") or ""))
            if seconds:
                detail["duration_seconds"] = seconds
            detail["hangup_cause"] = cause
        else:
            raise ValueError(f"not a Telnyx call status event: {kind!r}")
        if not event_id:
            raise ValueError("a Telnyx webhook without an id")
        return ProviderEvent(provider=self.id, provider_event_id=event_id,
                             provider_call_ref=str(payload.get("call_control_id") or ""),
                             status=status, occurred_at=str(envelope.get("occurred_at") or ""),
                             failure=failure, call_id_hint=str(state.get("c") or ""),
                             detail=detail)

    def parse_consent(self, headers: Mapping[str, str], body: bytes, url: str) -> ConsentReply:
        kind, payload, state, _ = self._event(body)
        if kind != "call.gather.ended":
            raise ValueError(f"not a Telnyx gather result: {kind!r}")
        digits = str(payload.get("digits") or "") if payload.get("status") == "valid" else ""
        return ConsentReply(str(payload.get("call_control_id") or ""), str(state.get("c") or ""),
                            digits=digits)

    # -- recording ------------------------------------------------------------------------

    def start_recording(self, call_id: str, provider_call_ref: str) -> str:
        """Dual-channel, from now. Telnyx names the file only once it is saved, so the
        recording is known here by its call: one recording per call, ``telnyx-rec:<call>``."""
        self._command(provider_call_ref, "record_start", {
            "format": "mp3", "channels": "dual", "command_id": f"rec-{call_id}",
            "client_state": _state({"c": call_id})})
        return f"{RECORDING_PREFIX}{provider_call_ref}"

    def set_recording_paused(self, provider_call_ref: str, recording_ref: str, paused: bool) -> None:
        self._command(provider_call_ref, "record_pause" if paused else "record_resume", {})

    def delete_recording(self, recording_ref: str) -> None:
        """Delete every file Telnyx saved for the call. Until one is saved there is nothing
        to delete yet, which is an error so the retention sweep asks again — never a
        recording marked deleted that still exists."""
        if not recording_ref.startswith(RECORDING_PREFIX):
            raise ProviderError(f"not a Telnyx recording reference: {recording_ref!r}")
        call = recording_ref[len(RECORDING_PREFIX):]
        status, data = self._request(
            "GET", "recordings?" + urllib.parse.urlencode({"filter[call_control_id]": call}))
        if not 200 <= status < 300:
            raise ProviderError(f"Telnyx could not list the call's recordings ({status})")
        items = data.get("data") if isinstance(data.get("data"), list) else []
        ids = [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]
        if not ids:
            raise ProviderError("Telnyx has not saved this recording yet; deletion will retry")
        for recording_id in ids:
            status, _ = self._request("DELETE", f"recordings/{recording_id}")
            if not (200 <= status < 300 or status == 404):
                raise ProviderError(f"Telnyx could not delete recording {recording_id} ({status})")

    def parse_recording(self, headers: Mapping[str, str], body: bytes, url: str) -> RecordingEvent:
        kind, payload, state, _ = self._event(body)
        if kind not in ("call.recording.saved", "call.recording.error"):
            raise ValueError(f"not a Telnyx recording event: {kind!r}")
        call = str(payload.get("call_control_id") or "")
        hint = str(state.get("c") or "")
        if not (call or hint):
            raise ValueError("a recording event that names no call")
        saved = kind == "call.recording.saved"
        seconds = _seconds_between(str(payload.get("recording_started_at") or ""),
                                   str(payload.get("recording_ended_at") or "")) if saved else 0
        return RecordingEvent(f"{RECORDING_PREFIX}{call}" if call else "", call, hint,
                              "completed" if saved else "failed", seconds)
