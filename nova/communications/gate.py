"""Whether one outbound call may be placed. One function, and every gap denies.

The gateway gathers the facts — the contact record, the consents it holds, whether it is
suppressed, how many calls the agent is holding — and this function judges them. Keeping
the judgement pure means the dashboard can explain a refusal with the same code that made
it, and a test can walk every rule without a carrier.

**Order is the policy.** A suppressed contact is refused before anything else is checked,
including approval: an approval screen for a call that may never be placed is an invitation
to approve it. Consent and calling hours come next, and the agent's own caps last, because a
cap refusal means "later" while the others mean "not this contact".

Every unknown is a denial: a contact with no timezone cannot be checked against calling
hours, so it is not called.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from nova.communications.spec import CommunicationsSpec

ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Contact:
    """A callable person, as the tenant recorded them. Agents name it; they never edit it."""

    contact_id: str
    phone: str
    #: IANA zone, e.g. ``America/Chicago``. Empty means unknown, which denies.
    timezone: str
    groups: tuple[str, ...] = ()
    #: How a follow-up email addresses them, and where it goes. A call never changes
    #: either: an address spoken on a call is not an address on file.
    name: str = ""
    email: str = ""


@dataclass(frozen=True)
class CallFacts:
    """Everything the gate needs, gathered by the gateway at request time."""

    agent: str
    purpose: str
    contact: Optional[Contact]
    consents: frozenset[str]
    suppressed: bool
    active_calls: int
    calls_today: int
    #: Whether this agent has completed a call with this contact before.
    prior_contact: bool
    #: The moment of the request, timezone-aware.
    now: datetime
    #: Why the tenant's budget allows no new call, or '' when it does.
    budget: str = ""


@dataclass(frozen=True)
class CallDecision:
    effect: str
    reason: str
    rule: str

    @property
    def allowed(self) -> bool:
        return self.effect == ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {"effect": self.effect, "reason": self.reason, "rule": self.rule}


def _deny(rule: str, reason: str) -> CallDecision:
    return CallDecision(DENY, reason, rule)


def _local_minutes(now: datetime, zone: str) -> Optional[int]:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        local = now.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return local.hour * 60 + local.minute


def decide_call(spec: CommunicationsSpec, facts: CallFacts) -> CallDecision:
    try:
        return _decide(spec, facts)
    except Exception as exc:  # noqa: BLE001 — a gate that raises must still say no
        return _deny("error", f"the call gate failed and refused by default: {exc}")


def _decide(spec: CommunicationsSpec, facts: CallFacts) -> CallDecision:
    profile = spec.profile(facts.agent)
    if profile is None or not profile.outbound:
        return _deny("no_grant", f"agent {facts.agent!r} has no outbound calling grant")

    purpose = spec.purposes.get(facts.purpose)
    if purpose is None or facts.purpose not in profile.purposes:
        return _deny(
            "purpose", f"agent {facts.agent!r} may not call for purpose {facts.purpose!r}"
        )

    contact = facts.contact
    if contact is None:
        return _deny("contact", "the contact does not exist in this tenant")
    if not set(contact.groups) & set(profile.contact_groups):
        return _deny(
            "contact_group",
            f"contact {contact.contact_id!r} is in none of the groups this agent may call",
        )

    if facts.suppressed:
        return _deny("suppressed", f"contact {contact.contact_id!r} has opted out of calls")

    missing = [kind for kind in purpose.required_consents() if kind not in facts.consents]
    if missing:
        return _deny(
            "consent",
            f"contact {contact.contact_id!r} has no {', '.join(missing)} consent on record "
            f"for purpose {purpose.id!r}",
        )

    if not contact.timezone:
        return _deny("calling_hours", "the contact's timezone is unknown, so calling hours "
                     "cannot be checked")
    local = _local_minutes(facts.now, contact.timezone)
    if local is None:
        return _deny("calling_hours", f"unknown timezone {contact.timezone!r}")
    if not (spec.window_start <= local < spec.window_end):
        return _deny("calling_hours", "outside calling hours in the contact's local time")

    if facts.active_calls >= profile.max_concurrent_calls:
        return _deny(
            "concurrency",
            f"agent is already holding {facts.active_calls} of "
            f"{profile.max_concurrent_calls} allowed concurrent calls",
        )
    if facts.calls_today >= profile.max_calls_per_day:
        return _deny(
            "daily_calls",
            f"agent has placed {facts.calls_today} of {profile.max_calls_per_day} calls "
            "allowed today (UTC)",
        )
    if facts.budget:
        return _deny("budget", facts.budget)

    if profile.approval == "always":
        return CallDecision(REQUIRE_APPROVAL, "this agent's calls need human approval", "approval")
    if profile.approval == "first_contact" and not facts.prior_contact:
        return CallDecision(
            REQUIRE_APPROVAL, "first call from this agent to this contact needs approval",
            "approval",
        )
    return CallDecision(ALLOW, "every check passed", "allow")
