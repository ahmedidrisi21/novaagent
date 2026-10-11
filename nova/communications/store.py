"""Durable call state: contacts, consent, suppressions, calls and carrier events.

SQLite from the standard library, one file per tenant, and every query also filtered by
tenant id — the file boundary is the first wall, the column is the second, and a test
asserts the second holds even when two tenants share a file.

Three uniqueness rules carry the safety properties, so they are constraints rather than
checks in Python that a race could slip past:

* ``(tenant_id, idempotency_key)`` — a retried request finds the call it already made.
* ``(provider, provider_event_id)`` — a webhook delivered twice is applied once.
* the dial claim — a call moves into ``initiated`` by one conditional UPDATE, so two
  approvals arriving together dial once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from nova.communications.contracts import (
    ACTIVE,
    CallStatus,
    FailureReason,
    advances,
)
from nova.communications.gate import Contact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT '',
    groups_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (tenant_id, contact_id)
);
CREATE TABLE IF NOT EXISTS consents (
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    evidence TEXT NOT NULL,
    PRIMARY KEY (tenant_id, contact_id, kind)
);
CREATE TABLE IF NOT EXISTS suppressions (
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, contact_id)
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    agent TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    telephony TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    failure TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    provider_call_ref TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    -- Set once, by the dial claim. Daily caps count this, not the request time.
    dialled_at TEXT NOT NULL DEFAULT '',
    voice_engine TEXT NOT NULL DEFAULT '',
    voice_session_id TEXT NOT NULL DEFAULT '',
    -- '' (never asked) | granted | refused | unclear. Evidence is what the caller pressed
    -- or said, as the carrier heard it.
    recording_consent TEXT NOT NULL DEFAULT '',
    recording_evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS calls_by_agent ON calls (tenant_id, agent, status);
CREATE INDEX IF NOT EXISTS calls_by_ref ON calls (telephony, provider_call_ref);
CREATE TABLE IF NOT EXISTS call_recordings (
    tenant_id TEXT NOT NULL,
    recording_ref TEXT NOT NULL,
    call_id TEXT NOT NULL,
    -- recording | paused | completed | absent | failed | deleted
    state TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    retention_until TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, recording_ref)
);
CREATE INDEX IF NOT EXISTS recordings_by_call ON call_recordings (tenant_id, call_id);
CREATE TABLE IF NOT EXISTS call_events (
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    status TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider, provider_event_id)
);
"""


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    tenant_id: str
    idempotency_key: str
    agent: str
    contact_id: str
    purpose: str
    task_id: str
    telephony: str
    action_digest: str
    status: CallStatus
    failure: str
    reason: str
    provider_call_ref: str
    approved_by: str
    created_at: str
    updated_at: str
    #: The engine the call was bridged to, chosen at answer time, and its session.
    voice_engine: str = ""
    voice_session_id: str = ""
    recording_consent: str = ""
    recording_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "agent": self.agent,
            "contact_id": self.contact_id,
            "purpose": self.purpose,
            "task_id": self.task_id,
            "telephony": self.telephony,
            "status": self.status.value,
            "failure": self.failure,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "voice_engine": self.voice_engine,
            "recording_consent": self.recording_consent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_CALL_COLUMNS = (
    "call_id, tenant_id, idempotency_key, agent, contact_id, purpose, task_id, telephony, "
    "action_digest, status, failure, reason, provider_call_ref, approved_by, created_at, "
    "updated_at, voice_engine, voice_session_id, recording_consent, recording_evidence"
)


def _call(row: Optional[tuple]) -> Optional[CallRecord]:
    if row is None:
        return None
    values = list(row)
    values[9] = CallStatus(values[9])
    return CallRecord(*values)


class CommsStore:
    def __init__(self, path: Path | str, *, tenant_id: str) -> None:
        self.path = Path(path)
        self.tenant_id = tenant_id
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=30
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a store created by an earlier version lacks. Additive only."""
        have = {row[1] for row in self._db.execute("PRAGMA table_info(calls)")}
        for column in ("voice_engine", "voice_session_id", "recording_consent",
                       "recording_evidence"):
            if column not in have:
                self._db.execute(f"ALTER TABLE calls ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        self._db.close()

    def _one(self, sql: str, *args: Any) -> Optional[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _write(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # -- contacts, consent, suppression -------------------------------------------------

    def put_contact(self, contact: Contact) -> None:
        self._write(
            "INSERT INTO contacts (tenant_id, contact_id, phone, timezone, groups_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id, contact_id) DO UPDATE SET "
            "phone = excluded.phone, timezone = excluded.timezone, "
            "groups_json = excluded.groups_json",
            self.tenant_id, contact.contact_id, contact.phone, contact.timezone,
            json.dumps(list(contact.groups)),
        )

    def contact(self, contact_id: str) -> Optional[Contact]:
        row = self._one(
            "SELECT contact_id, phone, timezone, groups_json FROM contacts "
            "WHERE tenant_id = ? AND contact_id = ?",
            self.tenant_id, contact_id,
        )
        if row is None:
            return None
        return Contact(row[0], row[1], row[2], tuple(json.loads(row[3])))

    def grant_consent(self, contact_id: str, kind: str, *, evidence: str, at: str) -> None:
        """Record consent. ``evidence`` says where it came from — a form id, a call id."""
        if not evidence:
            raise ValueError("consent without evidence is not consent")
        self._write(
            "INSERT OR REPLACE INTO consents (tenant_id, contact_id, kind, granted_at, evidence) "
            "VALUES (?, ?, ?, ?, ?)",
            self.tenant_id, contact_id, kind, at, evidence,
        )

    def revoke_consent(self, contact_id: str, kind: str) -> None:
        self._write(
            "DELETE FROM consents WHERE tenant_id = ? AND contact_id = ? AND kind = ?",
            self.tenant_id, contact_id, kind,
        )

    def consents(self, contact_id: str) -> frozenset[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind FROM consents WHERE tenant_id = ? AND contact_id = ?",
                (self.tenant_id, contact_id),
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def suppress(self, contact_id: str, *, reason: str, at: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO suppressions (tenant_id, contact_id, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            self.tenant_id, contact_id, reason, at,
        )

    def is_suppressed(self, contact_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM suppressions WHERE tenant_id = ? AND contact_id = ?",
            self.tenant_id, contact_id,
        ) is not None

    # -- calls --------------------------------------------------------------------------

    def create_call(
        self,
        *,
        idempotency_key: str,
        agent: str,
        contact_id: str,
        purpose: str,
        task_id: str,
        telephony: str,
        action_digest: str,
        status: CallStatus,
        reason: str,
        at: str,
    ) -> tuple[CallRecord, bool]:
        """Insert a call, or return the one this key already made. ``(record, created)``."""
        call_id = uuid.uuid4().hex
        try:
            self._write(
                f"INSERT INTO calls ({_CALL_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '', '', ?, ?, '', '', '', '')",
                call_id, self.tenant_id, idempotency_key, agent, contact_id, purpose,
                task_id, telephony, action_digest, status.value, reason, at, at,
            )
        except sqlite3.IntegrityError:
            existing = self._one(
                f"SELECT {_CALL_COLUMNS} FROM calls WHERE tenant_id = ? AND idempotency_key = ?",
                self.tenant_id, idempotency_key,
            )
            return _call(existing), False
        return self.call(call_id), True

    def call(self, call_id: str) -> Optional[CallRecord]:
        return _call(self._one(
            f"SELECT {_CALL_COLUMNS} FROM calls WHERE tenant_id = ? AND call_id = ?",
            self.tenant_id, call_id,
        ))

    def call_by_ref(self, telephony: str, provider_call_ref: str) -> Optional[CallRecord]:
        if not provider_call_ref:
            return None
        return _call(self._one(
            f"SELECT {_CALL_COLUMNS} FROM calls "
            "WHERE tenant_id = ? AND telephony = ? AND provider_call_ref = ?",
            self.tenant_id, telephony, provider_call_ref,
        ))

    def call_for_carrier(self, telephony: str, call_id: str, ref: str) -> Optional[CallRecord]:
        """The call a carrier callback names by our id, if it belongs to that carrier and the
        reference is not already another call's. Adopts ``ref`` when none was stored yet —
        the callback beat :meth:`set_provider_ref`."""
        record = self.call(call_id)
        if record is None or record.telephony != telephony:
            return None
        if record.provider_call_ref and record.provider_call_ref != ref:
            return None
        if not record.provider_call_ref:
            self._write(
                "UPDATE calls SET provider_call_ref = ? "
                "WHERE tenant_id = ? AND call_id = ? AND provider_call_ref = ''",
                ref, self.tenant_id, call_id,
            )
            record = self.call(call_id)
        return record

    def set_voice(self, call_id: str, *, engine: str, session_id: str = "", at: str) -> None:
        self._write(
            "UPDATE calls SET voice_engine = ?, voice_session_id = ?, updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ?",
            engine, session_id, at, self.tenant_id, call_id,
        )

    def claim_session(self, call_id: str, *, engine: str, session_id: str, at: str) -> bool:
        """Bind a voice session to a call exactly once. False means one is already bound."""
        return self._write(
            "UPDATE calls SET voice_session_id = ?, updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ? AND voice_engine = ? AND voice_session_id = ''",
            session_id, at, self.tenant_id, call_id, engine,
        ) == 1

    def claim_for_dial(self, call_id: str, *, approved_by: str, at: str) -> bool:
        """Move a call into ``initiated`` exactly once. False means someone else did."""
        return self._write(
            "UPDATE calls SET status = ?, approved_by = ?, dialled_at = ?, updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ? AND status IN (?, ?)",
            CallStatus.INITIATED.value, approved_by, at, at, self.tenant_id, call_id,
            CallStatus.REQUESTED.value, CallStatus.APPROVAL_REQUIRED.value,
        ) == 1

    def set_provider_ref(self, call_id: str, ref: str, *, at: str) -> None:
        self._write(
            "UPDATE calls SET provider_call_ref = ?, updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ?",
            ref, at, self.tenant_id, call_id,
        )

    def advance(
        self,
        call_id: str,
        status: CallStatus,
        *,
        at: str,
        failure: Optional[FailureReason] = None,
        reason: str = "",
    ) -> bool:
        """Move a call forward. Returns False, changing nothing, if it would not advance."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT status FROM calls WHERE tenant_id = ? AND call_id = ?",
                    (self.tenant_id, call_id),
                ).fetchone()
                if row is None or not advances(CallStatus(row[0]), status):
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute(
                    "UPDATE calls SET status = ?, failure = ?, "
                    "reason = CASE WHEN ? = '' THEN reason ELSE ? END, updated_at = ? "
                    "WHERE tenant_id = ? AND call_id = ?",
                    (status.value, failure.value if failure else "", reason, reason, at,
                     self.tenant_id, call_id),
                )
                self._db.execute("COMMIT")
                return True
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def calls_with_status(
        self, statuses, *, updated_before: str = "", limit: int = 500
    ) -> list[CallRecord]:
        """Calls in any of ``statuses``, oldest change first, optionally untouched since a time."""
        values = [CallStatus(s).value for s in statuses]
        if not values:
            return []
        marks = ", ".join("?" for _ in values)
        sql = (f"SELECT {_CALL_COLUMNS} FROM calls WHERE tenant_id = ? AND status IN ({marks})")
        args: list = [self.tenant_id, *values]
        if updated_before:
            sql += " AND updated_at < ?"
            args.append(updated_before)
        sql += " ORDER BY updated_at LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_call(row) for row in rows]

    def recent_calls(
        self, *, limit: int = 50, agent: str = "", status: str = ""
    ) -> list[CallRecord]:
        """Newest first, for the call monitor."""
        sql = f"SELECT {_CALL_COLUMNS} FROM calls WHERE tenant_id = ?"
        args: list = [self.tenant_id]
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        if status:
            sql += " AND status = ?"
            args.append(CallStatus(status).value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_call(row) for row in rows]

    # -- recordings -----------------------------------------------------------------------

    def set_recording_consent(self, call_id: str, outcome: str, evidence: str, *, at: str) -> bool:
        """Record the answer once. False if this call already has one: the first answer stands."""
        return self._write(
            "UPDATE calls SET recording_consent = ?, recording_evidence = ?, updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ? AND recording_consent = ''",
            outcome, evidence[:200], at, self.tenant_id, call_id,
        ) == 1

    def add_recording(self, call_id: str, recording_ref: str, *, at: str, retention_until: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO call_recordings (tenant_id, recording_ref, call_id, state, "
            "started_at, retention_until) VALUES (?, ?, ?, 'recording', ?, ?)",
            self.tenant_id, recording_ref, call_id, at, retention_until,
        )

    def set_recording_state(self, recording_ref: str, state: str, *, duration: int = 0,
                            at: str = "") -> None:
        """Move a recording on. A deleted recording stays deleted whatever arrives later."""
        self._write(
            "UPDATE call_recordings SET state = ?, "
            "duration_seconds = CASE WHEN ? > 0 THEN ? ELSE duration_seconds END, "
            "deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END "
            "WHERE tenant_id = ? AND recording_ref = ? AND state != 'deleted'",
            state, duration, duration, state, at, self.tenant_id, recording_ref,
        )

    def recordings(self, *, call_id: str = "", contact_id: str = "",
                   due_before: str = "") -> list[dict]:
        """Recordings by call, by contact, or past retention — never the deleted ones."""
        sql = ("SELECT r.recording_ref, r.call_id, r.state, r.duration_seconds, r.started_at, "
               "r.retention_until FROM call_recordings r JOIN calls c "
               "ON c.call_id = r.call_id AND c.tenant_id = r.tenant_id "
               "WHERE r.tenant_id = ? AND r.state != 'deleted'")
        args: list = [self.tenant_id]
        if call_id:
            sql += " AND r.call_id = ?"
            args.append(call_id)
        if contact_id:
            sql += " AND c.contact_id = ?"
            args.append(contact_id)
        if due_before:
            sql += " AND r.retention_until < ?"
            args.append(due_before)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY r.started_at", args).fetchall()
        keys = ("recording_ref", "call_id", "state", "duration_seconds", "started_at",
                "retention_until")
        return [dict(zip(keys, row)) for row in rows]

    def count_active(self, agent: str) -> int:
        statuses = [s.value for s in ACTIVE]
        marks = ", ".join("?" for _ in statuses)
        row = self._one(
            f"SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND agent = ? AND status IN ({marks})",
            self.tenant_id, agent, *statuses,
        )
        return int(row[0])

    def count_dialled_since(self, agent: str, since: str) -> int:
        """Calls handed to a carrier since ``since``. Refused, pending and rejected ones never were."""
        row = self._one(
            "SELECT COUNT(*) FROM calls WHERE tenant_id = ? AND agent = ? AND dialled_at != '' "
            "AND dialled_at >= ?",
            self.tenant_id, agent, since,
        )
        return int(row[0])

    def completed_before(self, agent: str, contact_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM calls WHERE tenant_id = ? AND agent = ? AND contact_id = ? "
            "AND status = ? LIMIT 1",
            self.tenant_id, agent, contact_id, CallStatus.COMPLETED.value,
        ) is not None

    # -- carrier events -----------------------------------------------------------------

    def record_event(
        self, *, provider: str, provider_event_id: str, call_id: str, status: CallStatus, at: str
    ) -> bool:
        """Remember a carrier event before acting on it. False means it was seen before.

        Written first, so a crash after this line leaves an event that is never applied
        twice; a missed status is recovered by reconciling with the carrier, a doubled
        side effect is not recoverable at all.
        """
        try:
            self._write(
                "INSERT INTO call_events (provider, provider_event_id, tenant_id, call_id, "
                "status, received_at) VALUES (?, ?, ?, ?, ?, ?)",
                provider, provider_event_id, self.tenant_id, call_id, status.value, at,
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def mark_applied(self, provider: str, provider_event_id: str) -> None:
        self._write(
            "UPDATE call_events SET applied = 1 "
            "WHERE tenant_id = ? AND provider = ? AND provider_event_id = ?",
            self.tenant_id, provider, provider_event_id,
        )
