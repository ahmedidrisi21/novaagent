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
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from nova.audit import AuditLog, new_correlation_id
from nova.communications.contracts import (
    CallStatus,
    FailureReason,
    PlaceCall,
    ProviderError,
    TelephonyProvider,
)
from nova.communications.gate import (
    ALLOW,
    DENY,
    CallDecision,
    CallFacts,
    decide_call,
)
from nova.communications.spec import CommunicationsSpec
from nova.communications.store import CallRecord, CommsStore


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


class CallGateway:
    def __init__(
        self,
        spec: CommunicationsSpec,
        store: CommsStore,
        audit: AuditLog,
        carriers: Mapping[str, TelephonyProvider],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if audit.tenant_id != store.tenant_id:
            raise ValueError("the audit log and the store belong to different tenants")
        self.spec = spec
        self.store = store
        self.audit = audit
        self.carriers = dict(carriers)
        self._clock = clock

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

    def opt_out(self, contact_id: str, *, reason: str, source: str) -> None:
        """Suppress a contact. Takes effect for every agent and every later request at once."""
        self.store.suppress(contact_id, reason=reason, at=_iso(self._clock()))
        self.audit.record(
            "communications.contact.suppressed", correlation_id=new_correlation_id(),
            subject=contact_id, detail={"reason": reason, "source": source},
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

    def ingest(
        self, carrier_id: str, *, headers: Mapping[str, str], body: bytes, url: str = ""
    ) -> IngestResult:
        carrier = self.carriers.get(carrier_id)
        if carrier is None:
            return IngestResult("rejected", f"no {carrier_id!r} adapter is configured")
        if not carrier.verify(headers, body, url):
            self.audit.record(
                "communications.callback.unverified", correlation_id=new_correlation_id(),
                subject=carrier_id, detail={"bytes": len(body)},
            )
            return IngestResult("rejected", "signature did not verify")
        try:
            event = carrier.normalize(json.loads(body.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            return IngestResult("rejected", f"unreadable callback: {exc}")

        record = self.store.call_by_ref(carrier.id, event.provider_call_ref)
        if record is None:
            return IngestResult("rejected", "no call in this tenant has that carrier reference")

        if not self.store.record_event(
            provider=carrier.id, provider_event_id=event.provider_event_id,
            call_id=record.call_id, status=event.status, at=_iso(self._clock()),
        ):
            return IngestResult("ignored", "duplicate delivery", record.call_id)

        if not self.store.advance(record.call_id, event.status, at=_iso(self._clock()),
                                  failure=event.failure):
            return IngestResult("ignored", "would not move the call forward", record.call_id)
        self.store.mark_applied(carrier.id, event.provider_event_id)
        self.audit.record(
            "communications.call.status", correlation_id=new_correlation_id(),
            subject=record.call_id,
            detail={"status": event.status.value,
                    "failure": event.failure.value if event.failure else ""},
        )
        return IngestResult("applied", call_id=record.call_id)
