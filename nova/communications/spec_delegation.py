"""Which agents may hand work to which, and the limits on handing it on.

Delegation is a grant, like calling: absent, no agent may delegate. A route names the
agents one agent may hand work to; nothing else is reachable, whatever a model asks. The
limits are what keep a chain of agents from becoming a loop: a maximum depth from the first
delegation, a cap on open work per receiving agent, and a deadline on every delegation.
Cycles are refused outright — an agent never receives work from a chain it is already in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from nova._fields import Doc
from nova.errors import SpecError


@dataclass(frozen=True)
class DelegationSpec:
    #: from agent -> the agents it may hand work to.
    routes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Delegations in a chain started by one agent's own work. 1 means no re-delegation.
    max_depth: int = 3
    #: Open delegations one agent may hold at once; beyond it, new ones are refused.
    max_open_per_agent: int = 10
    default_deadline_hours: int = 24
    max_deadline_hours: int = 168
    #: How often the runtime may retry a delegated task that failed.
    max_retries: int = 1

    @property
    def declared(self) -> bool:
        return bool(self.routes)

    def may(self, sender: str, receiver: str) -> bool:
        return receiver in self.routes.get(sender, ())

    def participants(self) -> set[str]:
        return set(self.routes) | {to for targets in self.routes.values() for to in targets}

    def to_dict(self) -> dict[str, Any]:
        if not self.routes:
            return {}
        return {"routes": {k: list(v) for k, v in self.routes.items()},
                "max_depth": self.max_depth, "max_open_per_agent": self.max_open_per_agent,
                "default_deadline_hours": self.default_deadline_hours,
                "max_deadline_hours": self.max_deadline_hours, "max_retries": self.max_retries}


def parse_delegation(doc: Optional[Doc]) -> DelegationSpec:
    if doc is None:
        return DelegationSpec()
    raw = doc._raw("routes") or []
    if not isinstance(raw, list):
        raise SpecError("must be a list of {from, to: [...]}",
                        field="communications.delegation.routes", source=doc.source)
    routes: dict[str, tuple[str, ...]] = {}
    for index, item in enumerate(raw):
        where = f"communications.delegation.routes[{index}]"
        route = Doc(item, source=doc.source, prefix=where)
        sender = route.identifier("from")
        targets = tuple(route.str_list("to"))
        route.reject_unknown()
        if not targets:
            raise SpecError("is required: the agents this one may hand work to",
                            field=f"{where}.to", source=doc.source)
        if sender in targets:
            raise SpecError("an agent cannot delegate to itself", field=f"{where}.to",
                            source=doc.source)
        if sender in routes:
            raise SpecError(f"{sender!r} has two routes; list every target in one",
                            field=where, source=doc.source)
        routes[sender] = targets
    spec = DelegationSpec(
        routes=routes,
        max_depth=doc.int_("max_depth", default=3, minimum=1, maximum=10),
        max_open_per_agent=doc.int_("max_open_per_agent", default=10, minimum=1, maximum=1000),
        default_deadline_hours=doc.int_("default_deadline_hours", default=24, minimum=1,
                                        maximum=720),
        max_deadline_hours=doc.int_("max_deadline_hours", default=168, minimum=1, maximum=720),
        max_retries=doc.int_("max_retries", default=1, minimum=0, maximum=5),
    )
    doc.reject_unknown()
    if spec.default_deadline_hours > spec.max_deadline_hours:
        raise SpecError("default_deadline_hours is longer than max_deadline_hours",
                        field="communications.delegation", source=doc.source)
    return spec
