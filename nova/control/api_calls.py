"""The Control API's call monitor: what calls are doing, and the three things an operator
may do about one — approve it, reject it, or end it.

A sibling of :mod:`nova.control.api` rather than more of it. Reads come straight from the
runtime's call store. Writes go through the same :class:`CallGateway` the call listeners
use, so an approval from the dashboard is re-checked against consent and calling hours
exactly as one from the CLI is, and is audited under the person who clicked.

Every route here defaults to admin: a call list names customers, and approving a call is
an action in the world.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

_NO_PUBLIC_URL = "https://public-url-not-configured.invalid"


def _gateway(api, actor: str):
    from nova.communications.assemble import build_gateway
    from nova.communications.store import CommsStore

    spec = api.bundle.communications
    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    return build_gateway(
        spec, store=store, audit=api.audit.with_actor(actor), env=os.environ,
        public_url=spec.public_url or _NO_PUBLIC_URL,
        knowledge_index=api.runtime.knowledge_index_path,
        knowledge_grants={a.id: a.knowledge.sources for a in api.bundle.agents},
    )


def list_calls(api, query: Mapping[str, str]):
    from nova.communications.contracts import CallStatus
    from nova.control.api import Response
    from nova.control.api import _error as error
    from nova.communications.store import CommsStore

    status = str(query.get("status") or "")
    if status and status not in {s.value for s in CallStatus}:
        return error(400, "unknown status", given=status)
    try:
        limit = int(query.get("limit") or 50)
    except ValueError:
        return error(400, "limit must be a whole number")
    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        records = store.recent_calls(limit=limit, agent=str(query.get("agent") or ""),
                                     status=status)
        active = sum(1 for r in records if r.status.active)
    finally:
        store.close()
    return Response(200, {"calls": [r.to_dict() for r in records], "active": active})


def get_call(api, call_id: str):
    from nova.communications.store import CommsStore
    from nova.control.api import Response
    from nova.control.api import _error as error

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        record = store.call(call_id)
        recordings = store.recordings(call_id=call_id) if record is not None else []
        extraction = store.extraction(call_id) if record is not None else None
        plan = _plan(store, call_id) if record is not None else None
        emails = [{k: e[k] for k in _EMAIL_META} for e in store.emails(call_id=call_id)] \
            if record is not None else []
    finally:
        store.close()
    if record is None:
        return error(404, "no such call in this tenant", call_id=call_id)
    # Metadata only. The audio stays at the carrier behind its own credentials; a control
    # plane that proxied it would be one more place a recording can leak from.
    return Response(200, {**record.to_dict(), "recordings": [
        {k: r[k] for k in ("state", "duration_seconds", "started_at", "retention_until")}
        for r in recordings], "extraction": extraction, "plan": plan, "emails": emails})


#: What the call monitor shows of an email. Never the text: message bodies are more
#: sensitive than delivery state, and the text is the tenant's template anyway.
_EMAIL_META = ("email_id", "action_id", "subject", "status", "detail", "created_at",
               "updated_at")


def _plan(store, call_id: str):
    from nova.communications.actions import plan_view

    return plan_view(store, call_id)


def list_actions(api, query: Mapping[str, str]):
    """Plans with actions waiting for a person, one per call, each with its digest."""
    from nova.communications.store import CommsStore
    from nova.control.api import Response

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        waiting = store.actions(status="awaiting_approval")
        plans = [_plan(store, call_id) for call_id in dict.fromkeys(a["call_id"] for a in waiting)]
        return Response(200, {"plans": plans})
    finally:
        store.close()


def action_stats(api, query: Mapping[str, str]):
    """Per action kind, how often people approved or rejected it: the evidence an
    administrator needs before moving a kind to ``auto``. The system never moves it."""
    from nova.communications.store import CommsStore
    from nova.control.api import Response

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        tiers = {r.kind: r.tier for r in api.bundle.communications.actions.rules.values()}
        return Response(200, {"kinds": {kind: {"tier": tiers.get(kind, ""), **counts}
                                        for kind, counts in store.action_stats().items()}})
    finally:
        store.close()


def decide_plan(api, tail: str, principal, payload: Mapping[str, Any]):
    from nova.communications.actions import decide_plan as decide
    from nova.control.api import Response
    from nova.control.api import _error as error

    call_id = tail.strip("/").split("/")[2]
    approve, reject = payload.get("approve") or [], payload.get("reject") or []
    if not (isinstance(approve, list) and isinstance(reject, list)) or not (approve or reject):
        return error(400, "give approve and/or reject as lists of action ids")
    gateway = _gateway(api, principal.name)
    try:
        return Response(200, decide(gateway, call_id, digest=str(payload.get("digest") or ""),
                                    approve=[str(a) for a in approve],
                                    reject=[str(r) for r in reject], actor=principal.name))
    except KeyError:
        return error(404, "no such call in this tenant", call_id=call_id)
    except ValueError as exc:
        return error(409, str(exc), call_id=call_id)
    finally:
        gateway.store.close()


def _approve(gateway, call_id, principal, reason):
    from nova.communications.contracts import CallStatus

    result = gateway.approve(call_id, approver=principal.name)
    record, decision = result.record, result.decision
    dialled = record.status not in (CallStatus.APPROVAL_REQUIRED, CallStatus.POLICY_BLOCKED,
                                    CallStatus.CANCELLED)
    body = {**record.to_dict(), "actor": principal.name,
            "decision": decision.to_dict() if decision else None}
    # 409 when the world said no: the request was fine, the call was not callable.
    return (200 if dialled and decision is not None else 409), body


def _reject(gateway, call_id, principal, reason):
    record = gateway.reject(call_id, approver=principal.name, reason=reason)
    return 200, {**record.to_dict(), "actor": principal.name}


def _end(gateway, call_id, principal, reason):
    record = gateway.end_call(call_id, actor=principal.name, reason=reason)
    return 200, {**record.to_dict(), "actor": principal.name}


def _recording(paused: bool):
    def act(gateway, call_id, principal, reason):
        from nova.communications.recording import set_paused

        changed = set_paused(gateway, call_id, paused, actor=principal.name)
        return (200 if changed else 409), {"call_id": call_id, "changed": changed,
                                           "actor": principal.name}

    return act


#: The operator verbs, and nothing else. A dashboard cannot place a call; agents request
#: calls, people decide on them.
_ACTIONS = {
    "approve": _approve,
    "reject": _reject,
    "end": _end,
    "pause-recording": _recording(True),
    "resume-recording": _recording(False),
}
CALL_ACTIONS = tuple(_ACTIONS)


def decide_call(api, tail: str, principal, payload: Mapping[str, Any]):
    from nova.communications.contracts import ProviderError
    from nova.control.api import Response
    from nova.control.api import _error as error

    call_id = tail.strip("/").split("/")[2]
    action = str(payload.get("action") or "").strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return error(400, f"action must be one of {', '.join(CALL_ACTIONS)}", given=action or None)
    if action in ("approve", "end") and not api.bundle.communications.public_url:
        return error(409, "communications.public_url is not set, so the carrier could not "
                          "report back on this call; set it before approving or ending calls")

    gateway = _gateway(api, principal.name)
    try:
        if gateway.store.call(call_id) is None:
            return error(404, "no such call in this tenant", call_id=call_id)
        status, body = handler(gateway, call_id, principal, str(payload.get("reason") or "").strip())
        return Response(status, body)
    except ProviderError as exc:
        return error(502, f"the carrier could not act on the call: {exc}", call_id=call_id)
    finally:
        gateway.store.close()


def list_reviews(api, query: Mapping[str, str]):
    from nova.communications.store import CommsStore
    from nova.control.api import Response

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        status = str(query.get("status") or "pending")
        return Response(200, {"reviews": store.reviews(status=status)})
    finally:
        store.close()


def get_contact(api, contact_id: str):
    from nova.communications.store import CommsStore
    from nova.control.api import Response
    from nova.control.api import _error as error

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        contact = store.contact(contact_id)
        if contact is None:
            return error(404, "no such contact in this tenant", contact_id=contact_id)
        return Response(200, {
            "contact_id": contact.contact_id, "groups": list(contact.groups),
            "timezone": contact.timezone, "suppressed": store.is_suppressed(contact_id),
            "consents": sorted(store.consents(contact_id)),
            "fields": store.attributes(contact_id),
        })
    finally:
        store.close()


def decide_review(api, tail: str, principal, payload: Mapping[str, Any]):
    from nova.communications.extraction import decide_review as decide
    from nova.control.api import Response
    from nova.control.api import _error as error

    review_id = tail.strip("/").split("/")[2]
    action = str(payload.get("action") or "").strip()
    if action not in ("approve", "reject"):
        return error(400, "action must be approve or reject", given=action or None)
    gateway = _gateway(api, principal.name)
    try:
        return Response(200, decide(gateway, review_id, approve=action == "approve",
                                    actor=principal.name))
    except KeyError:
        return error(404, "no such review in this tenant", review_id=review_id)
    except ValueError as exc:
        return error(409, str(exc), review_id=review_id)
    finally:
        gateway.store.close()


_HISTORY_FILTERS = {"contact": "contact_id", "agent": "agent", "channel": "channel",
                    "status": "status", "task": "task_id", "since": "since", "until": "until"}


def history(api, query: Mapping[str, str]):
    """One timeline, metadata only: transcripts, email text and recordings stay behind the
    per-call and per-email routes."""
    from nova.communications.history import timeline
    from nova.communications.store import CommsStore
    from nova.control.api import Response
    from nova.control.api import _error as error

    filters = {name: str(query.get(key) or "") for key, name in _HISTORY_FILTERS.items()}
    try:
        limit = int(query.get("limit") or 200)
    except ValueError:
        return error(400, "limit must be a whole number")
    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        return Response(200, {"events": timeline(store, limit=limit, spec=api.bundle.communications,
                                                 **filters)})
    except ValueError as exc:
        return error(400, str(exc))
    finally:
        store.close()


def usage(api, query: Mapping[str, str]):
    """This month's minutes, emails and cost against the budget. No contact appears in it."""
    from nova.communications.usage import month_to_date
    from nova.control.api import Response

    gateway = _gateway(api, "control-api")
    try:
        return Response(200, month_to_date(gateway))
    finally:
        gateway.store.close()


def list_delegations(api, query: Mapping[str, str]):
    from nova.communications.store import CommsStore
    from nova.control.api import Response

    store = CommsStore(api.runtime.calls_store_path, tenant_id=api.bundle.tenant_id)
    try:
        return Response(200, {"delegations": store.delegations(
            status=str(query.get("status") or ""), agent=str(query.get("agent") or ""))})
    finally:
        store.close()
