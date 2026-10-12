"""Durable state for emails agents write themselves, and for replies that come back.

Mixed into :class:`~nova.communications.store.CommsStore`. An agent's request is keyed by
its request id, so a request picked up twice is one email; a decision applies only to a
request still waiting for one; a reply is stored once per provider message.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Optional

MAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_requests (
    tenant_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    -- what an approver approves: the address, the subject and the text, hashed
    digest TEXT NOT NULL,
    -- awaiting_approval | sent | blocked | rejected | failed
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    email_id TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, request_id)
);
CREATE TABLE IF NOT EXISTS email_replies (
    tenant_id TEXT NOT NULL,
    reply_id TEXT NOT NULL,
    provider_ref TEXT NOT NULL,
    email_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    -- delivered (to the agent) | opted_out | unmatched
    outcome TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    text_until TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, reply_id),
    UNIQUE (tenant_id, provider_ref)
);
"""

REQUEST_KEYS = ("request_id", "agent", "contact_id", "purpose", "task_id", "to_addr", "subject",
                "body", "digest", "status", "reason", "email_id", "decided_by", "created_at",
                "updated_at")
REPLY_KEYS = ("reply_id", "provider_ref", "email_id", "contact_id", "subject", "text", "outcome",
              "task_id", "received_at", "text_until")


class MailStoreMixin:
    """Needs ``_db``, ``_lock``, ``tenant_id``, ``_one`` and ``_write`` from the store."""

    def create_email_request(self, **fields: Any) -> tuple[dict, bool]:
        """Insert, or return what this request id already made. ``(request, created)``."""
        values = {**{k: "" for k in REQUEST_KEYS}, **fields}
        values["updated_at"] = values["created_at"]
        try:
            self._write(
                f"INSERT INTO email_requests (tenant_id, {', '.join(REQUEST_KEYS)}) VALUES "
                f"(?, {', '.join('?' for _ in REQUEST_KEYS)})",
                self.tenant_id, *(values[k] for k in REQUEST_KEYS))
        except sqlite3.IntegrityError:
            return self.email_request(fields["request_id"]), False
        return self.email_request(fields["request_id"]), True

    def email_request(self, request_id: str) -> Optional[dict]:
        row = self._one(f"SELECT {', '.join(REQUEST_KEYS)} FROM email_requests WHERE "
                        "tenant_id = ? AND request_id = ?", self.tenant_id, request_id)
        return dict(zip(REQUEST_KEYS, row)) if row else None

    def email_requests(self, *, status: str = "", agent: str = "", limit: int = 200) -> list[dict]:
        sql = f"SELECT {', '.join(REQUEST_KEYS)} FROM email_requests WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        for column, value in (("status", status), ("agent", agent)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(zip(REQUEST_KEYS, row)) for row in rows]

    def settle_email_request(self, request_id: str, status: str, *, at: str, reason: str = "",
                             email_id: str = "", decided_by: str = "",
                             only_from: tuple[str, ...] = ("awaiting_approval",)) -> bool:
        """Move a request out of one of ``only_from``, once. False if it was not there."""
        marks = ", ".join("?" for _ in only_from)
        return self._write(
            f"UPDATE email_requests SET status = ?, reason = ?, email_id = ?, "
            f"decided_by = CASE WHEN ? = '' THEN decided_by ELSE ? END, updated_at = ? "
            f"WHERE tenant_id = ? AND request_id = ? AND status IN ({marks})",
            status, reason[:500], email_id, decided_by, decided_by, at, self.tenant_id,
            request_id, *only_from) == 1

    def claim_email_request(self, request_id: str, *, at: str, actor: str = "") -> bool:
        """Take a request to send it, once: awaiting -> sending. A second approver gets False."""
        return self._write(
            "UPDATE email_requests SET status = 'sending', decided_by = ?, updated_at = ? "
            "WHERE tenant_id = ? AND request_id = ? AND status IN ('awaiting_approval', 'new')",
            actor, at, self.tenant_id, request_id) == 1

    def count_agent_emails_since(self, agent: str, since: str) -> int:
        return int(self._one(
            "SELECT COUNT(*) FROM email_requests WHERE tenant_id = ? AND agent = ? "
            "AND status IN ('sent', 'sending') AND updated_at >= ?",
            self.tenant_id, agent, since)[0])

    def emailed_before(self, agent: str, contact_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM email_requests WHERE tenant_id = ? AND agent = ? AND contact_id = ? "
            "AND status = 'sent' LIMIT 1", self.tenant_id, agent, contact_id) is not None

    # -- replies --------------------------------------------------------------------------

    def add_reply(self, *, provider_ref: str, email_id: str, contact_id: str, subject: str,
                  text: str, outcome: str, at: str, text_until: str) -> Optional[str]:
        """Store a reply once per provider message. None if it was seen before."""
        reply_id = uuid.uuid4().hex[:16]
        try:
            self._write(
                f"INSERT INTO email_replies (tenant_id, {', '.join(REPLY_KEYS)}) VALUES "
                f"(?, {', '.join('?' for _ in REPLY_KEYS)})",
                self.tenant_id, reply_id, provider_ref, email_id, contact_id, subject, text,
                outcome, "", at, text_until)
        except sqlite3.IntegrityError:
            return None
        return reply_id

    def set_reply_task(self, reply_id: str, task_id: str) -> None:
        self._write("UPDATE email_replies SET task_id = ? WHERE tenant_id = ? AND reply_id = ?",
                    task_id, self.tenant_id, reply_id)

    def replies(self, *, contact_id: str = "", limit: int = 100) -> list[dict]:
        sql = f"SELECT {', '.join(REPLY_KEYS)} FROM email_replies WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        if contact_id:
            sql += " AND contact_id = ?"
            args.append(contact_id)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY received_at DESC LIMIT ?",
                                    (*args, int(limit))).fetchall()
        return [dict(zip(REPLY_KEYS, row)) for row in rows]

    def expire_reply_text(self, before: str) -> int:
        return self._write(
            "UPDATE email_replies SET text = '' WHERE tenant_id = ? AND text != '' "
            "AND text_until != '' AND text_until < ?", self.tenant_id, before)
