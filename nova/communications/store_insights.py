"""Aggregate queries for the weekly digest, and the record of which digests went out.

Mixed into :class:`~nova.communications.store.CommsStore`. Every query returns counts, never
a contact, a number or anything said: the digest is shared more widely than a call record.
"""

from __future__ import annotations

from typing import Any

INSIGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, period)
);
"""


class InsightsStoreMixin:
    """Needs ``_db``, ``_lock`` and ``tenant_id`` from the store."""

    def _grouped(self, sql: str, *args: Any) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, (self.tenant_id, *args)).fetchall()

    def call_outcomes(self, since: str, until: str) -> list[tuple]:
        """``(agent, purpose, status, failure, telephony, voice_engine, recording_consent,
        count, minutes)`` for calls requested in the window; minutes rounded up per call."""
        return self._grouped(
            "SELECT agent, purpose, status, failure, telephony, voice_engine, recording_consent, "
            "COUNT(*), COALESCE(SUM((duration_seconds + 59) / 60), 0) FROM calls "
            "WHERE tenant_id = ? AND created_at >= ? AND created_at < ? GROUP BY agent, purpose, "
            "status, failure, telephony, voice_engine, recording_consent",
            since, until)

    def blocked_reasons(self, since: str, until: str) -> list[tuple]:
        """``(reason, count)`` for calls the gate refused in the window."""
        return self._grouped(
            "SELECT reason, COUNT(*) FROM calls WHERE tenant_id = ? AND status = 'policy_blocked' "
            "AND created_at >= ? AND created_at < ? GROUP BY reason ORDER BY COUNT(*) DESC",
            since, until)

    def opt_outs(self, since: str, until: str) -> dict[str, int]:
        out = {"all_channels": 0}
        for (count,) in self._grouped(
                "SELECT COUNT(*) FROM suppressions WHERE tenant_id = ? AND created_at >= ? "
                "AND created_at < ?", since, until):
            out["all_channels"] = count
        for reason, count in self._grouped(
                "SELECT reason, COUNT(*) FROM channel_suppressions WHERE tenant_id = ? "
                "AND created_at >= ? AND created_at < ? GROUP BY reason", since, until):
            out[f"email_{reason.replace(' ', '_')}"] = count
        return out

    def action_outcomes(self, since: str, until: str) -> list[tuple]:
        """``(kind, tier, status, count)`` for actions proposed in the window."""
        return self._grouped(
            "SELECT kind, tier, status, COUNT(*) FROM call_actions WHERE tenant_id = ? "
            "AND created_at >= ? AND created_at < ? GROUP BY kind, tier, status", since, until)

    def delegation_outcomes(self, since: str, until: str) -> list[tuple]:
        return self._grouped(
            "SELECT status, COUNT(*) FROM delegations WHERE tenant_id = ? AND created_at >= ? "
            "AND created_at < ? GROUP BY status", since, until)

    def engine_failure_counts(self, since: str, until: str) -> list[tuple]:
        return self._grouped(
            "SELECT engine, COUNT(*) FROM engine_failures WHERE tenant_id = ? AND at >= ? "
            "AND at < ? GROUP BY engine", since, until)

    def pending_reviews(self) -> int:
        return self._grouped(
            "SELECT COUNT(*) FROM review_queue WHERE tenant_id = ? AND status = 'pending'")[0][0]

    def digest_sent(self, period: str) -> bool:
        return bool(self._grouped("SELECT COUNT(*) FROM digests WHERE tenant_id = ? AND period = ?",
                                  period)[0][0])

    def mark_digest(self, period: str, *, at: str) -> bool:
        """False if this period's digest already went out."""
        with self._lock:
            return self._db.execute(
                "INSERT OR IGNORE INTO digests (tenant_id, period, sent_at) VALUES (?, ?, ?)",
                (self.tenant_id, period, at)).rowcount == 1
