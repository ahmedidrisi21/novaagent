"""Durable state for what calls cost and how voice engines are holding up.

Mixed into :class:`~nova.communications.store.CommsStore`. A call's billed duration comes
from the carrier (its final callback, or its call record during reconciliation); its billed
price arrives later still, when the carrier settles it. Engine failures are kept just long
enough to steer new calls away from an engine that keeps failing.
"""

from __future__ import annotations

from typing import Any, Optional

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_failures (
    tenant_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    call_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS engine_failures_by_time ON engine_failures (tenant_id, engine, at);
-- One row per alert raised: each threshold alerts once a month.
CREATE TABLE IF NOT EXISTS budget_alerts (
    tenant_id TEXT NOT NULL,
    month TEXT NOT NULL,
    percent INTEGER NOT NULL,
    spend REAL NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, month, percent)
);
"""

#: Columns added to ``calls``: what the carrier says the call lasted and cost.
USAGE_COLUMNS = (
    ("duration_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("billed_price", "REAL"),
    ("billed_currency", "TEXT NOT NULL DEFAULT ''"),
)


class UsageStoreMixin:
    """Needs ``_db``, ``_lock``, ``tenant_id``, ``_one`` and ``_write`` from the store."""

    def _migrate_usage(self) -> None:
        self._db.executescript(USAGE_SCHEMA)
        have = {row[1] for row in self._db.execute("PRAGMA table_info(calls)")}
        for column, kind in USAGE_COLUMNS:
            if column not in have:
                self._db.execute(f"ALTER TABLE calls ADD COLUMN {column} {kind}")

    def set_duration(self, call_id: str, seconds: int) -> None:
        self._write("UPDATE calls SET duration_seconds = ? WHERE tenant_id = ? AND call_id = ?",
                    max(0, int(seconds)), self.tenant_id, call_id)

    def set_billing(self, call_id: str, *, seconds: int, price: float, currency: str) -> None:
        self._write(
            "UPDATE calls SET duration_seconds = ?, billed_price = ?, billed_currency = ? "
            "WHERE tenant_id = ? AND call_id = ?",
            max(0, int(seconds)), float(price), currency, self.tenant_id, call_id)

    def usage_rows(self, since: str) -> list[dict]:
        """Calls dialled since ``since``, with what is known about their length and cost."""
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id, agent, telephony, voice_engine, status, duration_seconds, "
                "billed_price, billed_currency, dialled_at FROM calls WHERE tenant_id = ? "
                "AND dialled_at != '' AND dialled_at >= ?", (self.tenant_id, since)).fetchall()
        keys = ("call_id", "agent", "telephony", "voice_engine", "status", "duration_seconds",
                "billed_price", "billed_currency", "dialled_at")
        return [dict(zip(keys, row)) for row in rows]

    def call_usage(self, call_id: str) -> Optional[dict[str, Any]]:
        row = self._one("SELECT duration_seconds, billed_price, billed_currency FROM calls "
                        "WHERE tenant_id = ? AND call_id = ?", self.tenant_id, call_id)
        if row is None:
            return None
        return {"duration_seconds": row[0], "billed_price": row[1], "billed_currency": row[2]}

    def unbilled_calls(self, *, ended_before: str, limit: int = 50) -> list[tuple[str, str, str]]:
        """Finished calls the carrier has not priced for us yet: ``(call_id, carrier, ref)``."""
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id, telephony, provider_call_ref FROM calls WHERE tenant_id = ? "
                "AND status IN ('completed', 'failed') AND provider_call_ref != '' "
                "AND billed_price IS NULL AND updated_at < ? ORDER BY updated_at LIMIT ?",
                (self.tenant_id, ended_before, int(limit))).fetchall()
        return [tuple(row) for row in rows]

    def emails_sent_since(self, since: str) -> int:
        return int(self._one(
            "SELECT COUNT(*) FROM emails WHERE tenant_id = ? AND provider_ref != '' "
            "AND created_at >= ?", self.tenant_id, since)[0])

    # -- engine health --------------------------------------------------------------------

    def record_engine_failure(self, engine: str, call_id: str, reason: str, *, at: str) -> None:
        self._write("INSERT INTO engine_failures (tenant_id, engine, call_id, reason, at) "
                    "VALUES (?, ?, ?, ?, ?)", self.tenant_id, engine, call_id, reason[:300], at)

    def engine_failures_since(self, engine: str, since: str) -> int:
        return int(self._one(
            "SELECT COUNT(*) FROM engine_failures WHERE tenant_id = ? AND engine = ? AND at >= ?",
            self.tenant_id, engine, since)[0])

    def switch_engine(self, call_id: str, *, from_engine: str, to_engine: str, at: str) -> bool:
        """Move a call's voice to another engine, once: only from the one that failed."""
        return self._write(
            "UPDATE calls SET voice_engine = ?, voice_session_id = '', updated_at = ? "
            "WHERE tenant_id = ? AND call_id = ? AND voice_engine = ?",
            to_engine, at, self.tenant_id, call_id, from_engine) == 1

    # -- budget alerts --------------------------------------------------------------------

    def record_budget_alert(self, month: str, percent: int, *, spend: float, at: str) -> bool:
        """False if this threshold already alerted this month."""
        return self._write(
            "INSERT OR IGNORE INTO budget_alerts (tenant_id, month, percent, spend, at) "
            "VALUES (?, ?, ?, ?, ?)", self.tenant_id, month, int(percent), float(spend), at) == 1
