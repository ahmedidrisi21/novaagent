"""One timeline of everything that happened with a contact, an agent or a piece of work.

Calls, the emails and actions they led to, and work handed between agents, newest first,
each with its status, time, agent, provider, contact, task and who approved it. A row is
**metadata only**: no transcript, no email text, no recording. Those stay behind the
narrower routes that show one call or one email, so a timeline can be shown to more people
than a transcript can.

**Linked by task.** A call carries the task its agent was working on; a delegation carries
the task it was made from and the task it created; actions and emails belong to their call.
Filtering by a task id shows that piece of work end to end.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

CHANNELS = ("call", "email", "action", "delegation")


def _call_row(record) -> dict:
    return {"at": record.created_at, "updated_at": record.updated_at, "channel": "call",
            "id": record.call_id, "status": record.status.value, "agent": record.agent,
            "contact_id": record.contact_id, "task_id": record.task_id,
            "provider": record.telephony, "approved_by": record.approved_by,
            "what": f"{record.purpose} call" + (f" via {record.voice_engine}"
                                                if record.voice_engine else ""),
            "reason": record.reason}


def _action_row(action: Mapping[str, Any], call) -> dict:
    return {"at": action["created_at"], "updated_at": action["updated_at"], "channel": "action",
            "id": action["action_id"], "status": action["status"],
            "agent": call.agent if call else "", "contact_id": action["contact_id"],
            "task_id": call.task_id if call else "", "provider": "",
            "approved_by": action["decided_by"],
            "what": f"{action['kind']} ({action['tier']}) from call {action['call_id']}",
            "reason": action["error"] or action["note"]}


def _email_row(email: Mapping[str, Any], call) -> dict:
    return {"at": email["created_at"], "updated_at": email["updated_at"], "channel": "email",
            "id": email["email_id"], "status": email["status"],
            "agent": call.agent if call else "", "contact_id": email["contact_id"],
            "task_id": call.task_id if call else "", "provider": email["provider"],
            "approved_by": "", "what": f"follow-up email from call {email['call_id']}",
            "reason": email["detail"]}


def _delegation_row(record: Mapping[str, Any]) -> dict:
    return {"at": record["created_at"], "updated_at": record["updated_at"],
            "channel": "delegation", "id": record["delegation_id"], "status": record["status"],
            "agent": record["from_agent"], "contact_id": record["contact_id"],
            "task_id": record["parent_task_id"], "provider": "", "approved_by": "",
            "what": f"{record['from_agent']} -> {record['to_agent']}: {record['objective'][:120]}",
            "reason": record["reason"], "to_agent": record["to_agent"],
            "created_task_id": record["task_id"]}


def timeline(store, *, contact_id: str = "", agent: str = "", channel: str = "",
             status: str = "", task_id: str = "", since: str = "", until: str = "",
             limit: int = 200) -> list[dict]:
    """Newest first. Every filter narrows; none is required."""
    if channel and channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    limit = max(1, min(int(limit), 1000))
    calls = store.recent_calls(limit=1000, agent=agent, contact_id=contact_id, task_id=task_id)
    by_id = {record.call_id: record for record in calls}

    def call_of(call_id: str):
        return by_id.get(call_id) or store.call(call_id)

    rows: list[dict] = [_call_row(record) for record in calls]
    related: Iterable[dict]
    if contact_id:
        related = store.actions(contact_id=contact_id, limit=1000)
    else:
        related = [a for record in calls for a in store.actions(call_id=record.call_id)]
    rows += [_action_row(a, call_of(a["call_id"])) for a in related]
    emails = store.emails(contact_id=contact_id, limit=1000) if contact_id else \
        [e for record in calls for e in store.emails(call_id=record.call_id)]
    rows += [_email_row(e, call_of(e["call_id"])) for e in emails]
    rows += [_delegation_row(d) for d in store.delegations(agent=agent, contact_id=contact_id,
                                                          limit=1000)]

    keep: list[Callable[[dict], bool]] = [
        lambda r: not channel or r["channel"] == channel,
        lambda r: not status or r["status"] == status,
        lambda r: not agent or agent in (r["agent"], r.get("to_agent")),
        lambda r: not task_id or task_id in (r["task_id"], r.get("created_task_id")),
        lambda r: not since or r["at"] >= since,
        lambda r: not until or r["at"] < until,
    ]
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: r["at"], reverse=True):
        key = (row["channel"], row["id"])
        if key not in seen and all(test(row) for test in keep):
            seen.add(key)
            out.append(row)
    return out[:limit]
