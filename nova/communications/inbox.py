"""How an agent asks for a call: a signed request in a spool, decided by the gateway.

The agent's tool never dials and never holds a carrier credential. It writes a request —
which contact, which purpose, why — into the agent's own inbox directory, signed with a
key only that agent's profile holds. The gateway picks it up, checks the signature against
the agent the directory and the request both name, and runs it through
:meth:`CallGateway.request_call`: the same grant, consent, hours, caps and approval as a
call requested any other way. The result lands beside the request for the tool to read.

**Keys.** One master key per deployment, created on first use in the NOVA home with owner-
only permissions; each agent's key is an HMAC of the master and the agent id, so the
gateway can verify any agent's request without a registry and an agent's profile holds
only its own key. What separates agents is what separates their profiles today — the
operating-system boundary between them. An agent that can read another's profile can sign
as it; :mod:`nova.knowledge` grants rest on the same boundary, and the design note says so.

**Idempotent.** A request's id is its idempotency key, so a request picked up twice — two
gateway processes, a crash between dialling and moving the file — dials once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from nova.audit import new_correlation_id

MASTER_KEY_NAME = "call-request.key"
REQUESTS, RESULTS, DONE = "requests", "results", "done"
DELEGATIONS, REPORTS, EMAILS = "delegations", "reports", "emails"
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
#: Request files bigger than this are not requests.
MAX_REQUEST_BYTES = 8192


def master_key(home: Path) -> bytes:
    """This deployment's master key, created on first use, readable by its owner only."""
    path = Path(home) / "nova" / MASTER_KEY_NAME
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_hex(32).encode("ascii"))
        except BaseException:
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
    return path.read_text(encoding="ascii").strip().encode("ascii")


def agent_key(master: bytes, agent: str) -> str:
    return hmac.new(master, f"agent:{agent}".encode("utf-8"), hashlib.sha256).hexdigest()


def canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps({k: payload[k] for k in sorted(payload) if k != "signature"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(key: str, payload: Mapping[str, Any]) -> str:
    return hmac.new(key.encode("ascii"), canonical(payload), hashlib.sha256).hexdigest()


def calling_grant(communications, agent: str, *, inbox: Path, master: bytes) -> Optional[dict]:
    """What the agent's communications tools are configured with, or None when it may
    neither request calls nor take part in delegation.

    The purposes are named so the model can pick one; the contact groups are not — an
    agent names a contact it was given, and the gateway decides whether it may be called.
    """
    profile = communications.profile(agent)
    calls = profile is not None and profile.outbound
    routes = communications.delegation.routes
    targets = routes.get(agent, ())
    receives = any(agent in to for to in routes.values())
    email = communications.email.grant(agent)
    if not (calls or targets or receives or email):
        return None
    grant = {
        "version": 1,
        "agent_id": agent,
        "inbox": str(inbox),
        "key": agent_key(master, agent),
        "purposes": [
            {"id": purpose, "description": communications.purposes[purpose].description}
            for purpose in profile.purposes
        ] if calls else [],
    }
    if targets or receives:
        grant["delegation"] = {
            "may_delegate_to": list(targets),
            "receives": receives,
            "default_deadline_hours": communications.delegation.default_deadline_hours,
            "max_deadline_hours": communications.delegation.max_deadline_hours,
        }
    if email is not None:
        grant["email"] = {"purposes": [
            {"id": purpose, "description": communications.purposes[purpose].description}
            for purpose in email.purposes]}
    return grant


def calling_identity(communications, agent: str) -> Optional[dict]:
    """The part of an agent's calling grant that is its identity: who, and for what.
    The signing key and the inbox path are deployment, and stay out of the digest."""
    if communications is None:
        return None
    grant = calling_grant(communications, agent, inbox=Path("."), master=b"")
    if grant is None:
        return None
    return {k: v for k, v in grant.items() if k not in ("key", "inbox")}


def _write_result(directory: Path, request_id: str, body: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{request_id}.tmp"
    temporary.write_text(json.dumps(dict(body), sort_keys=True), encoding="utf-8")
    temporary.replace(directory / f"{request_id}.json")


def _reject(gateway, results: Path, request_id: str, agent: str, reason: str) -> dict:
    body = {"request_id": request_id, "status": "rejected", "reason": reason}
    if _ID.match(request_id):
        _write_result(results, request_id, body)
    gateway.audit.record("communications.agent_request.rejected",
                         correlation_id=new_correlation_id(), subject=agent,
                         detail={"request_id": request_id[:64], "reason": reason})
    return body


def _call(gateway, inbox: Path, agent: str, request_id: str, payload: Mapping[str, Any]) -> dict:
    result = gateway.request_call(
        agent=agent,
        contact_id=str(payload.get("contact_id") or ""),
        purpose=str(payload.get("purpose") or ""),
        idempotency_key=f"agent:{agent}:{request_id}",
        task_id=str(payload.get("task_id") or "")[:64],
    )
    body = _status(request_id, result.record)
    gateway.audit.record("communications.agent_request.decided", correlation_id=new_correlation_id(),
                         subject=result.record.call_id,
                         detail={"agent": agent, "request_id": request_id,
                                 "note": str(payload.get("note") or "")[:300],
                                 "status": body["status"]})
    return body


def _delegation(gateway, inbox: Path, agent: str, request_id: str,
                payload: Mapping[str, Any]) -> dict:
    from nova.communications.delegation import delegate

    expected = payload.get("expected") or []
    hours = payload.get("deadline_hours")
    return delegate(
        gateway, delegation_id=request_id, sender=agent,
        receiver=str(payload.get("to_agent") or ""), objective=str(payload.get("objective") or ""),
        expected=expected if isinstance(expected, list) else [],
        deadline_hours=hours if isinstance(hours, int) and not isinstance(hours, bool) else None,
        contact_id=str(payload.get("contact_id") or ""), context=str(payload.get("context") or ""),
        parent_task_id=str(payload.get("task_id") or "")[:64])


def _email(gateway, inbox: Path, agent: str, request_id: str,
           payload: Mapping[str, Any]) -> dict:
    from nova.communications.agent_email import request_email

    return request_email(gateway, agent=agent, request_id=request_id,
                         contact_id=str(payload.get("contact_id") or ""),
                         purpose=str(payload.get("purpose") or ""),
                         subject=str(payload.get("subject") or ""),
                         body=str(payload.get("body") or ""),
                         task_id=str(payload.get("task_id") or ""))


def _report(gateway, inbox: Path, agent: str, request_id: str,
            payload: Mapping[str, Any]) -> dict:
    from nova.communications.delegation import report

    delegation_id = str(payload.get("delegation_id") or "")
    answer = report(gateway, delegation_id=delegation_id, reporter=agent,
                    status=str(payload.get("status") or ""), result=payload.get("result"),
                    note=str(payload.get("note") or ""))
    # The delegator's own result file moves on at once, not at the next refresh.
    record = gateway.store.delegation(delegation_id)
    _write_result(inbox / RESULTS / record["from_agent"], delegation_id, answer)
    return {"request_id": request_id, "status": "accepted", "delegation": answer}


#: Spool directory -> what a verified request in it asks for. Each is decided exactly as the
#: same request from anywhere else would be.
SPOOLS: Mapping[str, Callable[..., dict]] = {
    REQUESTS: _call,
    DELEGATIONS: _delegation,
    REPORTS: _report,
    EMAILS: _email,
}


def process(gateway, inbox: Path, master: bytes) -> list[dict]:
    """Decide every waiting request in every spool. Returns what was written back."""
    out: list[dict] = []
    for spool, decide in SPOOLS.items():
        root = Path(inbox) / spool
        if root.is_dir():
            out.extend(_drain(gateway, Path(inbox), root, master, decide))
    return out


def _drain(gateway, inbox: Path, root: Path, master: bytes, decide) -> list[dict]:
    out: list[dict] = []
    for agent_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        agent = agent_dir.name
        results = inbox / RESULTS / agent
        for path in sorted(agent_dir.glob("*.json")):
            request_id = path.stem
            try:
                if path.stat().st_size > MAX_REQUEST_BYTES:
                    raise ValueError("too large")
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as exc:
                out.append(_reject(gateway, results, request_id, agent, f"unreadable: {exc}"))
                path.unlink(missing_ok=True)
                continue

            given = str(payload.get("signature") or "")
            if not hmac.compare_digest(given, sign(agent_key(master, agent), payload)):
                out.append(_reject(gateway, results, request_id, agent, "signature did not verify"))
            elif payload.get("agent") != agent or payload.get("request_id") != request_id \
                    or not _ID.match(request_id):
                out.append(_reject(gateway, results, request_id, agent,
                                   "the request does not match the inbox it was found in"))
            else:
                try:
                    body = decide(gateway, inbox, agent, request_id, payload)
                except ValueError as exc:
                    out.append(_reject(gateway, results, request_id, agent, str(exc)))
                else:
                    _write_result(results, request_id, body)
                    out.append(body)
            done = inbox / DONE / root.name / agent
            done.mkdir(parents=True, exist_ok=True)
            path.replace(done / path.name)
    return out


def _status(request_id: str, record) -> dict:
    return {"request_id": request_id, "call_id": record.call_id, "status": record.status.value,
            "reason": record.reason, "failure": record.failure}


def refresh(gateway, inbox: Path) -> int:
    """Bring every unfinished result up to date with its call, delegation or email."""
    from nova.communications.delegation import view

    changed = 0
    root = Path(inbox) / RESULTS
    if not root.is_dir():
        return 0
    for path in root.glob("*/*.json"):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if body.get("call_id"):
            record = gateway.store.call(str(body["call_id"]))
            fresh = _status(body["request_id"], record) if record else None
        elif body.get("delegation_id") and "to_agent" in body:
            record = gateway.store.delegation(str(body["delegation_id"]))
            fresh = view(record) if record else None
        elif "email_id" in body and body.get("status") in ("awaiting_approval", "sending", "new"):
            from nova.communications.agent_email import view as email_view

            request = gateway.store.email_request(str(body["request_id"]))
            fresh = email_view(request) if request else None
        else:
            continue
        if fresh is not None and fresh != body:
            _write_result(path.parent, body["request_id"], fresh)
            changed += 1
    return changed
