"""Durable state for work one agent handed to another.

Mixed into :class:`~nova.communications.store.CommsStore` like the action store. A
delegation's id is the delegator's request id, so a request picked up twice is one
delegation; its work item is keyed by that id, so the board holds one task for it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

DELEGATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
    tenant_id TEXT NOT NULL,
    delegation_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    -- the delegator's task when it asked (its workflow), and the task made for the receiver
    parent_task_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    -- the delegation this one was made while working on, or ''
    parent_id TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL,
    objective TEXT NOT NULL,
    expected_json TEXT NOT NULL DEFAULT '[]',
    contact_id TEXT NOT NULL DEFAULT '',
    call_id TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    -- requested | assigned | needs_approval | blocked | completed | failed | refused | expired
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    deadline TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, delegation_id)
);
CREATE INDEX IF NOT EXISTS delegations_by_task ON delegations (tenant_id, task_id);
CREATE INDEX IF NOT EXISTS delegations_by_receiver ON delegations (tenant_id, to_agent, status);
"""

DELEGATION_KEYS = ("delegation_id", "from_agent", "to_agent", "parent_task_id", "task_id",
                   "parent_id", "depth", "objective", "expected_json", "contact_id", "call_id",
                   "context", "status", "reason", "result_json", "deadline", "created_at",
                   "updated_at")
#: Still waiting on the receiver. Counted against its open-work cap and expired by deadline.
OPEN = ("requested", "assigned", "needs_approval", "blocked")
#: A report may move a delegation only out of these.
REPORTABLE = ("assigned", "needs_approval", "blocked")


def _delegation(row: Optional[tuple]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(zip(DELEGATION_KEYS, row))
    out["expected"] = json.loads(out.pop("expected_json"))
    raw = out.pop("result_json")
    out["result"] = json.loads(raw) if raw else None
    return out


class DelegationsStoreMixin:
    """Needs ``_db``, ``_lock``, ``tenant_id``, ``_one`` and ``_write`` from the store."""

    def create_delegation(self, *, delegation_id: str, from_agent: str, to_agent: str,
                          parent_task_id: str, parent_id: str, depth: int, objective: str,
                          expected: list, contact_id: str, call_id: str, context: str,
                          status: str, reason: str, deadline: str, at: str) -> tuple[dict, bool]:
        """Insert, or return the delegation this id already made. ``(delegation, created)``."""
        try:
            self._write(
                f"INSERT INTO delegations (tenant_id, {', '.join(DELEGATION_KEYS)}) VALUES "
                f"(?, {', '.join('?' for _ in DELEGATION_KEYS)})",
                self.tenant_id, delegation_id, from_agent, to_agent, parent_task_id, "",
                parent_id, depth, objective, json.dumps(expected), contact_id, call_id, context,
                status, reason, "", deadline, at, at)
        except sqlite3.IntegrityError:
            return self.delegation(delegation_id), False
        return self.delegation(delegation_id), True

    def delegation(self, delegation_id: str) -> Optional[dict]:
        return _delegation(self._one(
            f"SELECT {', '.join(DELEGATION_KEYS)} FROM delegations WHERE tenant_id = ? "
            "AND delegation_id = ?", self.tenant_id, delegation_id))

    def delegation_for_task(self, task_id: str) -> Optional[dict]:
        """The delegation that created ``task_id`` on the board, if one did."""
        if not task_id:
            return None
        return _delegation(self._one(
            f"SELECT {', '.join(DELEGATION_KEYS)} FROM delegations WHERE tenant_id = ? "
            "AND task_id = ?", self.tenant_id, task_id))

    def delegations(self, *, status: str = "", agent: str = "", contact_id: str = "",
                    limit: int = 200) -> list[dict]:
        sql = f"SELECT {', '.join(DELEGATION_KEYS)} FROM delegations WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if agent:
            sql += " AND (from_agent = ? OR to_agent = ?)"
            args += [agent, agent]
        if contact_id:
            sql += " AND contact_id = ?"
            args.append(contact_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_delegation(row) for row in rows]

    def count_open_for(self, agent: str) -> int:
        marks = ", ".join("?" for _ in OPEN)
        return int(self._one(
            f"SELECT COUNT(*) FROM delegations WHERE tenant_id = ? AND to_agent = ? "
            f"AND status IN ({marks})", self.tenant_id, agent, *OPEN)[0])

    def assign_delegation(self, delegation_id: str, task_id: str, *, at: str) -> bool:
        return self._write(
            "UPDATE delegations SET status = 'assigned', task_id = ?, updated_at = ? "
            "WHERE tenant_id = ? AND delegation_id = ? AND status = 'requested'",
            task_id, at, self.tenant_id, delegation_id) == 1

    def report_delegation(self, delegation_id: str, *, status: str, result: Any, reason: str,
                          at: str) -> bool:
        """The receiver's answer. False when the delegation was not waiting for one."""
        marks = ", ".join("?" for _ in REPORTABLE)
        return self._write(
            f"UPDATE delegations SET status = ?, result_json = ?, reason = ?, updated_at = ? "
            f"WHERE tenant_id = ? AND delegation_id = ? AND status IN ({marks})",
            status, json.dumps(result) if result is not None else "", reason[:500], at,
            self.tenant_id, delegation_id, *REPORTABLE) == 1

    def expire_delegations(self, before: str, *, at: str) -> list[str]:
        marks = ", ".join("?" for _ in OPEN)
        with self._lock:
            rows = self._db.execute(
                f"SELECT delegation_id FROM delegations WHERE tenant_id = ? AND deadline < ? "
                f"AND status IN ({marks})", (self.tenant_id, before, *OPEN)).fetchall()
            ids = [row[0] for row in rows]
            for delegation_id in ids:
                self._db.execute(
                    f"UPDATE delegations SET status = 'expired', reason = 'the deadline passed "
                    f"with no result', updated_at = ? WHERE tenant_id = ? AND delegation_id = ? "
                    f"AND status IN ({marks})", (at, self.tenant_id, delegation_id, *OPEN))
        return ids
