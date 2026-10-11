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

#: The operator verbs, and nothing else. A dashboard cannot place a call; agents request
#: calls, people decide on them.
CALL_ACTIONS = ("approve", "reject", "end")

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
    finally:
        store.close()
    if record is None:
        return error(404, "no such call in this tenant", call_id=call_id)
    return Response(200, record.to_dict())


def decide_call(api, tail: str, principal, payload: Mapping[str, Any]):
    from nova.communications.contracts import CallStatus, ProviderError
    from nova.control.api import Response
    from nova.control.api import _error as error

    call_id = tail.strip("/").split("/")[2]
    action = str(payload.get("action") or "").strip()
    if action not in CALL_ACTIONS:
        return error(400, f"action must be one of {', '.join(CALL_ACTIONS)}", given=action or None)
    reason = str(payload.get("reason") or "").strip()
    if action != "reject" and not api.bundle.communications.public_url:
        return error(409, "communications.public_url is not set, so the carrier could not "
                          "report back on this call; set it before approving or ending calls")

    gateway = _gateway(api, principal.name)
    try:
        if gateway.store.call(call_id) is None:
            return error(404, "no such call in this tenant", call_id=call_id)
        if action == "approve":
            result = gateway.approve(call_id, approver=principal.name)
            record, decision = result.record, result.decision
            dialled = record.status not in (CallStatus.APPROVAL_REQUIRED, CallStatus.POLICY_BLOCKED,
                                            CallStatus.CANCELLED)
            body = {**record.to_dict(), "actor": principal.name,
                    "decision": decision.to_dict() if decision else None}
            # 409 when the world said no: the request was fine, the call was not callable.
            return Response(200 if dialled and decision is not None else 409, body)
        if action == "reject":
            record = gateway.reject(call_id, approver=principal.name, reason=reason)
        else:
            record = gateway.end_call(call_id, actor=principal.name, reason=reason)
        return Response(200, {**record.to_dict(), "actor": principal.name})
    except ProviderError as exc:
        return error(502, f"the carrier could not act on the call: {exc}", call_id=call_id)
    finally:
        gateway.store.close()
