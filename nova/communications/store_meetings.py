"""Durable state for meetings agents book, and the calendar changes that come back.

Mixed into :class:`~nova.communications.store.CommsStore`. A request is keyed by its request
id, so a request picked up twice is one meeting; a decision applies only to a request still
waiting for one; a calendar webhook is applied once however often it is delivered.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

MEETINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_requests (
    tenant_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    -- UTC, 2026-10-13T15:00:00Z
    start TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    -- what an approver approves: the address, event type, time and length, hashed
    digest TEXT NOT NULL,
    -- awaiting_approval | new | sending | booked | blocked | rejected | failed | cancelled
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    booking_ref TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, request_id)
);
CREATE INDEX IF NOT EXISTS meeting_requests_ref ON meeting_requests (tenant_id, booking_ref);
CREATE TABLE IF NOT EXISTS meeting_events (
    tenant_id TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    booking_ref TEXT NOT NULL,
    change TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider_event_id)
);
"""

MEETING_KEYS = ("request_id", "agent", "contact_id", "purpose", "event_type", "task_id",
                "to_addr", "start", "duration_minutes", "digest", "status", "reason",
                "booking_ref", "decided_by", "created_at", "updated_at")


class MeetingsStoreMixin:
    """Needs ``_db``, ``_lock``, ``tenant_id``, ``_one`` and ``_write`` from the store."""

    def create_meeting_request(self, **fields: Any) -> tuple[dict, bool]:
        """Insert, or return what this request id already made. ``(request, created)``."""
        values = {**{k: "" for k in MEETING_KEYS}, **fields}
        values["updated_at"] = values["created_at"]
        try:
            self._write(
                f"INSERT INTO meeting_requests (tenant_id, {', '.join(MEETING_KEYS)}) VALUES "
                f"(?, {', '.join('?' for _ in MEETING_KEYS)})",
                self.tenant_id, *(values[k] for k in MEETING_KEYS))
        except sqlite3.IntegrityError:
            return self.meeting_request(fields["request_id"]), False
        return self.meeting_request(fields["request_id"]), True

    def meeting_request(self, request_id: str) -> Optional[dict]:
        row = self._one(f"SELECT {', '.join(MEETING_KEYS)} FROM meeting_requests WHERE "
                        "tenant_id = ? AND request_id = ?", self.tenant_id, request_id)
        return dict(zip(MEETING_KEYS, row)) if row else None

    def meeting_by_ref(self, booking_ref: str) -> Optional[dict]:
        if not booking_ref:
            return None
        row = self._one(f"SELECT {', '.join(MEETING_KEYS)} FROM meeting_requests WHERE "
                        "tenant_id = ? AND booking_ref = ?", self.tenant_id, booking_ref)
        return dict(zip(MEETING_KEYS, row)) if row else None

    def meeting_requests(self, *, status: str = "", agent: str = "", contact_id: str = "",
                         limit: int = 200) -> list[dict]:
        sql = f"SELECT {', '.join(MEETING_KEYS)} FROM meeting_requests WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        for column, value in (("status", status), ("agent", agent), ("contact_id", contact_id)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(zip(MEETING_KEYS, row)) for row in rows]

    def settle_meeting_request(self, request_id: str, status: str, *, at: str, reason: str = "",
                               booking_ref: str = "", decided_by: str = "",
                               only_from: tuple[str, ...] = ("awaiting_approval",)) -> bool:
        """Move a request out of one of ``only_from``, once. False if it was not there."""
        marks = ", ".join("?" for _ in only_from)
        return self._write(
            f"UPDATE meeting_requests SET status = ?, reason = ?, "
            f"booking_ref = CASE WHEN ? = '' THEN booking_ref ELSE ? END, "
            f"decided_by = CASE WHEN ? = '' THEN decided_by ELSE ? END, updated_at = ? "
            f"WHERE tenant_id = ? AND request_id = ? AND status IN ({marks})",
            status, reason[:500], booking_ref, booking_ref, decided_by, decided_by, at,
            self.tenant_id, request_id, *only_from) == 1

    def claim_meeting_request(self, request_id: str, *, at: str, actor: str = "") -> bool:
        """Take a request to book it, once: awaiting or new -> sending."""
        return self._write(
            "UPDATE meeting_requests SET status = 'sending', decided_by = ?, updated_at = ? "
            "WHERE tenant_id = ? AND request_id = ? AND status IN ('awaiting_approval', 'new')",
            actor, at, self.tenant_id, request_id) == 1

    def reschedule_meeting(self, request_id: str, *, booking_ref: str, start: str, at: str,
                           reason: str) -> bool:
        return self._write(
            "UPDATE meeting_requests SET booking_ref = ?, start = ?, reason = ?, updated_at = ? "
            "WHERE tenant_id = ? AND request_id = ? AND status = 'booked'",
            booking_ref, start, reason[:500], at, self.tenant_id, request_id) == 1

    def count_agent_meetings_since(self, agent: str, since: str) -> int:
        return int(self._one(
            "SELECT COUNT(*) FROM meeting_requests WHERE tenant_id = ? AND agent = ? "
            "AND status IN ('booked', 'sending') AND updated_at >= ?",
            self.tenant_id, agent, since)[0])

    def met_before(self, agent: str, contact_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM meeting_requests WHERE tenant_id = ? AND agent = ? AND "
            "contact_id = ? AND status = 'booked' LIMIT 1",
            self.tenant_id, agent, contact_id) is not None

    def upcoming_meetings(self, after: str) -> list[dict]:
        """Booked meetings that have not started."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(MEETING_KEYS)} FROM meeting_requests WHERE tenant_id = ? "
                "AND status = 'booked' AND start > ?", (self.tenant_id, after)).fetchall()
        return [dict(zip(MEETING_KEYS, row)) for row in rows]

    def record_meeting_event(self, provider_event_id: str, booking_ref: str, change: str,
                             at: str) -> bool:
        """True the first time a delivery is seen."""
        try:
            self._write("INSERT INTO meeting_events (tenant_id, provider_event_id, booking_ref, "
                        "change, received_at) VALUES (?, ?, ?, ?, ?)",
                        self.tenant_id, provider_event_id, booking_ref, change, at)
        except sqlite3.IntegrityError:
            return False
        return True
