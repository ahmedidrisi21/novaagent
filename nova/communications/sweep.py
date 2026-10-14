"""Reconciliation: calls whose callbacks never came, and approvals nobody gave.

Carriers deliver callbacks at-least-once *when they can*; a crash, a deploy, or a dropped
webhook leaves a call the store thinks is still ringing. That matters beyond tidiness:
an active call holds one of its agent's concurrency slots, so a handful of lost callbacks
quietly stops an agent calling anyone.

The sweep asks the carrier — it never guesses a call ended because time passed. The two
exceptions are states no carrier knows about:

* **requested** past ``STRANDED_AFTER``: the gateway dials synchronously, so a request still
  waiting means the process died between the decision and the dial. Nothing was dialled.
* **initiated with no carrier reference** past ``STRANDED_AFTER``: the carrier never
  answered the dial request *and* sent no callback carrying our call id. Marked failed with
  a reason that says exactly that, so a human can check the carrier console if it matters.

Recordings past their retention are deleted at the carrier. Finished calls are read into
fields and an action plan, due actions are carried out, email text past its keep date is
deleted, and delegations are put on the board or expired at their deadline.

Approvals expire after ``APPROVAL_TTL``: an approval given tomorrow for a call proposed
today is approving a context that no longer exists.

Every change is audited as ``communications.call.reconciled``. The sweep is idempotent and
safe to run from several processes; each change goes through the store's forward-only
transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from nova.audit import new_correlation_id
from nova.communications.contracts import ACTIVE, CallStatus, FailureReason, ProviderError
from nova.communications.gateway import CallGateway
from nova.communications.actions import run_due
from nova.communications.agent_email import resume_sending
from nova.communications.meetings import cancel_for_opted_out, resume_sending as resume_meetings
from nova.communications.replies import expire as reply_expiry
from nova.communications.delegation import sweep_delegations
from nova.communications.extraction import run_pending
from nova.communications.insights import deliver_weekly
from nova.communications.followup_email import expire_bodies
from nova.communications.recording import expire
from nova.communications.store import CallRecord
from nova.communications.usage import raise_alerts, reconcile_billing

STRANDED_AFTER = timedelta(minutes=10)
APPROVAL_TTL = timedelta(hours=24)
#: Beyond a call's own time limit before the carrier is asked about it.
GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class Reconciled:
    call_id: str
    before: str
    after: str
    note: str

    def to_dict(self) -> dict:
        return {"call_id": self.call_id, "before": self.before, "after": self.after,
                "note": self.note}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sweep(gateway: CallGateway) -> list[Reconciled]:
    now = gateway.now()
    store, out = gateway.store, []

    def settle(record: CallRecord, status: CallStatus, note: str,
               failure: FailureReason | None = None) -> None:
        if store.advance(record.call_id, status, at=_iso(now), failure=failure, reason=note):
            out.append(Reconciled(record.call_id, record.status.value, status.value, note))
            gateway.audit.record(
                "communications.call.reconciled", correlation_id=new_correlation_id(),
                subject=record.call_id,
                detail={"before": record.status.value, "after": status.value, "note": note},
            )

    for record in store.calls_with_status([CallStatus.APPROVAL_REQUIRED],
                                          updated_before=_iso(now - APPROVAL_TTL)):
        settle(record, CallStatus.CANCELLED, "approval expired: nobody decided within 24 hours")

    stranded = _iso(now - STRANDED_AFTER)
    for record in store.calls_with_status([CallStatus.REQUESTED], updated_before=stranded):
        settle(record, CallStatus.FAILED, "interrupted before dialling; nothing was dialled",
               FailureReason.PROVIDER_FAILURE)

    for record in store.calls_with_status(ACTIVE, updated_before=_iso(now - GRACE)):
        if not record.provider_call_ref:
            if record.updated_at < stranded:
                settle(record, CallStatus.FAILED,
                       "the carrier never confirmed this call and sent no callback for it",
                       FailureReason.PROVIDER_FAILURE)
            continue
        profile = gateway.spec.profile(record.agent)
        limit = timedelta(seconds=profile.max_call_seconds if profile else 3600) + GRACE
        if record.updated_at >= _iso(now - limit) and record.status is not CallStatus.INITIATED:
            continue  # a live call within its own time limit is not stuck
        carrier = gateway.carriers.get(record.telephony)
        if carrier is None:
            continue
        try:
            event = carrier.fetch_status(record.provider_call_ref)
        except ProviderError as exc:
            out.append(Reconciled(record.call_id, record.status.value, record.status.value,
                                  f"carrier unreachable, left as is: {exc}"))
            continue
        if event is None:
            settle(record, CallStatus.FAILED, "the carrier has no record of this call",
                   FailureReason.PROVIDER_FAILURE)
            continue
        if store.record_event(provider=carrier.id, provider_event_id=event.provider_event_id,
                              call_id=record.call_id, status=event.status, at=_iso(now)):
            settle(record, event.status, f"carrier reports {event.status.value}", event.failure)

    extracted = run_pending(gateway, gateway.extractor)
    if extracted:
        out.append(Reconciled("", "", "", f"extracted {extracted} finished call(s)"))

    carried = run_due(gateway)
    if carried:
        out.append(Reconciled("", "", "", f"carried out {carried} post-call action(s)"))
    expire_bodies(gateway)
    reply_expiry(gateway)
    resent = resume_sending(gateway)
    if resent:
        out.append(Reconciled("", "", "", f"resumed {resent} interrupted agent email(s)"))
    settled = resume_meetings(gateway)
    if settled:
        out.append(Reconciled("", "", "", f"settled {settled} meeting(s) left mid-booking"))
    dropped = cancel_for_opted_out(gateway)
    if dropped:
        out.append(Reconciled("", "", "", f"cancelled {dropped} meeting(s) for opted-out contacts"))
    delegated = sweep_delegations(gateway)
    if delegated:
        out.append(Reconciled("", "", "", f"assigned or expired {delegated} delegation(s)"))

    billed = reconcile_billing(gateway)
    if billed:
        out.append(Reconciled("", "", "", f"recorded the carrier's bill for {billed} call(s)"))
    for percent in raise_alerts(gateway):
        out.append(Reconciled("", "", "", f"communications budget passed {percent}%"))

    period = deliver_weekly(gateway)
    if period:
        out.append(Reconciled("", "", "", f"sent the weekly digest for {period}"))

    expired = expire(gateway)
    if expired:
        out.append(Reconciled("", "", "", f"deleted {expired} recording(s) past retention"))
    return out
