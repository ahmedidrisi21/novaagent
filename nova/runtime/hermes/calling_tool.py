"""The calling tools, as installed into the runtime.

Copied verbatim into the profile of each agent with a calling grant, so it imports nothing
from NOVA: only the standard library. It never dials and holds no carrier credential.

``request_call`` writes a signed request into this agent's inbox; the call gateway picks it
up, checks the signature, and decides the call exactly as it decides any other — the
tenant's grant, the contact's consent and opt-outs, calling hours, caps, and approval.
``call_status`` reads the answer the gateway wrote back. An agent therefore learns
"waiting for approval", "blocked: no consent on record" or "completed" — never a phone
number, and never a way around the gate.

The grant — which purposes this agent may call for, where its inbox is, and its signing
key — is read from ``nova-calling.json`` beside the profile, resolved from this file's own
location so several agents on one host each read their own.
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

_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_CACHE: Dict[str, Any] = {}


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / CONFIG_FILENAME


def _load_config() -> Optional[Dict[str, Any]]:
    if "config" in _CACHE:
        return _CACHE["config"]
    config = None
    try:
        path = _config_path()
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("purposes") and loaded.get("key"):
                config = loaded
    except (OSError, ValueError):
        config = None
    _CACHE["config"] = config
    return config


def _sign(key: str, payload: Dict[str, Any]) -> str:
    """Must match ``nova.communications.inbox.sign`` byte for byte; a test holds them together."""
    body = json.dumps({k: payload[k] for k in sorted(payload) if k != "signature"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("ascii"), body, hashlib.sha256).hexdigest()


def _purpose_ids(config: Dict[str, Any]) -> list:
    return [str(p.get("id")) for p in config.get("purposes", []) if isinstance(p, dict) and p.get("id")]


def _reply(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def handle_request_call(args: Dict[str, Any], **_kwargs: Any) -> str:
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
    request_id = uuid.uuid4().hex
    payload = {
        "agent": str(config["agent_id"]),
        "request_id": request_id,
        "contact_id": contact_id,
        "purpose": purpose,
        "note": str(args.get("note") or "")[:300],
        "task_id": str(os.environ.get("HERMES_KANBAN_TASK") or "")[:64],
    }
    payload["signature"] = _sign(str(config["key"]), payload)
    directory = Path(str(config["inbox"])) / "requests" / str(config["agent_id"])
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{request_id}.tmp"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(directory / f"{request_id}.json")
    except OSError as exc:
        return _reply(status="unavailable", message=f"The request could not be queued: {exc}")
    return _reply(
        status="queued", request_id=request_id,
        message=("Requested. The call gateway decides whether and when it is placed — it may "
                 "need a person's approval, or be refused for consent, opt-out or calling "
                 "hours. Check with call_status; do not tell anyone a call was made until it "
                 "says so."),
    )


def handle_call_status(args: Dict[str, Any], **_kwargs: Any) -> str:
    config = _load_config()
    if not config:
        return _reply(status="unavailable", message="Calling is not configured for this agent.")
    request_id = str(args.get("request_id") or "").strip()
    if not _ID.match(request_id):
        return _reply(status="refused", message="request_id is the id request_call returned.")
    path = Path(str(config["inbox"])) / "results" / str(config["agent_id"]) / f"{request_id}.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _reply(status="queued", request_id=request_id,
                      message="Not yet picked up by the call gateway.")
    except (OSError, ValueError):
        return _reply(status="unavailable", message="The result could not be read.")
    return json.dumps(result, sort_keys=True)


def _schemas(config: Dict[str, Any]) -> tuple:
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


def register(ctx: Any) -> None:
    """Register the tools. Called once by the runtime's plugin loader."""
    config = _load_config()
    if not config:
        # No grant, no tools: an agent that may not call should not see a calling tool.
        return
    request, status = _schemas(config)
    ctx.register_tool(name=REQUEST_TOOL, toolset=TOOLSET, schema=request,
                      handler=handle_request_call,
                      description="Request a phone call; the gateway decides.", emoji="📞")
    ctx.register_tool(name=STATUS_TOOL, toolset=TOOLSET, schema=status,
                      handler=handle_call_status,
                      description="See where a requested call stands.", emoji="📞")
