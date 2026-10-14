"""The calling tools, as installed into the runtime.

Copied verbatim into the profile of each agent with a calling grant, so it imports nothing
from NOVA: only the standard library. It never dials and holds no carrier credential.

``request_call`` writes a signed request into this agent's inbox; the call gateway picks it
up, checks the signature, and decides the call exactly as it decides any other — the
tenant's grant, the contact's consent and opt-outs, calling hours, caps, and approval.
``call_status`` reads the answer the gateway wrote back. An agent therefore learns
"waiting for approval", "blocked: no consent on record" or "completed" — never a phone
number, and never a way around the gate.

The same plugin carries delegation, when the tenant routes work through this agent:
``delegate_work`` hands work to an agent this one may delegate to, ``delegation_status``
reads where it stands, and ``report_delegation`` answers work delegated to this agent. The
gateway checks the route, depth, cycles, budget and deadline; the tool only asks.

The grant — which purposes this agent may call for, whom it may delegate to, where its
inbox is, and its signing key — is read from ``nova-calling.json`` beside the profile,
resolved from this file's own location so several agents on one host each read their own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILENAME = "nova-calling.json"
TOOLSET = "nova_calling"
REQUEST_TOOL = "request_call"
STATUS_TOOL = "call_status"
DELEGATION_TOOLSET = "nova_delegation"
DELEGATE_TOOL = "delegate_work"
DELEGATION_STATUS_TOOL = "delegation_status"
REPORT_TOOL = "report_delegation"
REPORT_STATUSES = ("completed", "needs_approval", "blocked", "failed")
EMAIL_TOOLSET = "nova_email"
EMAIL_TOOL = "send_email"
EMAIL_STATUS_TOOL = "email_status"
MEETING_TOOLSET = "nova_meetings"
MEETING_TOOL = "book_meeting"
SLOTS_TOOL = "meeting_slots"
MEETING_STATUS_TOOL = "meeting_status"

_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_CACHE: dict[str, Any] = {}


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / CONFIG_FILENAME


def _load_config() -> Optional[dict[str, Any]]:
    if "config" in _CACHE:
        return _CACHE["config"]
    config = None
    try:
        path = _config_path()
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("key") and (
                    loaded.get("purposes") or isinstance(loaded.get("delegation"), dict)
                    or isinstance(loaded.get("email"), dict)
                    or isinstance(loaded.get("meetings"), dict)):
                config = loaded
    except (OSError, ValueError):
        config = None
    _CACHE["config"] = config
    return config


def _sign(key: str, payload: dict[str, Any]) -> str:
    """Must match ``nova.communications.inbox.sign`` byte for byte; a test holds them together."""
    body = json.dumps({k: payload[k] for k in sorted(payload) if k != "signature"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("ascii"), body, hashlib.sha256).hexdigest()


def _purpose_ids(config: dict[str, Any]) -> list:
    return [str(p.get("id")) for p in config.get("purposes", []) if isinstance(p, dict) and p.get("id")]


def _reply(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def handle_request_call(args: dict[str, Any], **_kwargs: Any) -> str:
    """Queue one call request. Never raises into the agent loop."""
    config = _load_config()
    if not config:
        return _reply(status="unavailable", message="Calling is not configured for this agent.")
    contact_id = str(args.get("contact_id") or "").strip()
    purpose = str(args.get("purpose") or "").strip()
    if not contact_id or len(contact_id) > 128:
        return _reply(status="refused", message="contact_id is required: the CRM id of the contact.")
    if purpose not in _purpose_ids(config):
        return _reply(status="refused",
                      message=f"purpose must be one of: {', '.join(_purpose_ids(config))}")
    request_id, problem = _spool(config, "requests", {
        "contact_id": contact_id,
        "purpose": purpose,
        "note": str(args.get("note") or "")[:300],
    })
    if problem:
        return problem
    return _reply(
        status="queued", request_id=request_id,
        message=("Requested. The call gateway decides whether and when it is placed — it may "
                 "need a person's approval, or be refused for consent, opt-out or calling "
                 "hours. Check with call_status; do not tell anyone a call was made until it "
                 "says so."),
    )


def _spool(config: dict[str, Any], spool: str, fields: dict[str, Any]) -> tuple:
    """Sign and file one request. ``(request_id, None)``, or ``(None, reply)`` on failure."""
    request_id = uuid.uuid4().hex
    payload = {"agent": str(config["agent_id"]), "request_id": request_id,
               "task_id": str(os.environ.get("HERMES_KANBAN_TASK") or "")[:64], **fields}
    payload["signature"] = _sign(str(config["key"]), payload)
    directory = Path(str(config["inbox"])) / spool / str(config["agent_id"])
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{request_id}.tmp"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(directory / f"{request_id}.json")
    except OSError as exc:
        return None, _reply(status="unavailable", message=f"The request could not be queued: {exc}")
    return request_id, None


def _read_result(config: dict[str, Any], request_id: str) -> str:
    path = Path(str(config["inbox"])) / "results" / str(config["agent_id"]) / f"{request_id}.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _reply(status="queued", request_id=request_id,
                      message="Not yet picked up by the gateway.")
    except (OSError, ValueError):
        return _reply(status="unavailable", message="The result could not be read.")
    return json.dumps(result, sort_keys=True)


def handle_call_status(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not config:
        return _reply(status="unavailable", message="Calling is not configured for this agent.")
    request_id = str(args.get("request_id") or "").strip()
    if not _ID.match(request_id):
        return _reply(status="refused", message="request_id is the id request_call returned.")
    return _read_result(config, request_id)


def _delegation(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    grant = (config or {}).get("delegation")
    return grant if isinstance(grant, dict) else {}


def handle_delegate_work(args: dict[str, Any], **_kwargs: Any) -> str:
    """Hand work to another agent. Never raises into the agent loop."""
    config = _load_config()
    targets = [str(t) for t in _delegation(config).get("may_delegate_to") or []]
    if not targets:
        return _reply(status="unavailable", message="This agent may not delegate work.")
    to_agent = str(args.get("to_agent") or "").strip()
    if to_agent not in targets:
        return _reply(status="refused", message=f"to_agent must be one of: {', '.join(targets)}")
    objective = " ".join(str(args.get("objective") or "").split())
    if not objective:
        return _reply(status="refused", message="objective is required: what the work must achieve.")
    expected = args.get("expected_fields") or []
    hours = args.get("deadline_hours")
    request_id, problem = _spool(config, "delegations", {
        "to_agent": to_agent,
        "objective": objective[:2000],
        "expected": [str(f) for f in expected][:20] if isinstance(expected, list) else [],
        "deadline_hours": hours if isinstance(hours, int) and not isinstance(hours, bool) else None,
        "contact_id": str(args.get("contact_id") or "")[:128],
        "context": str(args.get("context") or "")[:4000],
    })
    if problem:
        return problem
    return _reply(status="queued", delegation_id=request_id,
                  message=("Requested. The gateway checks the route, the chain and the "
                           "receiver's workload before it reaches their board. Check with "
                           "delegation_status; do not report the work as done until it says "
                           "completed."))


def handle_delegation_status(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not _delegation(config).get("may_delegate_to"):
        return _reply(status="unavailable", message="This agent may not delegate work.")
    delegation_id = str(args.get("delegation_id") or "").strip()
    if not _ID.match(delegation_id):
        return _reply(status="refused", message="delegation_id is the id delegate_work returned.")
    return _read_result(config, delegation_id)


def handle_report_delegation(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not _delegation(config).get("receives"):
        return _reply(status="unavailable", message="No work is delegated to this agent.")
    delegation_id = str(args.get("delegation_id") or "").strip()
    status = str(args.get("status") or "").strip()
    if not _ID.match(delegation_id) or status not in REPORT_STATUSES:
        return _reply(status="refused",
                      message=f"give the delegation_id from your task and a status: "
                              f"{', '.join(REPORT_STATUSES)}")
    result = args.get("result")
    if result is not None and not isinstance(result, (dict, str)):
        return _reply(status="refused", message="result is an object, or a sentence.")
    request_id, problem = _spool(config, "reports", {
        "delegation_id": delegation_id, "status": status, "result": result,
        "note": str(args.get("note") or "")[:500],
    })
    if problem:
        return problem
    return _reply(status="queued", request_id=request_id,
                  message="Filed. The gateway checks it is yours to report and that a completed "
                          "result has every field asked for.")


def _schemas(config: dict[str, Any]) -> tuple:
    purposes = "\n".join(
        f"  - {p.get('id')}: {p.get('description') or ''}".rstrip(": ")
        for p in config.get("purposes", []) if isinstance(p, dict)
    )
    request = {
        "name": REQUEST_TOOL,
        "description": (
            "Ask for a phone call to a contact, for one of your approved purposes. This does "
            "not dial: the call gateway checks consent, opt-outs, calling hours and limits, "
            "may require a person's approval, and places the call if everything allows it. "
            "Name the contact by its CRM id; never a phone number.\n\nYour purposes:\n" + purposes
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "The contact's CRM id."},
                "purpose": {"type": "string", "enum": _purpose_ids(config)},
                "note": {"type": "string", "description": "Why this call, in one sentence."},
            },
            "required": ["contact_id", "purpose"],
        },
    }
    status = {
        "name": STATUS_TOOL,
        "description": "Where a call you requested stands: queued, waiting for approval, "
                       "refused (with the reason), ringing, connected, completed or failed.",
        "parameters": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
        },
    }
    return request, status


def _email_purposes(config: Optional[dict[str, Any]]) -> list:
    grant = (config or {}).get("email")
    if not isinstance(grant, dict):
        return []
    return [str(p.get("id")) for p in grant.get("purposes", []) if isinstance(p, dict) and p.get("id")]


def handle_send_email(args: dict[str, Any], **_kwargs: Any) -> str:
    """Ask for an email to a contact. Never raises into the agent loop."""
    config = _load_config()
    purposes = _email_purposes(config)
    if not purposes:
        return _reply(status="unavailable", message="This agent may not send email.")
    contact_id = str(args.get("contact_id") or "").strip()
    purpose = str(args.get("purpose") or "").strip()
    subject = " ".join(str(args.get("subject") or "").split())
    body = str(args.get("body") or "").strip()
    if not contact_id or len(contact_id) > 128:
        return _reply(status="refused", message="contact_id is required: the CRM id of the contact.")
    if purpose not in purposes:
        return _reply(status="refused", message=f"purpose must be one of: {', '.join(purposes)}")
    if not (subject and body):
        return _reply(status="refused", message="subject and body are required.")
    request_id, problem = _spool(config, "emails", {
        "contact_id": contact_id, "purpose": purpose, "subject": subject[:200],
        "body": body[:10000]})
    if problem:
        return problem
    return _reply(status="queued", request_id=request_id,
                  message=("Requested. The gateway sends it to the address on file only if the "
                           "contact may be emailed, and a person may need to approve it first. "
                           "Check with email_status; do not say it was sent until it says so."))


def handle_email_status(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not _email_purposes(config):
        return _reply(status="unavailable", message="This agent may not send email.")
    request_id = str(args.get("request_id") or "").strip()
    if not _ID.match(request_id):
        return _reply(status="refused", message="request_id is the id send_email returned.")
    return _read_result(config, request_id)


def _email_schemas(config: dict[str, Any]) -> tuple:
    send = {
        "name": EMAIL_TOOL,
        "description": (
            "Ask for an email to a contact, for one of your approved purposes. Name the contact "
            "by its CRM id; it goes to the address on file, never to one you supply. The "
            "gateway checks opt-outs, consent and limits, and a person may approve it first. "
            "Plain text only; an unsubscribe line is added for you."),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "The contact's CRM id."},
                "purpose": {"type": "string", "enum": _email_purposes(config)},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message, plain text."},
            },
            "required": ["contact_id", "purpose", "subject", "body"],
        },
    }
    status = {
        "name": EMAIL_STATUS_TOOL,
        "description": "Where an email you asked for stands: waiting for approval, sent, "
                       "blocked (with the reason), rejected or failed.",
        "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}},
                       "required": ["request_id"]},
    }
    return send, status


def _meeting_grant(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    grant = (config or {}).get("meetings")
    return grant if isinstance(grant, dict) else {}


def _meeting_types(grant: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(e["id"]): e for e in grant.get("event_types", [])
            if isinstance(e, dict) and e.get("id")}


def handle_book_meeting(args: dict[str, Any], **_kwargs: Any) -> str:
    """Ask for a meeting with a contact. Never raises into the agent loop."""
    config = _load_config()
    grant = _meeting_grant(config)
    types = _meeting_types(grant)
    if not types:
        return _reply(status="unavailable", message="This agent may not book meetings.")
    contact_id = str(args.get("contact_id") or "").strip()
    purpose = str(args.get("purpose") or "").strip()
    event_type = str(args.get("event_type") or "").strip()
    start = str(args.get("start") or "").strip()
    purposes = [str(p.get("id")) for p in grant.get("purposes", []) if isinstance(p, dict)]
    if not contact_id or len(contact_id) > 128:
        return _reply(status="refused", message="contact_id is required: the CRM id of the contact.")
    if purpose not in purposes:
        return _reply(status="refused", message=f"purpose must be one of: {', '.join(purposes)}")
    if event_type not in types:
        return _reply(status="refused", message=f"event_type must be one of: {', '.join(types)}")
    if not start or len(start) > 40:
        return _reply(status="refused", message="start is required, e.g. 2026-10-13T10:30:00-05:00.")
    request_id, problem = _spool(config, "meetings", {
        "contact_id": contact_id, "purpose": purpose, "event_type": event_type, "start": start})
    if problem:
        return problem
    return _reply(status="queued", request_id=request_id,
                  message=("Requested. The gateway books it at the calendar only if the contact "
                           "may be invited, and a person may need to approve it first. Check "
                           "with meeting_status; do not say it is booked until it says so. If "
                           "the time is taken, the status lists open times to try."))


def handle_meeting_slots(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    types = _meeting_types(_meeting_grant(config))
    if not types:
        return _reply(status="unavailable", message="This agent may not book meetings.")
    contact_id = str(args.get("contact_id") or "").strip()
    event_type = str(args.get("event_type") or "").strip()
    day = str(args.get("day") or "").strip()
    if not contact_id or len(contact_id) > 128:
        return _reply(status="refused", message="contact_id is required: the CRM id of the contact.")
    if event_type not in types:
        return _reply(status="refused", message=f"event_type must be one of: {', '.join(types)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return _reply(status="refused", message="day must be YYYY-MM-DD, in the contact's day.")
    request_id, problem = _spool(config, "slots", {
        "contact_id": contact_id, "event_type": event_type, "day": day})
    if problem:
        return problem
    return _reply(status="queued", request_id=request_id,
                  message="Asked. Read the open times with meeting_status and this request_id.")


def handle_meeting_status(args: dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not _meeting_types(_meeting_grant(config)):
        return _reply(status="unavailable", message="This agent may not book meetings.")
    request_id = str(args.get("request_id") or "").strip()
    if not _ID.match(request_id):
        return _reply(status="refused",
                      message="request_id is the id book_meeting or meeting_slots returned.")
    return _read_result(config, request_id)


def _meeting_schemas(grant: dict[str, Any]) -> tuple:
    types = _meeting_types(grant)
    kinds = "; ".join(f"{e['id']}: {e.get('title', '')} ({e.get('duration_minutes', '?')} min)"
                      for e in types.values())
    purposes = [str(p.get("id")) for p in grant.get("purposes", []) if isinstance(p, dict)]
    book = {
        "name": MEETING_TOOL,
        "description": (
            "Ask for a meeting with a contact, for one of your approved purposes. Name the "
            "contact by its CRM id; the invitation goes to the address on file, never to one you "
            "supply. The gateway checks opt-outs, consent, notice and limits, and a person may "
            f"approve it first. Event types: {kinds}."),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "The contact's CRM id."},
                "purpose": {"type": "string", "enum": purposes},
                "event_type": {"type": "string", "enum": list(types)},
                "start": {"type": "string", "description": (
                    "ISO 8601, e.g. 2026-10-13T10:30:00-05:00. Without an offset it is read in "
                    "the contact's time zone.")},
            },
            "required": ["contact_id", "purpose", "event_type", "start"],
        },
    }
    slots = {
        "name": SLOTS_TOOL,
        "description": "Ask which times are open on one day for an event type. Read the answer "
                       "with meeting_status.",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "event_type": {"type": "string", "enum": list(types)},
                "day": {"type": "string", "description": "YYYY-MM-DD, the contact's day."},
            },
            "required": ["contact_id", "event_type", "day"],
        },
    }
    status = {
        "name": MEETING_STATUS_TOOL,
        "description": "Where a meeting you asked for stands (waiting for approval, booked, "
                       "blocked with the reason, rejected, failed with open times, cancelled), "
                       "or the open times meeting_slots asked for.",
        "parameters": {"type": "object", "properties": {"request_id": {"type": "string"}},
                       "required": ["request_id"]},
    }
    return book, slots, status


def _delegation_schemas(grant: dict[str, Any]) -> tuple:
    targets = [str(t) for t in grant.get("may_delegate_to") or []]
    delegate = {
        "name": DELEGATE_TOOL,
        "description": (
            "Hand a piece of work to another agent and get a structured result back. Say "
            "what must be achieved, which result fields you need, and optionally the contact "
            "it concerns. The gateway may refuse it: a chain too deep, a loop back to an "
            "agent already involved, or a receiver with too much open work."),
        "parameters": {
            "type": "object",
            "properties": {
                "to_agent": {"type": "string", "enum": targets},
                "objective": {"type": "string", "description": "What the work must achieve."},
                "expected_fields": {"type": "array", "items": {"type": "string"},
                                    "description": "Result fields you need back, lowercase."},
                "deadline_hours": {"type": "integer", "minimum": 1,
                                   "maximum": int(grant.get("max_deadline_hours") or 168)},
                "contact_id": {"type": "string", "description": "The CRM id, if it concerns one."},
                "context": {"type": "string", "description": "What the receiver needs to know."},
            },
            "required": ["to_agent", "objective"],
        },
    }
    status = {
        "name": DELEGATION_STATUS_TOOL,
        "description": "Where work you delegated stands, and its result once reported.",
        "parameters": {"type": "object",
                       "properties": {"delegation_id": {"type": "string"}},
                       "required": ["delegation_id"]},
    }
    report = {
        "name": REPORT_TOOL,
        "description": (
            "Answer work delegated to you: completed (with every field asked for), "
            "needs_approval, blocked or failed. The delegation id is in your task."),
        "parameters": {
            "type": "object",
            "properties": {
                "delegation_id": {"type": "string"},
                "status": {"type": "string", "enum": list(REPORT_STATUSES)},
                "result": {"type": "object", "description": "The fields asked for."},
                "note": {"type": "string", "description": "Why, when not completed."},
            },
            "required": ["delegation_id", "status"],
        },
    }
    return delegate, status, report


def register(ctx: Any) -> None:
    """Register the tools this agent's grant allows. Called once by the plugin loader."""
    config = _load_config()
    if not config:
        # No grant, no tools: an agent that may not call should not see a calling tool.
        return
    if config.get("purposes"):
        request, status = _schemas(config)
        ctx.register_tool(name=REQUEST_TOOL, toolset=TOOLSET, schema=request,
                          handler=handle_request_call,
                          description="Request a phone call; the gateway decides.", emoji="📞")
        ctx.register_tool(name=STATUS_TOOL, toolset=TOOLSET, schema=status,
                          handler=handle_call_status,
                          description="See where a requested call stands.", emoji="📞")
    if _email_purposes(config):
        send, status = _email_schemas(config)
        ctx.register_tool(name=EMAIL_TOOL, toolset=EMAIL_TOOLSET, schema=send,
                          handler=handle_send_email,
                          description="Ask for an email to a contact; the gateway decides.",
                          emoji="✉️")
        ctx.register_tool(name=EMAIL_STATUS_TOOL, toolset=EMAIL_TOOLSET, schema=status,
                          handler=handle_email_status,
                          description="See where a requested email stands.", emoji="✉️")
    meeting_grant = _meeting_grant(config)
    if _meeting_types(meeting_grant):
        book, slots, meeting_status = _meeting_schemas(meeting_grant)
        ctx.register_tool(name=MEETING_TOOL, toolset=MEETING_TOOLSET, schema=book,
                          handler=handle_book_meeting,
                          description="Ask for a meeting with a contact; the gateway decides.",
                          emoji="📅")
        ctx.register_tool(name=SLOTS_TOOL, toolset=MEETING_TOOLSET, schema=slots,
                          handler=handle_meeting_slots,
                          description="Ask which times are open for a meeting.", emoji="📅")
        ctx.register_tool(name=MEETING_STATUS_TOOL, toolset=MEETING_TOOLSET, schema=meeting_status,
                          handler=handle_meeting_status,
                          description="See where a requested meeting stands.", emoji="📅")
    grant = _delegation(config)
    if not grant:
        return
    delegate, status, report = _delegation_schemas(grant)
    if grant.get("may_delegate_to"):
        ctx.register_tool(name=DELEGATE_TOOL, toolset=DELEGATION_TOOLSET, schema=delegate,
                          handler=handle_delegate_work,
                          description="Hand work to another agent.", emoji="🤝")
        ctx.register_tool(name=DELEGATION_STATUS_TOOL, toolset=DELEGATION_TOOLSET, schema=status,
                          handler=handle_delegation_status,
                          description="See where delegated work stands.", emoji="🤝")
    if grant.get("receives"):
        ctx.register_tool(name=REPORT_TOOL, toolset=DELEGATION_TOOLSET, schema=report,
                          handler=handle_report_delegation,
                          description="Report on work delegated to you.", emoji="🤝")
