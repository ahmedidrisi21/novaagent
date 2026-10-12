"""Call recording: consent first, pauses around payment details, deletion on schedule.

**One rule, every market.** Recording starts only after the person on the line says yes
— asked by the carrier, in the tenant's words, before any model speaks. Several US states
need every party's consent; asking everyone, every time, satisfies all of them and needs
no per-state logic that could be wrong. Only an explicit yes counts: a no, silence, or an
answer that could be either means the call goes on unrecorded (or ends, if the profile
says so). The answer is kept as evidence — what was pressed or said, as the carrier
heard it.

**Payment details are not recorded.** :class:`PaymentGuard` hears the call as it is
recognised, partial results included, and pauses the recording the moment card or account
talk starts — before the number, not after it. It resumes after a stretch of ordinary
conversation. It errs towards pausing: a phone number read out pauses too, and that is the
right side to be wrong on.

**Recordings expire.** Each one carries its profile's retention; the sweep deletes them at
the carrier when it passes, and a person's recordings can be deleted on request.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from datetime import timedelta
from typing import Callable, Optional

from nova.audit import new_correlation_id
from nova.communications.contracts import AnswerPlan, CallStatus, ProviderError

GRANTED, REFUSED, UNCLEAR = "granted", "refused", "unclear"

_YES = ("no problem", "that's fine", "thats fine", "go ahead", "of course", "all right",
        "you may", "you can", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "fine",
        "alright", "absolutely", "certainly", "allow")
_NO = ("rather not", "do not", "don't", "dont", "not ok", "not okay", "no", "nope", "nah",
       "refuse", "decline", "never", "stop")


def _has(phrase: str, text: str) -> bool:
    return re.search(rf"(?<![\w']){re.escape(phrase)}(?![\w'])", text) is not None


def classify_consent(digits: str, speech: str) -> tuple[str, str]:
    """``(outcome, evidence)``. Deterministic on purpose: a model deciding whether someone
    consented is a model that can be talked into it."""
    digits = (digits or "").strip()
    if digits == "1":
        return GRANTED, "pressed 1"
    if digits == "2":
        return REFUSED, "pressed 2"
    text = " ".join(re.sub(r"[^\w' ]", " ", (speech or "").lower()).split())
    if not text:
        return UNCLEAR, f"pressed {digits}" if digits else "no answer"
    yes = [p for p in _YES if _has(p, text)]
    remainder = text
    for phrase in yes:
        remainder = re.sub(rf"(?<![\w']){re.escape(phrase)}(?![\w'])", " ", remainder)
    no = any(_has(p, remainder) for p in _NO)
    evidence = f'said "{speech.strip()[:120]}"'
    if yes and not no:
        return GRANTED, evidence
    if no and not yes:
        return REFUSED, evidence
    return UNCLEAR, evidence


#: Card, bank and identity talk, or a run of eight or more digits however it is spaced.
SENSITIVE = re.compile(
    r"\b(card number|credit card|debit card|card details|long number on|cvv|cvc|"
    r"security code|expir\w*|sort code|account number|routing number|pin|"
    r"social security|bank details)\b|\d(?:[\s-]?\d){7,}",
    re.IGNORECASE,
)

#: Seconds of conversation without sensitive talk before a paused recording resumes.
RESUME_AFTER = 20.0


class PaymentGuard:
    """Pauses a recording around payment details. Fed everything said, partials included."""

    def __init__(self, pause: Callable[[], None], resume: Callable[[], None], *,
                 clock: Callable[[], float] = time.monotonic,
                 resume_after: float = RESUME_AFTER) -> None:
        self._pause, self._resume, self._clock = pause, resume, clock
        self._resume_after = resume_after
        self._lock = threading.Lock()
        self._paused = False
        self._last = 0.0

    @property
    def paused(self) -> bool:
        return self._paused

    def heard(self, speaker: str, text: str) -> None:
        action = None
        with self._lock:
            now = self._clock()
            if SENSITIVE.search(text or ""):
                self._last = now
                if not self._paused:
                    self._paused, action = True, self._pause
            elif self._paused and now - self._last >= self._resume_after:
                self._paused, action = False, self._resume
        if action is not None:
            action()


def _iso(gateway, delta: timedelta = timedelta()) -> str:
    from nova.communications.gateway import _iso as iso

    return iso(gateway.now() + delta)


# -- consent, at the carrier ---------------------------------------------------------------


def consent_reply(gateway, carrier_id: str, *, headers, body: bytes, url: str = ""):
    """The carrier reports the person's answer. Record it, start recording on a yes, then
    connect the voice engine — or end the call, if the profile ends on refusal."""
    from nova.communications.gateway import AnswerResponse

    carrier = gateway.carriers.get(carrier_id)
    if carrier is None:
        return AnswerResponse(404, body=b"no such carrier")
    if not carrier.verify(headers, body, url):
        gateway.audit.record("communications.callback.unverified",
                             correlation_id=new_correlation_id(), subject=carrier_id,
                             detail={"bytes": len(body)})
        return AnswerResponse(403, body=b"signature did not verify")

    reply = None

    def hang_up(say: str = ""):
        return gateway.respond(carrier, reply.provider_call_ref if reply else "",
                               AnswerPlan(hang_up=True, say=say))

    try:
        reply = carrier.parse_consent(headers, body, url)
    except ValueError:
        return hang_up()
    record = gateway.store.call_by_ref(carrier.id, reply.provider_call_ref)
    if record is None and reply.call_id_hint:
        record = gateway.store.call_for_carrier(carrier.id, reply.call_id_hint,
                                                reply.provider_call_ref)
    profile = gateway.spec.profile(record.agent) if record else None
    if record is None or profile is None or record.status is not CallStatus.ANSWERED:
        return hang_up()

    outcome, evidence = classify_consent(reply.digits, reply.speech)
    correlation = new_correlation_id()
    if not gateway.store.set_recording_consent(record.call_id, outcome, evidence,
                                               at=_iso(gateway)):
        # A retried callback: the first answer stands, and so does what it led to.
        record = gateway.store.call(record.call_id)
        outcome = record.recording_consent
    else:
        gateway.audit.record("communications.recording.consent", correlation_id=correlation,
                             subject=record.call_id,
                             detail={"outcome": outcome, "evidence": evidence})
        if outcome == GRANTED:
            _start(gateway, carrier, record, profile, correlation)

    if outcome != GRANTED and profile.recording.on_refusal == "end":
        gateway.store.advance(record.call_id, CallStatus.CANCELLED, at=_iso(gateway),
                              reason="recording consent not given; profile ends such calls")
        return hang_up("Understood. We'll be in touch another way. Goodbye.")
    plan = gateway.bridge_plan(record, profile, say="")
    return gateway.respond(carrier, record.provider_call_ref, plan) if plan else hang_up()


def _start(gateway, carrier, record, profile, correlation: str) -> None:
    try:
        ref = carrier.start_recording(record.call_id, record.provider_call_ref)
    except ProviderError as exc:
        gateway.audit.record("communications.recording.failed", correlation_id=correlation,
                             subject=record.call_id, error=str(exc))
        return
    gateway.store.add_recording(
        record.call_id, ref, at=_iso(gateway),
        retention_until=_iso(gateway, timedelta(days=profile.recording.retention_days)),
    )
    gateway.audit.record("communications.recording.started", correlation_id=correlation,
                         subject=record.call_id, detail={"recording": ref})


# -- during the call -----------------------------------------------------------------------


def set_paused(gateway, call_id: str, paused: bool, *, actor: str) -> int:
    """Pause or resume every live recording on a call. Returns how many changed."""
    record = gateway.store.call(call_id)
    if record is None:
        raise KeyError(f"no call {call_id!r} in this tenant")
    carrier = gateway.carriers.get(record.telephony)
    want, have = ("paused", "recording") if paused else ("recording", "paused")
    changed = 0
    for recording in gateway.store.recordings(call_id=call_id):
        if recording["state"] != have or carrier is None:
            continue
        carrier.set_recording_paused(record.provider_call_ref, recording["recording_ref"], paused)
        gateway.store.set_recording_state(recording["recording_ref"], want)
        changed += 1
    if changed:
        gateway.audit.record(f"communications.recording.{'paused' if paused else 'resumed'}",
                             correlation_id=new_correlation_id(), subject=call_id,
                             detail={"actor": actor})
    return changed


def guard_for(gateway, record) -> Optional[Callable[[str, str], None]]:
    """The payment guard for a call whose profile records, or None."""
    profile = gateway.spec.profile(record.agent)
    if profile is None or not profile.recording.enabled:
        return None

    def act(paused: bool) -> Callable[[], None]:
        def run() -> None:
            try:
                set_paused(gateway, record.call_id, paused, actor="payment-guard")
            except ProviderError as exc:
                gateway.audit.record("communications.recording.guard_failed",
                                     correlation_id=new_correlation_id(),
                                     subject=record.call_id, error=str(exc))
        return run

    return PaymentGuard(act(True), act(False)).heard


# -- afterwards ----------------------------------------------------------------------------

_STATES = {"in-progress": None, "completed": "completed", "absent": "absent", "failed": "failed"}


def recording_event(gateway, carrier_id: str, *, headers, body: bytes, url: str = ""):
    from nova.communications.gateway import IngestResult

    carrier = gateway.carriers.get(carrier_id)
    if carrier is None:
        return IngestResult("rejected", f"no {carrier_id!r} adapter", http_status=404)
    if not carrier.verify(headers, body, url):
        return IngestResult("rejected", "signature did not verify", http_status=403)
    try:
        event = carrier.parse_recording(headers, body, url)
    except ValueError as exc:
        return IngestResult("rejected", str(exc), http_status=400)
    known = {r["recording_ref"]: r for r in gateway.store.recordings(
        call_id=event.call_id_hint)} if event.call_id_hint else {}
    if not event.recording_ref and len(known) == 1:
        # The carrier named only the call (by our id); it has one recording.
        event = replace(event, recording_ref=next(iter(known)))
    if event.recording_ref not in known:
        return IngestResult("ignored", "not a recording this tenant started")
    state = _STATES.get(event.status)
    if state is None:
        return IngestResult("ignored", "in progress", known[event.recording_ref]["call_id"])
    gateway.store.set_recording_state(event.recording_ref, state, duration=event.duration_seconds)
    return IngestResult("applied", call_id=known[event.recording_ref]["call_id"])


def delete_recordings(gateway, recordings, *, reason: str, actor: str) -> int:
    deleted = 0
    for recording in recordings:
        record = gateway.store.call(recording["call_id"])
        carrier = gateway.carriers.get(record.telephony) if record else None
        if carrier is None:
            continue
        try:
            carrier.delete_recording(recording["recording_ref"])
        except ProviderError as exc:
            gateway.audit.record("communications.recording.delete_failed",
                                 correlation_id=new_correlation_id(),
                                 subject=recording["call_id"], error=str(exc))
            continue
        gateway.store.set_recording_state(recording["recording_ref"], "deleted", at=_iso(gateway))
        gateway.audit.record("communications.recording.deleted", correlation_id=new_correlation_id(),
                             subject=recording["call_id"],
                             detail={"recording": recording["recording_ref"], "reason": reason,
                                     "actor": actor})
        deleted += 1
    return deleted


def expire(gateway) -> int:
    """Delete every recording past its retention. Run by the sweep."""
    return delete_recordings(gateway, gateway.store.recordings(due_before=_iso(gateway)),
                             reason="retention period ended", actor="retention")


def forget_contact(gateway, contact_id: str, *, actor: str) -> int:
    """A person asked for their recordings to be deleted."""
    return delete_recordings(gateway, gateway.store.recordings(contact_id=contact_id),
                             reason="deletion requested for this contact", actor=actor)
