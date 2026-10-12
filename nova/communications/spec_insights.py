"""Where the weekly communications digest goes. Absent: the digest is on demand only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from nova._fields import Doc
from nova.errors import SpecError

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


@dataclass(frozen=True)
class InsightsSpec:
    #: An agent the digest becomes work for.
    assignee: str = ""
    #: Staff addresses it is emailed to, through the tenant's email provider. Never a contact.
    email_to: tuple[str, ...] = ()
    weekday: str = "monday"

    @property
    def declared(self) -> bool:
        return bool(self.assignee or self.email_to)

    def to_dict(self) -> dict[str, Any]:
        if not self.declared:
            return {}
        return {"assignee": self.assignee, "email_to": list(self.email_to), "weekday": self.weekday}


def parse_insights(doc: Optional[Doc], *, email_provider: str) -> InsightsSpec:
    if doc is None:
        return InsightsSpec()
    spec = InsightsSpec(assignee=doc.str_("assignee"), email_to=tuple(doc.str_list("email_to")),
                        weekday=doc.choice("weekday", WEEKDAYS, default="monday") or "monday")
    doc.reject_unknown()
    for address in spec.email_to:
        if not _ADDRESS.match(address):
            raise SpecError(f"{address!r} is not an email address",
                            field="communications.insights.email_to", source=doc.source)
    if spec.email_to and not email_provider:
        raise SpecError("emailing the digest needs communications.email (the sender)",
                        field="communications.insights.email_to", source=doc.source)
    if not spec.declared:
        raise SpecError("name an assignee, email_to, or both — or leave insights out",
                        field="communications.insights", source=doc.source)
    return spec
