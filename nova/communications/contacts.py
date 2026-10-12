"""Importing callable contacts and their consent from a CSV export.

The CSV is what a CRM exports. Columns:

=========================  ===============================================================
``contact_id``             required; the CRM's id, the name agents use
``phone``                  required; E.164
``timezone``               required; IANA (``America/Chicago``) — calling hours depend on it
``groups``                 required; ``;``-separated, matched against agents' grants
``ai_voice_consent``       optional; **evidence** of AI-voice consent (form id, URL, call id)
``marketing_consent``      optional; evidence of prior express *written* marketing consent
``name``                   optional; how a follow-up email greets them
``email``                  optional; the only address a follow-up email is ever sent to
=========================  ===============================================================

A consent column holds evidence, never ``yes``: consent nobody can show is consent nobody
has. A blank consent cell grants nothing and revokes nothing — revoking is a separate,
deliberate act. Rows that fail validation are reported by line and skipped; the rest are
imported. The audit records counts, never phone numbers.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nova.audit import AuditLog, new_correlation_id
from nova.communications.gate import Contact
from nova.communications.spec import CONSENT_AI_VOICE, CONSENT_MARKETING_WRITTEN, E164
from nova.communications.store import CommsStore

REQUIRED = ("contact_id", "phone", "timezone", "groups")
CONSENT_COLUMNS = {"ai_voice_consent": CONSENT_AI_VOICE, "marketing_consent": CONSENT_MARKETING_WRITTEN}
_BARE_YES = {"yes", "y", "true", "1", "x", "ok"}
EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


@dataclass
class ImportReport:
    imported: int = 0
    consents: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"imported": self.imported, "consents": self.consents, "errors": self.errors}


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def import_contacts(text: str, *, store: CommsStore, audit: AuditLog, at: str,
                    source: str = "csv") -> ImportReport:
    report = ImportReport()
    reader = csv.DictReader(io.StringIO(text))
    missing = [name for name in REQUIRED if name not in (reader.fieldnames or [])]
    if missing:
        report.errors.append(f"header: missing column(s) {', '.join(missing)}")
        return report

    for line, row in enumerate(reader, start=2):
        values = {k: (v or "").strip() for k, v in row.items() if k}
        problems = []
        if not values["contact_id"]:
            problems.append("contact_id is empty")
        if not E164.match(values["phone"]):
            problems.append(f"phone {values['phone']!r} is not E.164")
        if not _valid_zone(values["timezone"]):
            problems.append(f"timezone {values['timezone']!r} is not an IANA zone")
        groups = tuple(g.strip() for g in values["groups"].split(";") if g.strip())
        if not groups:
            problems.append("groups is empty")
        for column in CONSENT_COLUMNS:
            if values.get(column, "").lower() in _BARE_YES:
                problems.append(f"{column} must be evidence (a form id, URL or call id), not "
                                f"{values[column]!r}")
        if values.get("email") and not EMAIL.match(values["email"]):
            problems.append(f"email {values['email']!r} is not an address")
        if problems:
            report.errors.append(f"line {line}: {'; '.join(problems)}")
            continue

        store.put_contact(Contact(values["contact_id"], values["phone"], values["timezone"], groups,
                                  name=values.get("name", "")[:120],
                                  email=values.get("email", "")))
        report.imported += 1
        for column, kind in CONSENT_COLUMNS.items():
            evidence = values.get(column, "")
            if evidence:
                store.grant_consent(values["contact_id"], kind, evidence=evidence, at=at)
                report.consents += 1

    audit.record(
        "communications.contacts.imported", correlation_id=new_correlation_id(), subject=source,
        detail={"imported": report.imported, "consents": report.consents,
                "rejected": len(report.errors)},
    )
    return report
