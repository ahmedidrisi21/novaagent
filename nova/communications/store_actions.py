"""Durable state for what a call led to: its action plan, the emails sent, email opt-outs.

Mixed into :class:`~nova.communications.store.CommsStore`, so it shares that store's one
SQLite file, lock and tenant filter. The safety properties are constraints, as there:

* an action runs once — :meth:`claim_action` moves it to ``running`` by one conditional
  UPDATE, and every executor passes an idempotency key derived from the action id, so a
  crash and a re-run produce one call, one task, one email;
* a call's plan is written with its extraction in one transaction, so a crash cannot leave
  a call extracted with its actions lost, or proposed twice;
* a person's decision applies only to actions still waiting for one.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Optional

from nova.communications.mail import EmailStatus, email_advances

ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_actions (
    tenant_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    -- the tier it was proposed at: auto | approve | never
    tier TEXT NOT NULL,
    -- awaiting_approval | pending | running | done | failed | rejected | draft | cancelled
    status TEXT NOT NULL,
    params_json TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    -- UTC; '' runs as soon as it is pending.
    run_at TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, action_id),
    UNIQUE (tenant_id, call_id, seq)
);
CREATE INDEX IF NOT EXISTS actions_by_status ON call_actions (tenant_id, status, run_at);
CREATE TABLE IF NOT EXISTS emails (
    tenant_id TEXT NOT NULL,
    email_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    body_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, email_id),
    UNIQUE (tenant_id, action_id)
);
CREATE INDEX IF NOT EXISTS emails_by_ref ON emails (tenant_id, provider, provider_ref);
CREATE TABLE IF NOT EXISTS email_events (
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    email_id TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider, provider_event_id)
);
-- Opt-outs from one channel. A row in ``suppressions`` stops every channel; a row here
-- stops only its own (an unsubscribe link stops email, not calls).
CREATE TABLE IF NOT EXISTS channel_suppressions (
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, contact_id, channel)
);
"""

ACTION_KEYS = ("action_id", "call_id", "contact_id", "seq", "kind", "tier", "status",
               "params_json", "quote", "note", "run_at", "result", "error", "decided_by",
               "created_at", "updated_at")
EMAIL_KEYS = ("email_id", "action_id", "call_id", "contact_id", "to_addr", "subject", "body",
              "status", "provider", "provider_ref", "detail", "body_until", "created_at",
              "updated_at")
#: Actions that reach the customer. An opt-out cancels the ones still waiting.
OUTREACH = ("callback", "follow_up_email")
WAITING = ("awaiting_approval", "pending")


def _action(row: Optional[tuple]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(zip(ACTION_KEYS, row))
    out["params"] = json.loads(out.pop("params_json"))
    return out


class ActionsStoreMixin:
    """Needs ``_db``, ``_lock``, ``tenant_id``, ``_one`` and ``_write`` from the store."""

    # -- plans --------------------------------------------------------------------------

    def save_extraction(self, call_id: str, *, model: str, fields: list, summary: str,
                        error: str, at: str, transcript_until: str,
                        actions: Iterable[dict] = ()) -> bool:
        """The call's reading and its action plan, together and once. False if already saved."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute(
                    "SELECT 1 FROM call_extractions WHERE tenant_id = ? AND call_id = ?",
                    (self.tenant_id, call_id)).fetchone():
                    self._db.execute("ROLLBACK")
                    return False
                self._db.execute(
                    "INSERT INTO call_extractions (tenant_id, call_id, model, fields_json, "
                    "summary, error, transcript_until, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.tenant_id, call_id, model, json.dumps(fields), summary, error,
                     transcript_until, at))
                for seq, action in enumerate(actions):
                    self._db.execute(
                        f"INSERT INTO call_actions (tenant_id, {', '.join(ACTION_KEYS)}) VALUES "
                        f"(?, {', '.join('?' for _ in ACTION_KEYS)})",
                        (self.tenant_id, uuid.uuid4().hex[:16], call_id, action["contact_id"],
                         seq, action["kind"], action["tier"], action["status"],
                         json.dumps(action["params"], sort_keys=True), action.get("quote", ""),
                         action.get("note", ""), action.get("run_at", ""), "", "", "", at, at))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return True

    def action(self, action_id: str) -> Optional[dict]:
        return _action(self._one(
            f"SELECT {', '.join(ACTION_KEYS)} FROM call_actions WHERE tenant_id = ? "
            "AND action_id = ?", self.tenant_id, action_id))

    def actions(self, *, call_id: str = "", status: str = "", contact_id: str = "",
                limit: int = 200) -> list[dict]:
        sql = f"SELECT {', '.join(ACTION_KEYS)} FROM call_actions WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        if call_id:
            sql += " AND call_id = ?"
            args.append(call_id)
        if contact_id:
            sql += " AND contact_id = ?"
            args.append(contact_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at, call_id, seq LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_action(row) for row in rows]

    def due_actions(self, now: str, *, limit: int = 50) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT action_id FROM call_actions WHERE tenant_id = ? AND status = 'pending' "
                "AND (run_at = '' OR run_at <= ?) ORDER BY run_at, created_at LIMIT ?",
                (self.tenant_id, now, int(limit))).fetchall()
        return [row[0] for row in rows]

    def claim_action(self, action_id: str, *, at: str) -> bool:
        """Move a due action to ``running`` exactly once. False means it is not ours to run."""
        return self._write(
            "UPDATE call_actions SET status = 'running', updated_at = ? WHERE tenant_id = ? "
            "AND action_id = ? AND status = 'pending'", at, self.tenant_id, action_id) == 1

    def finish_action(self, action_id: str, status: str, *, at: str, result: str = "",
                      error: str = "") -> None:
        self._write(
            "UPDATE call_actions SET status = ?, result = ?, error = ?, updated_at = ? "
            "WHERE tenant_id = ? AND action_id = ? AND status = 'running'",
            status, result, error[:500], at, self.tenant_id, action_id)

    def hold_action(self, action_id: str, *, note: str, at: str) -> None:
        """A running auto action that a limit stopped: it now waits for a person."""
        self._write(
            "UPDATE call_actions SET status = 'awaiting_approval', note = ?, updated_at = ? "
            "WHERE tenant_id = ? AND action_id = ? AND status = 'running'",
            note[:300], at, self.tenant_id, action_id)

    def requeue_stale_actions(self, before: str, *, at: str) -> int:
        """Actions left ``running`` by a process that died. Safe: every executor is idempotent."""
        return self._write(
            "UPDATE call_actions SET status = 'pending', updated_at = ? WHERE tenant_id = ? "
            "AND status = 'running' AND updated_at < ?", at, self.tenant_id, before)

    def decide_actions(self, call_id: str, *, approve: Iterable[str], reject: Iterable[str],
                       actor: str, at: str) -> int:
        """Apply a person's decision to the call's waiting actions, in one transaction."""
        changed = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for ids, status in ((approve, "pending"), (reject, "rejected")):
                    for action_id in ids:
                        changed += self._db.execute(
                            "UPDATE call_actions SET status = ?, decided_by = ?, updated_at = ? "
                            "WHERE tenant_id = ? AND call_id = ? AND action_id = ? "
                            "AND status = 'awaiting_approval'",
                            (status, actor, at, self.tenant_id, call_id, action_id)).rowcount
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return changed

    def cancel_outreach(self, contact_id: str, *, reason: str, at: str,
                        kinds: Iterable[str] = OUTREACH) -> int:
        kinds = tuple(kinds)
        marks = ", ".join("?" for _ in kinds)
        return self._write(
            f"UPDATE call_actions SET status = 'cancelled', note = ?, updated_at = ? "
            f"WHERE tenant_id = ? AND contact_id = ? AND kind IN ({marks}) "
            f"AND status IN ('awaiting_approval', 'pending')",
            reason[:300], at, self.tenant_id, contact_id, *kinds)

    def count_actions_done(self, contact_id: str, kind: str, since: str, *,
                           undecided_only: bool = True) -> int:
        """Actions of ``kind`` carried out for a contact since ``since``; by default only those
        no person approved — the ones the auto-tier rate limit counts."""
        sql = ("SELECT COUNT(*) FROM call_actions WHERE tenant_id = ? AND contact_id = ? AND "
               "kind = ? AND status = 'done' AND updated_at >= ?")
        if undecided_only:
            sql += " AND decided_by = ''"
        return int(self._one(sql, self.tenant_id, contact_id, kind, since)[0])

    def spawned_by(self, call_id: str) -> str:
        """The call whose callback action placed ``call_id``, or ''."""
        row = self._one(
            "SELECT call_id FROM call_actions WHERE tenant_id = ? AND kind = 'callback' "
            "AND result = ?", self.tenant_id, call_id)
        return row[0] if row else ""

    def action_stats(self) -> dict[str, dict[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, status, decided_by != '' AS decided, COUNT(*) FROM call_actions "
                "WHERE tenant_id = ? GROUP BY kind, status, decided", (self.tenant_id,)).fetchall()
        out: dict[str, dict[str, int]] = {}
        for kind, status, decided, count in rows:
            entry = out.setdefault(kind, {})
            entry[status] = entry.get(status, 0) + count
            if decided:
                key = "rejected_by_person" if status == "rejected" else "approved_by_person"
                entry[key] = entry.get(key, 0) + count
        return out

    # -- emails -------------------------------------------------------------------------

    def create_email(self, *, action_id: str, call_id: str, contact_id: str, to: str,
                     subject: str, body: str, provider: str, body_until: str,
                     at: str) -> tuple[dict, bool]:
        """One email per action. ``(email, created)``; a re-run finds the first one."""
        email_id = uuid.uuid4().hex
        try:
            self._write(
                f"INSERT INTO emails (tenant_id, {', '.join(EMAIL_KEYS)}) VALUES "
                f"(?, {', '.join('?' for _ in EMAIL_KEYS)})",
                self.tenant_id, email_id, action_id, call_id, contact_id, to, subject, body,
                EmailStatus.QUEUED.value, provider, "", "", body_until, at, at)
        except sqlite3.IntegrityError:
            row = self._one(f"SELECT {', '.join(EMAIL_KEYS)} FROM emails WHERE tenant_id = ? "
                            "AND action_id = ?", self.tenant_id, action_id)
            return dict(zip(EMAIL_KEYS, row)), False
        return self.email(email_id), True

    def email(self, email_id: str) -> Optional[dict]:
        row = self._one(f"SELECT {', '.join(EMAIL_KEYS)} FROM emails WHERE tenant_id = ? "
                        "AND email_id = ?", self.tenant_id, email_id)
        return dict(zip(EMAIL_KEYS, row)) if row else None

    def email_by_ref(self, provider: str, ref: str) -> Optional[dict]:
        if not ref:
            return None
        row = self._one(f"SELECT {', '.join(EMAIL_KEYS)} FROM emails WHERE tenant_id = ? "
                        "AND provider = ? AND provider_ref = ?", self.tenant_id, provider, ref)
        return dict(zip(EMAIL_KEYS, row)) if row else None

    def emails(self, *, contact_id: str = "", call_id: str = "", limit: int = 100) -> list[dict]:
        sql = f"SELECT {', '.join(EMAIL_KEYS)} FROM emails WHERE tenant_id = ?"
        args: list[Any] = [self.tenant_id]
        if contact_id:
            sql += " AND contact_id = ?"
            args.append(contact_id)
        if call_id:
            sql += " AND call_id = ?"
            args.append(call_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(zip(EMAIL_KEYS, row)) for row in rows]

    def set_email_ref(self, email_id: str, ref: str, *, at: str) -> None:
        """Record the provider's id, and ``sent`` unless an event already moved it further."""
        self._write(
            "UPDATE emails SET provider_ref = ?, updated_at = ?, status = CASE WHEN status = ? "
            "THEN ? ELSE status END WHERE tenant_id = ? AND email_id = ?",
            ref, at, EmailStatus.QUEUED.value, EmailStatus.SENT.value, self.tenant_id, email_id)

    def advance_email(self, email_id: str, status: EmailStatus, *, at: str,
                      detail: str = "") -> bool:
        with self._lock:
            row = self._db.execute("SELECT status FROM emails WHERE tenant_id = ? AND email_id = ?",
                                   (self.tenant_id, email_id)).fetchone()
            if row is None or not email_advances(EmailStatus(row[0]), status):
                return False
            self._db.execute(
                "UPDATE emails SET status = ?, detail = CASE WHEN ? = '' THEN detail ELSE ? END, "
                "updated_at = ? WHERE tenant_id = ? AND email_id = ?",
                (status.value, detail, detail, at, self.tenant_id, email_id))
        return True

    def record_email_event(self, *, provider: str, provider_event_id: str, email_id: str,
                           status: EmailStatus, at: str) -> bool:
        try:
            self._write(
                "INSERT INTO email_events (tenant_id, provider, provider_event_id, email_id, "
                "status, received_at) VALUES (?, ?, ?, ?, ?, ?)",
                self.tenant_id, provider, provider_event_id, email_id, status.value, at)
        except sqlite3.IntegrityError:
            return False
        return True

    def expire_email_bodies(self, before: str) -> int:
        return self._write(
            "UPDATE emails SET body = '' WHERE tenant_id = ? AND body != '' AND body_until < ?",
            self.tenant_id, before)

    def count_emails_since(self, contact_id: str, since: str) -> int:
        return int(self._one(
            "SELECT COUNT(*) FROM emails WHERE tenant_id = ? AND contact_id = ? "
            "AND created_at >= ?", self.tenant_id, contact_id, since)[0])

    # -- channel opt-outs ---------------------------------------------------------------

    def suppress_channel(self, contact_id: str, channel: str, *, reason: str, at: str) -> bool:
        """False if the contact had already opted out of this channel."""
        return self._write(
            "INSERT OR IGNORE INTO channel_suppressions (tenant_id, contact_id, channel, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            self.tenant_id, contact_id, channel, reason, at) == 1

    def channel_suppressed(self, contact_id: str, channel: str) -> bool:
        return self._one(
            "SELECT 1 FROM channel_suppressions WHERE tenant_id = ? AND contact_id = ? "
            "AND channel = ?", self.tenant_id, contact_id, channel) is not None
