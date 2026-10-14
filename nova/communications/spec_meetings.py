"""The ``communications.meetings`` block: which calendar, which event types, who may book.

An event type is a kind of meeting the tenant has set up at its calendar provider (an
``Intro call`` of 30 minutes). An agent may book only the event types its grant names, only
for purposes both name, only for contacts in the groups the grant names — the same shape as
an agent's email grant, for the same reason: "may book anything for anyone" is not a grant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from nova._fields import Doc
from nova.communications.providers import Kind, get_provider
from nova.communications.spec_actions import EMAIL_APPROVAL
from nova.errors import SpecError

#: The provider's own id for an event type. Cal.com's are numbers; kept as text so a
#: provider with other ids needs no spec change.
_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class EventType:
    id: str
    title: str
    provider_ref: str
    duration_minutes: int
    purposes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "provider_ref": self.provider_ref,
                "duration_minutes": self.duration_minutes, "purposes": list(self.purposes)}


@dataclass(frozen=True)
class MeetingGrant:
    agent: str
    purposes: tuple[str, ...]
    contact_groups: tuple[str, ...]
    event_types: tuple[str, ...]
    approval: str = "always"
    max_per_day: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "purposes": list(self.purposes),
                "contact_groups": list(self.contact_groups),
                "event_types": list(self.event_types), "approval": self.approval,
                "max_per_day": self.max_per_day}


@dataclass(frozen=True)
class MeetingsSpec:
    provider: str = ""
    event_types: tuple[EventType, ...] = ()
    agents: tuple[MeetingGrant, ...] = ()
    #: How far ahead a meeting may be booked, and how soon: a time already past, or
    #: minutes away, is not a time anyone can be told about.
    horizon_days: int = 30
    min_notice_hours: int = 2

    def grant(self, agent: str) -> Optional[MeetingGrant]:
        return next((g for g in self.agents if g.agent == agent), None)

    def event_type(self, event_type_id: str) -> Optional[EventType]:
        return next((e for e in self.event_types if e.id == event_type_id), None)

    def to_dict(self) -> dict[str, Any]:
        if not self.provider:
            return {}
        return {"provider": self.provider, "horizon_days": self.horizon_days,
                "min_notice_hours": self.min_notice_hours,
                "event_types": [e.to_dict() for e in self.event_types],
                "agents": [g.to_dict() for g in self.agents]}


def _list(doc: Doc, key: str) -> list:
    raw = doc._raw(key) or []
    if not isinstance(raw, list):
        raise SpecError("must be a list", field=f"communications.meetings.{key}", source=doc.source)
    return raw


def parse_meetings(doc: Optional[Doc], *, allow_test: bool) -> MeetingsSpec:
    if doc is None:
        return MeetingsSpec()
    provider_id = doc.str_("provider", required=True)
    provider = get_provider(provider_id)
    if provider is None or provider.kind is not Kind.CALENDAR:
        raise SpecError(f"{provider_id!r} is not a known calendar provider",
                        field="communications.meetings.provider", source=doc.source)
    if provider.test_only and not allow_test:
        raise SpecError(f"{provider_id!r} books nothing; set allow_test_providers: true to use "
                        "it outside production", field="communications.meetings.provider",
                        source=doc.source)
    types: list[EventType] = []
    for index, item in enumerate(_list(doc, "event_types")):
        where = f"communications.meetings.event_types[{index}]"
        item_doc = Doc(item, source=doc.source, prefix=where)
        event_type = EventType(
            id=item_doc.identifier("id"), title=item_doc.str_("title", required=True),
            provider_ref=str(item_doc._raw("provider_ref") or "").strip(),
            duration_minutes=item_doc.int_("duration_minutes", default=30, minimum=5, maximum=480),
            purposes=tuple(item_doc.str_list("purposes")))
        item_doc.reject_unknown()
        if not _REF.match(event_type.provider_ref):
            raise SpecError("provider_ref is the event type's id at the calendar provider",
                            field=f"{where}.provider_ref", source=doc.source)
        if not event_type.purposes:
            raise SpecError("purposes is required: the purposes this meeting may be booked for",
                            field=where, source=doc.source)
        if any(t.id == event_type.id for t in types):
            raise SpecError(f"event type {event_type.id!r} is declared twice", field=where,
                            source=doc.source)
        types.append(event_type)
    if not types:
        raise SpecError("at least one event type is required",
                        field="communications.meetings.event_types", source=doc.source)
    grants: list[MeetingGrant] = []
    for index, item in enumerate(_list(doc, "agents")):
        where = f"communications.meetings.agents[{index}]"
        item_doc = Doc(item, source=doc.source, prefix=where)
        grant = MeetingGrant(
            agent=item_doc.identifier("agent"), purposes=tuple(item_doc.str_list("purposes")),
            contact_groups=tuple(item_doc.str_list("contact_groups")),
            event_types=tuple(item_doc.str_list("event_types")),
            approval=item_doc.choice("approval", EMAIL_APPROVAL, default="always") or "always",
            max_per_day=item_doc.int_("max_per_day", default=10, minimum=1, maximum=500))
        item_doc.reject_unknown()
        if not (grant.purposes and grant.contact_groups and grant.event_types):
            raise SpecError("purposes, contact_groups and event_types are required: an agent "
                            "that may book anyone into anything is not a grant", field=where,
                            source=doc.source)
        unknown = [e for e in grant.event_types if not any(t.id == e for t in types)]
        if unknown:
            raise SpecError(f"{unknown[0]!r} is not a declared event type",
                            field=f"{where}.event_types", source=doc.source)
        if any(g.agent == grant.agent for g in grants):
            raise SpecError(f"{grant.agent!r} has two meeting grants", field=where,
                            source=doc.source)
        grants.append(grant)
    spec = MeetingsSpec(
        provider=provider_id, event_types=tuple(types), agents=tuple(grants),
        horizon_days=doc.int_("horizon_days", default=30, minimum=1, maximum=365),
        min_notice_hours=doc.int_("min_notice_hours", default=2, minimum=0, maximum=720))
    doc.reject_unknown()
    return spec
