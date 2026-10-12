"""The one path from "an agent wants to call someone" to a carrier dialling.

An agent supplies four things: who it is, which contact, which purpose, and an idempotency
key. Everything that reaches the carrier — the number, the caller id, the duration — is
resolved here from the tenant's own records. A model cannot put a phone number into a call
because no parameter accepts one.

**The gate's caps hold only for calls that go through here.** That is true by construction
for any agent that holds no carrier credential, and false for one that does: the runtime's
SMS adapter reads Twilio credentials from an agent's environment, and an agent with those
can call Twilio's API directly. ``docs/platform/COMMUNICATIONS.md`` says so where an operator
will read it, and the provider catalogue repeats it on the Twilio entry.

Approval is bound to the action, not the call id. The request stores a digest of the
resolved number, caller id, carrier, purpose and duration; an approval is honoured only if
that digest still matches when the approver clicks, so editing the contact's number or the
agent's profile after the request voids the approval instead of riding it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Mapping, Optional

from nova.audit import AuditLog, new_correlation_id
from nova.communications.contracts import (
    Answerer,
    AnswerPlan,
    CallHooks,
    CallStatus,
    ConsentPrompt,
    FailureReason,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    SipVoiceEngine,
    StreamVoiceEngine,
    TelephonyProvider,
    VoiceEngine,
    VoiceSessionRequest,
)
from nova.communications.gate import (
    ALLOW,
    DENY,
    CallDecision,
    CallFacts,
    decide_call,
)
from nova.communications.mail import EmailProvider
from nova.communications.recording import guard_for
from nova.communications.spec import CommunicationsSpec
from nova.communications.store import CallRecord, CommsStore
from nova.communications.usage import exhausted

if TYPE_CHECKING:
    from nova.runtime.base import WorkItem


#: Said while the call moves to the next engine, so the silence has a reason.
FAILOVER_LINE = "One moment, please."
#: An engine that failed this often within this window is passed over for new calls.
ENGINE_FAILURES_TO_SKIP = 3
ENGINE_WINDOW = timedelta(minutes=10)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CallResult:
    record: CallRecord
    decision: Optional[CallDecision]
    #: False when the idempotency key matched an earlier request and nothing new happened.
    created: bool


@dataclass(frozen=True)
class IngestResult:
    #: ``applied``, ``ignored`` (seen before, or would move the call backwards) or
    #: ``rejected`` (unsigned, unparseable, or for a call this tenant does not have).
    outcome: str
    reason: str = ""
    call_id: str = ""
    #: What to answer the sender: 403 for a bad signature, 400 for anything else
    #: rejected. A carrier that gets a 2xx stops retrying, which is right for a duplicate.
    http_status: int = 200


@dataclass(frozen=True)
class AnswerResponse:
    http_status: int
    content_type: str = "text/plain"
    body: bytes = b""


class CallGateway:
    def __init__(
        self,
        spec: CommunicationsSpec,
        store: CommsStore,
        audit: AuditLog,
        carriers: Mapping[str, TelephonyProvider],
        *,
        voices: Optional[Mapping[str, VoiceEngine]] = None,
        call_secret: bytes = b"",
        answers: Optional[Callable[[CallRecord], Optional[Answerer]]] = None,
        extractor=None,
        email: Optional[EmailProvider] = None,
        tasks: Optional[Callable[[WorkItem], str]] = None,
        link_secret: bytes = b"",
        public_url: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if audit.tenant_id != store.tenant_id:
            raise ValueError("the audit log and the store belong to different tenants")
        if voices and len(call_secret) < 16:
            raise ValueError("bridging to a voice engine needs a call secret of 16+ bytes")
        self.spec = spec
        self.store = store
        self.audit = audit
        self.carriers = dict(carriers)
        self.voices = dict(voices or {})
        self._secret = call_secret
        #: Builds the read-only answerer for a call, or None when the agent has no
        #: knowledge to consult. Absent means calls run on their instructions alone.
        self._answers = answers
        #: Reads finished calls into their purpose's fields. None: transcripts wait.
        self.extractor = extractor
        #: Sends the follow-up emails calls earn. None: email actions wait.
        self.email = email
        #: Puts work on the runtime's board, returning its task id. None: work actions wait.
        self.tasks = tasks
        #: Signs the unsubscribe link in every email.
        self.link_secret = link_secret
        self.public_url = public_url or spec.public_url
        self._clock = clock

    def call_token(self, call_id: str) -> str:
        """Proof, carried in the SIP bridge, that a voice session belongs to this call.

        SIP headers are caller-controlled: anyone who can reach the voice engine's SIP
        address can claim any call id. Only someone holding the gateway's secret can
        produce the token that goes with it.
        """
        message = f"{self.store.tenant_id}:{call_id}".encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    # -- requests -----------------------------------------------------------------------

    def _facts(self, agent: str, contact_id: str, purpose: str) -> CallFacts:
        now = self._clock()
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return CallFacts(
            agent=agent,
            purpose=purpose,
            contact=self.store.contact(contact_id),
            consents=self.store.consents(contact_id),
            suppressed=self.store.is_suppressed(contact_id),
            active_calls=self.store.count_active(agent),
            calls_today=self.store.count_dialled_since(agent, _iso(midnight)),
            prior_contact=self.store.completed_before(agent, contact_id),
            now=now,
            budget=exhausted(self),
        )

    def _action_digest(self, agent: str, contact_id: str, purpose: str) -> str:
        """What an approver is approving, reduced to a hash. Empty when it cannot be resolved."""
        profile = self.spec.profile(agent)
        contact = self.store.contact(contact_id)
        if profile is None or contact is None:
            return ""
        action = {
            "agent": agent,
            "contact_id": contact_id,
            "to": contact.phone,
            "purpose": purpose,
            "telephony": profile.telephony,
            "caller_id": profile.caller_id,
            "max_seconds": profile.max_call_seconds,
        }
        canonical = json.dumps(action, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def request_call(
        self, *, agent: str, contact_id: str, purpose: str, idempotency_key: str, task_id: str = ""
    ) -> CallResult:
        if not idempotency_key:
            raise ValueError("an idempotency key is required — retries must not dial twice")
        decision = decide_call(self.spec, self._facts(agent, contact_id, purpose))
        status = {
            ALLOW: CallStatus.REQUESTED,
            DENY: CallStatus.POLICY_BLOCKED,
        }.get(decision.effect, CallStatus.APPROVAL_REQUIRED)
        profile = self.spec.profile(agent)
        record, created = self.store.create_call(
            idempotency_key=idempotency_key,
            agent=agent,
            contact_id=contact_id,
            purpose=purpose,
            task_id=task_id,
            telephony=profile.telephony if profile else "",
            action_digest=self._action_digest(agent, contact_id, purpose),
            status=status,
            reason=decision.reason,
            at=_iso(self._clock()),
        )
        if not created:
            return CallResult(record, None, created=False)

        correlation = new_correlation_id()
        self.audit.record(
            "communications.call.requested",
            correlation_id=correlation,
            subject=record.call_id,
            detail={
                "agent": agent,
                "contact_id": contact_id,
                "purpose": purpose,
                "task_id": task_id,
                "decision": decision.to_dict(),
            },
        )
        if decision.effect == ALLOW:
            record = self._dial(record, approver="", correlation=correlation)
        return CallResult(record, decision, created=True)

    def approve(self, call_id: str, *, approver: str) -> CallResult:
        """A human approved. Re-check everything — consent may have been withdrawn since."""
        if not approver:
            raise ValueError("an approval with no identity is not an approval")
        record = self._require(call_id)
        if record.status is not CallStatus.APPROVAL_REQUIRED:
            return CallResult(record, None, created=False)
        correlation = new_correlation_id()

        if self._action_digest(record.agent, record.contact_id, record.purpose) != record.action_digest:
            reason = "the call changed after it was requested; request it again to approve"
            self.store.advance(call_id, CallStatus.CANCELLED, at=_iso(self._clock()), reason=reason)
            self.audit.record(
                "communications.call.approval_refused",
                correlation_id=correlation, subject=call_id,
                detail={"approver": approver, "reason": reason},
            )
            return CallResult(self._require(call_id), None, created=False)

        decision = decide_call(self.spec, self._facts(record.agent, record.contact_id, record.purpose))
        self.audit.record(
            "communications.call.approved",
            correlation_id=correlation, subject=call_id,
            detail={"approver": approver, "decision": decision.to_dict()},
        )
        if decision.effect == DENY:
            self.store.advance(
                call_id, CallStatus.POLICY_BLOCKED, at=_iso(self._clock()),
                failure=FailureReason.POLICY_BLOCKED, reason=decision.reason,
            )
            return CallResult(self._require(call_id), decision, created=False)
        return CallResult(self._dial(record, approver=approver, correlation=correlation),
                          decision, created=False)

    def reject(self, call_id: str, *, approver: str, reason: str) -> CallRecord:
        if not approver:
            raise ValueError("a rejection with no identity is not a rejection")
        self.store.advance(call_id, CallStatus.CANCELLED, at=_iso(self._clock()),
                           reason=reason or "rejected")
        self.audit.record(
            "communications.call.rejected", correlation_id=new_correlation_id(),
            subject=call_id, detail={"approver": approver, "reason": reason},
        )
        return self._require(call_id)

    def now(self) -> datetime:
        return self._clock()

    def end_call(self, call_id: str, *, actor: str, reason: str = "") -> CallRecord:
        """An operator stops a call. Pending ones are cancelled; live ones are hung up at the
        carrier, whose final callback records how the call ended."""
        if not actor:
            raise ValueError("ending a call with no identity is not an action anyone took")
        record = self._require(call_id)
        correlation = new_correlation_id()
        reason = reason or f"ended by {actor}"
        if record.status.terminal:
            return record
        if record.status in (CallStatus.REQUESTED, CallStatus.APPROVAL_REQUIRED):
            self.store.advance(call_id, CallStatus.CANCELLED, at=_iso(self._clock()), reason=reason)
        else:
            carrier = self.carriers.get(record.telephony)
            if carrier is None or not record.provider_call_ref:
                raise ProviderError(
                    f"no {record.telephony!r} adapter or carrier reference to hang up with"
                )
            carrier.end_call(record.provider_call_ref)
        self.audit.record("communications.call.ended", correlation_id=correlation,
                          subject=call_id, detail={"actor": actor, "reason": reason,
                                                   "was": record.status.value})
        return self._require(call_id)

    def opt_out(self, contact_id: str, *, reason: str, source: str) -> None:
        """Suppress a contact. Takes effect for every agent, every channel and every later
        request at once, and cancels callbacks and emails still waiting to go out."""
        at = _iso(self._clock())
        self.store.suppress(contact_id, reason=reason, at=at)
        cancelled = self.store.cancel_outreach(contact_id, reason="the contact opted out", at=at)
        self.audit.record(
            "communications.contact.suppressed", correlation_id=new_correlation_id(),
            subject=contact_id, detail={"reason": reason, "source": source,
                                        "cancelled_actions": cancelled},
        )

    def _require(self, call_id: str) -> CallRecord:
        record = self.store.call(call_id)
        if record is None:
            raise KeyError(f"no call {call_id!r} in this tenant")
        return record

    def _dial(self, record: CallRecord, *, approver: str, correlation: str) -> CallRecord:
        now = _iso(self._clock())
        if not self.store.claim_for_dial(record.call_id, approved_by=approver, at=now):
            # Someone else claimed it first. Their dial is the only dial.
            return self._require(record.call_id)

        profile = self.spec.profile(record.agent)
        contact = self.store.contact(record.contact_id)
        carrier = self.carriers.get(record.telephony)
        try:
            if profile is None or contact is None:
                raise ProviderError("the profile or contact vanished before dialling")
            if carrier is None:
                raise ProviderError(f"no {record.telephony!r} adapter is configured")
            ref = carrier.place_call(
                PlaceCall(
                    call_id=record.call_id,
                    tenant_id=self.store.tenant_id,
                    to_number=contact.phone,
                    from_number=profile.caller_id,
                    max_seconds=profile.max_call_seconds,
                )
            )
        except ProviderError as exc:
            self.store.advance(record.call_id, CallStatus.FAILED, at=_iso(self._clock()),
                               failure=exc.reason, reason=str(exc))
            self.audit.record(
                "communications.call.failed", correlation_id=correlation,
                subject=record.call_id, detail={"failure": exc.reason.value}, error=str(exc),
            )
            return self._require(record.call_id)

        self.store.set_provider_ref(record.call_id, ref, at=_iso(self._clock()))
        self.audit.record(
            "communications.call.placed", correlation_id=correlation, subject=record.call_id,
            detail={"telephony": record.telephony, "approver": approver},
        )
        return self._require(record.call_id)

    # -- carrier callbacks --------------------------------------------------------------

    def _find(self, carrier: TelephonyProvider, event: ProviderEvent) -> Optional[CallRecord]:
        record = self.store.call_by_ref(carrier.id, event.provider_call_ref)
        if record is None and event.call_id_hint:
            record = self.store.call_for_carrier(carrier.id, event.call_id_hint, event.provider_call_ref)
        return record

    def _read(self, carrier_id: str, headers: Mapping[str, str], body: bytes, url: str):
        """Verify and parse a carrier request. ``(carrier, event, None)`` or ``(.., error)``."""
        carrier = self.carriers.get(carrier_id)
        if carrier is None:
            return None, None, IngestResult("rejected", f"no {carrier_id!r} adapter", http_status=404)
        if not carrier.verify(headers, body, url):
            self.audit.record(
                "communications.callback.unverified", correlation_id=new_correlation_id(),
                subject=carrier_id, detail={"bytes": len(body)},
            )
            return carrier, None, IngestResult("rejected", "signature did not verify", http_status=403)
        try:
            return carrier, carrier.parse(headers, body, url), None
        except ValueError as exc:
            return carrier, None, IngestResult("rejected", f"unreadable callback: {exc}", http_status=400)

    def ingest(
        self, carrier_id: str, *, headers: Mapping[str, str], body: bytes, url: str = ""
    ) -> IngestResult:
        carrier, event, error = self._read(carrier_id, headers, body, url)
        if error is not None:
            return error

        record = self._find(carrier, event)
        if record is None:
            return IngestResult("rejected", "no call in this tenant matches", http_status=400)

        if not self.store.record_event(
            provider=carrier.id, provider_event_id=event.provider_event_id,
            call_id=record.call_id, status=event.status, at=_iso(self._clock()),
        ):
            return IngestResult("ignored", "duplicate delivery", record.call_id)

        if not self.store.advance(record.call_id, event.status, at=_iso(self._clock()),
                                  failure=event.failure):
            return IngestResult("ignored", "would not move the call forward", record.call_id)
        self.store.mark_applied(carrier.id, event.provider_event_id)
        if event.detail.get("duration_seconds"):
            self.store.set_duration(record.call_id, int(event.detail["duration_seconds"]))
        self.audit.record(
            "communications.call.status", correlation_id=new_correlation_id(),
            subject=record.call_id,
            detail={"status": event.status.value,
                    "failure": event.failure.value if event.failure else ""},
        )
        return IngestResult("applied", call_id=record.call_id)

    def answer(
        self, carrier_id: str, *, headers: Mapping[str, str], body: bytes, url: str = ""
    ) -> AnswerResponse:
        """The carrier asks what to do with an answered call. Every doubt hangs up."""
        carrier, event, error = self._read(carrier_id, headers, body, url)
        if error is not None:
            return AnswerResponse(error.http_status, body=error.reason.encode("utf-8"))

        def hang_up() -> AnswerResponse:
            return AnswerResponse(200, *carrier.render_answer(AnswerPlan(hang_up=True)))

        record = self._find(carrier, event)
        if record is None:
            return hang_up()
        now = _iso(self._clock())
        self.store.record_event(
            provider=carrier.id, provider_event_id=event.provider_event_id,
            call_id=record.call_id, status=event.status, at=now,
        )
        correlation = new_correlation_id()

        if event.failure is FailureReason.MACHINE_DETECTED:
            self.store.advance(record.call_id, CallStatus.FAILED, at=now,
                               failure=FailureReason.MACHINE_DETECTED,
                               reason="answered by a machine; nothing was said")
            self.audit.record("communications.call.machine", correlation_id=correlation,
                              subject=record.call_id, detail=dict(event.detail))
            return hang_up()

        self.store.advance(record.call_id, CallStatus.ANSWERED, at=now)
        record = self.store.call(record.call_id)
        profile = self.spec.profile(record.agent)
        if record.status is not CallStatus.ANSWERED or profile is None:
            # Ended, cancelled, or already bridged. Never put a second engine on a line.
            return hang_up()

        if profile.recording.enabled:
            # Ask before anyone is on the line; the consent callback connects the engine.
            plan = AnswerPlan(say=self.spec.disclosure, language=profile.voice.language,
                              consent=ConsentPrompt(record.call_id, self.spec.consent_prompt))
            self.audit.record("communications.call.answered", correlation_id=correlation,
                              subject=record.call_id, detail={"asking_consent": True})
            return AnswerResponse(200, *carrier.render_answer(plan))

        plan = self.bridge_plan(record, profile, say=self.spec.disclosure)
        if plan is None:
            return hang_up()
        self.audit.record("communications.call.answered", correlation_id=correlation,
                          subject=record.call_id,
                          detail={"voice_engine": self.store.call(record.call_id).voice_engine})
        return AnswerResponse(200, *carrier.render_answer(plan))

    def bridge_plan(self, record: CallRecord, profile, *, say: str) -> Optional[AnswerPlan]:
        """Connect an answered call to its voice engine. None (and the call failed) when no
        engine this agent may use is configured."""
        now = _iso(self._clock())
        engine = record.voice_engine or self._next_engine(profile, after=None)
        if not engine:
            reason = "no voice engine this agent may use is configured"
            self.store.advance(record.call_id, CallStatus.FAILED, at=now,
                               failure=FailureReason.PROVIDER_FAILURE, reason=reason)
            self.audit.record("communications.call.failed", correlation_id=new_correlation_id(),
                              subject=record.call_id, error=reason)
            return None
        if not record.voice_engine:
            self.store.set_voice(record.call_id, engine=engine, at=now)
        return AnswerPlan(
            say=say,
            language=profile.voice.language,
            connect=self.voices[engine].bridge_target(record.call_id, self.call_token(record.call_id)),
        )

    def _healthy(self, engine: str) -> bool:
        since = _iso(self._clock() - ENGINE_WINDOW)
        return self.store.engine_failures_since(engine, since) < ENGINE_FAILURES_TO_SKIP

    def _next_engine(self, profile, *, after: Optional[str]) -> str:
        """The first configured engine in the agent's order, after ``after``. An engine that
        keeps failing is passed over while another is available."""
        order = list(profile.voice.ordered)
        if after is None:
            candidates = order
        elif after in order:
            candidates = order[order.index(after) + 1:]
        else:
            candidates = []
        configured = [e for e in candidates if e in self.voices]
        return next((e for e in configured if self._healthy(e)), configured[0] if configured else "")

    def engine_failed(self, call_id: str, engine: str, reason: str) -> bool:
        """A voice engine refused or dropped a live call. Move the call to the agent's next
        engine through the carrier, without hanging up; True when it moved. Otherwise end the
        phone leg, so the person is not left in silence. Each engine is tried once per call."""
        record = self.store.call(call_id)
        if record is None:
            return False
        now = _iso(self._clock())
        self.store.record_engine_failure(engine, call_id, reason, at=now)
        profile = self.spec.profile(record.agent)
        carrier = self.carriers.get(record.telephony)
        target = self._next_engine(profile, after=engine) if profile else ""
        correlation = new_correlation_id()
        if target and carrier is not None and record.provider_call_ref and \
                not record.status.terminal and self.store.switch_engine(
                    call_id, from_engine=engine, to_engine=target, at=now):
            try:
                carrier.redirect(record.provider_call_ref, AnswerPlan(
                    say=FAILOVER_LINE, language=profile.voice.language,
                    connect=self.voices[target].bridge_target(call_id, self.call_token(call_id))))
            except ProviderError as exc:
                reason = f"{reason}; failover to {target} failed: {exc}"
            else:
                self.audit.record("communications.call.failed_over", correlation_id=correlation,
                                  subject=call_id,
                                  detail={"from": engine, "to": target, "reason": reason[:300]})
                return True
        self.store.advance(call_id, CallStatus.FAILED, at=_iso(self._clock()),
                           failure=FailureReason.PROVIDER_FAILURE, reason=reason)
        self.audit.record("communications.call.failed", correlation_id=correlation,
                          subject=call_id, detail={"voice_engine": engine}, error=reason)
        self._end_quietly(record)
        return False

    # -- voice sessions -----------------------------------------------------------------

    def _proves(self, record: Optional[CallRecord], call_id: str, token: str, engine_id: str) -> bool:
        """Whether a claimed session is the one this call is waiting for."""
        return (
            record is not None
            and hmac.compare_digest(token, self.call_token(call_id))
            and record.status is CallStatus.ANSWERED
            and record.voice_engine == engine_id
        )

    def _session_request(self, record: CallRecord, *, delegation: bool) -> VoiceSessionRequest:
        profile = self.spec.profile(record.agent)
        purpose = self.spec.purposes.get(record.purpose)
        return VoiceSessionRequest(
            call_id=record.call_id,
            tenant_id=self.store.tenant_id,
            agent=record.agent,
            instructions=(purpose.instructions or purpose.description) if purpose else "",
            voice=profile.voice.voice if profile else "",
            language=profile.voice.language if profile else "",
            delegation=delegation,
        )

    def transcriber(self, record: CallRecord) -> Callable[[str, str], None]:
        """Writes a call's finished turns, redacted, for extraction after the call."""
        from nova.communications.extraction import redact

        def said(speaker: str, text: str) -> None:
            cleaned = redact(text).strip()
            if cleaned:
                self.store.append_turn(record.call_id, speaker, cleaned, at=_iso(self._clock()))

        return said

    def _answerer(self, record: CallRecord) -> Optional[Answerer]:
        return self._answers(record) if self._answers is not None else None

    def voice_event(self, engine_id: str, *, headers: Mapping[str, str], body: bytes) -> IngestResult:
        """A SIP voice engine announces a bridged call. Accept it only for the call it proves."""
        voice = self.voices.get(engine_id)
        if not isinstance(voice, SipVoiceEngine):
            return IngestResult("rejected", f"no {engine_id!r} SIP adapter", http_status=404)
        if not voice.verify(headers, body):
            self.audit.record(
                "communications.callback.unverified", correlation_id=new_correlation_id(),
                subject=engine_id, detail={"bytes": len(body)},
            )
            return IngestResult("rejected", "signature did not verify", http_status=403)
        try:
            incoming = voice.parse_incoming(headers, body)
        except ValueError as exc:
            return IngestResult("rejected", f"unreadable webhook: {exc}", http_status=400)
        if incoming is None:
            return IngestResult("ignored", "not an incoming session")

        call_id = incoming.headers.get("x-call-id", "")
        token = incoming.headers.get("x-call-token", "")
        record = self.store.call(call_id) if call_id else None
        correlation = new_correlation_id()
        if not self._proves(record, call_id, token, engine_id):
            voice.reject(incoming.session_id)
            self.audit.record(
                "communications.voice.rejected", correlation_id=correlation,
                subject=incoming.session_id, detail={"claimed_call": call_id},
            )
            return IngestResult("rejected", "the session does not prove a call awaiting it",
                                call_id, http_status=200)

        now = _iso(self._clock())
        if not self.store.record_event(
            provider=engine_id, provider_event_id=incoming.event_id, call_id=call_id,
            status=CallStatus.SESSION_CONNECTED, at=now,
        ):
            return IngestResult("ignored", "duplicate delivery", call_id)
        if not self.store.claim_session(call_id, engine=engine_id,
                                        session_id=incoming.session_id, at=now):
            voice.reject(incoming.session_id)
            return IngestResult("ignored", "the call already has a voice session", call_id)

        answer = self._answerer(record)
        try:
            voice.accept(incoming.session_id,
                         self._session_request(record, delegation=answer is not None))
        except ProviderError as exc:
            moved = self.engine_failed(call_id, engine_id, str(exc))
            return IngestResult("applied" if moved else "rejected",
                                "failed over" if moved else str(exc), call_id, http_status=200)

        self.store.advance(call_id, CallStatus.SESSION_CONNECTED, at=_iso(self._clock()))
        self.store.mark_applied(engine_id, incoming.event_id)
        # Always attached: the transcript is how a spoken opt-out is caught after the call.
        voice.attach(incoming.session_id, CallHooks(answer=answer, heard=guard_for(self, record),
                                                    said=self.transcriber(record)))
        self.audit.record("communications.call.session_connected", correlation_id=correlation,
                          subject=call_id,
                          detail={"voice_engine": engine_id, "knowledge": answer is not None})
        return IngestResult("applied", call_id=call_id)

    def stream_started(
        self, engine_id: str, *, call_id: str, token: str, stream_sid: str
    ) -> Optional[tuple[StreamVoiceEngine, VoiceSessionRequest, CallHooks]]:
        """The media bridge received a stream claiming a call. What to run it with, or None.

        Same proof as a SIP session: the token, an answered call, the engine chosen at
        answer time, and no session bound yet.
        """
        voice = self.voices.get(engine_id)
        record = self.store.call(call_id) if call_id else None
        correlation = new_correlation_id()
        if not isinstance(voice, StreamVoiceEngine) or not self._proves(
            record, call_id, token, engine_id
        ) or not self.store.claim_session(call_id, engine=engine_id, session_id=stream_sid,
                                          at=_iso(self._clock())):
            self.audit.record(
                "communications.voice.rejected", correlation_id=correlation,
                subject=stream_sid, detail={"claimed_call": call_id, "engine": engine_id},
            )
            return None

        self.store.advance(call_id, CallStatus.SESSION_CONNECTED, at=_iso(self._clock()))
        answer = self._answerer(record)
        hooks = CallHooks(
            answer=answer,
            opt_out=lambda said: self.opt_out(
                record.contact_id, reason="asked on the call not to be called again",
                source=f"call:{call_id}",
            ),
            end_call=lambda: self._end_quietly(self.store.call(call_id)),
            heard=guard_for(self, record),
            said=self.transcriber(record),
        )
        self.audit.record("communications.call.session_connected", correlation_id=correlation,
                          subject=call_id,
                          detail={"voice_engine": engine_id, "knowledge": answer is not None})
        return voice, self._session_request(record, delegation=answer is not None), hooks

    def _end_quietly(self, record: CallRecord) -> None:
        """Hang up the phone leg after the engine failed, so the person is not left in silence."""
        carrier = self.carriers.get(record.telephony)
        if carrier is None or not record.provider_call_ref:
            return
        try:
            carrier.end_call(record.provider_call_ref)
        except ProviderError as exc:
            self.audit.record("communications.call.end_failed", correlation_id=new_correlation_id(),
                              subject=record.call_id, error=str(exc))
