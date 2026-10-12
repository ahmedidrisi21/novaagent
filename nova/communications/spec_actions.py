"""What a tenant declares about acting on a finished call, and about the email it may send.

After a call, the reading model **proposes** actions; it never chooses how they are
handled. The tenant fixes that here, per action kind, as a tier:

* ``auto`` — carried out without a person, still through every gate (consent, hours, caps).
* ``approve`` — waits for a person, who approves the call's plan as it was shown.
* ``never`` — recorded as a draft for a person to handle outside NOVA; no approval runs it.

A purpose lists which kinds a call of that purpose may propose; a kind the tenant did not
configure is never proposed. The commercial kind — discounts, refunds, contract or payment
changes — may only ever be ``never``. A spoken opt-out is not an action kind: it is honoured
on every call, without a model and without a tier.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from nova._fields import Doc
from nova.communications.providers import Kind, get_provider
from nova.errors import SpecError

TIERS = ("auto", "approve", "never")


@dataclass(frozen=True)
class ActionKind:
    """One kind of post-call action: what the model is told it means, and its rules."""

    name: str
    #: What a call must have established for the model to propose it. Shown to the model.
    meaning: str
    default_tier: str
    #: Tiers a tenant may choose. The commercial kind allows only ``never``.
    tiers: tuple[str, ...] = TIERS
    #: Needs an agent to hand the work to.
    needs_assignee: bool = False
    #: Needs ``when``: a local date and time.
    needs_when: bool = False


KINDS: Mapping[str, ActionKind] = {k.name: k for k in (
    ActionKind("escalate", "the caller asked for a person, or complained", "auto",
               needs_assignee=True),
    ActionKind("task", "we committed to a next step the caller is waiting on", "auto",
               needs_assignee=True),
    ActionKind("callback", "the caller agreed a time to be called back", "auto",
               needs_when=True),
    ActionKind("follow_up_email", "the caller asked us to send them details in writing",
               "approve"),
    ActionKind("flag", "the agent promised something it may not be allowed to promise",
               "auto", needs_assignee=True),
    ActionKind("handoff", "the caller is ready for another agent to take forward (for "
               "example a qualified lead for sales)", "auto", needs_assignee=True),
    ActionKind("commercial_change",
               "the caller asked for a discount, refund, contract or payment change",
               "never", tiers=("never",)),
)}

#: Placeholders an email template may use. Everything else is refused when the bundle loads,
#: so a template cannot reach for a field nobody reviewed.
TEMPLATE_FIELDS = ("name", "summary", "detail", "agent")


@dataclass(frozen=True)
class ActionRule:
    kind: str
    tier: str
    #: The agent work goes to, for kinds that create work.
    assignee: str = ""
    #: Email kinds: the tenant's template. ``{summary}`` and ``{detail}`` are filled from
    #: the call, redacted; ``{name}`` from the contact record.
    subject: str = ""
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "tier": self.tier}
        for key in ("assignee", "subject", "body"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


EMAIL_APPROVAL = ("always", "first_contact", "never")
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


@dataclass(frozen=True)
class EmailGrant:
    """One agent's grant to write emails itself: for which purposes, to which contact
    groups, how many a day, and whether a person approves each one first."""

    agent: str
    purposes: tuple[str, ...]
    contact_groups: tuple[str, ...]
    approval: str = "always"
    max_per_day: int = 20

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "purposes": list(self.purposes),
                "contact_groups": list(self.contact_groups), "approval": self.approval,
                "max_per_day": self.max_per_day}


@dataclass(frozen=True)
class EmailSpec:
    """The sender. Credentials live with the gateway (``RESEND_API_KEY``)."""

    provider: str = ""
    sender: str = ""
    reply_to: str = ""
    #: Follow-up emails sent without a person approving, per contact per day.
    max_auto_per_contact_per_day: int = 1
    #: Days the sent text is kept. 0 keeps only the subject and delivery state.
    keep_body_days: int = 30
    #: A domain whose mail the provider receives (Resend inbound). Set, every email's
    #: reply address is ``reply+<signed id>@<domain>`` and replies reach the agent.
    reply_domain: str = ""
    #: Agents that may write their own emails to contacts, and on what terms.
    agents: tuple[EmailGrant, ...] = ()

    def grant(self, agent: str) -> Optional[EmailGrant]:
        return next((g for g in self.agents if g.agent == agent), None)

    def to_dict(self) -> dict[str, Any]:
        if not self.provider:
            return {}
        return {"provider": self.provider, "from": self.sender, "reply_to": self.reply_to,
                "max_auto_per_contact_per_day": self.max_auto_per_contact_per_day,
                "keep_body_days": self.keep_body_days, "reply_domain": self.reply_domain,
                "agents": [g.to_dict() for g in self.agents]}


@dataclass(frozen=True)
class ActionsSpec:
    rules: Mapping[str, ActionRule] = field(default_factory=dict)
    #: Callback calls an auto-tier callback may chain: call -> callback -> callback stops
    #: at this depth and waits for a person.
    max_chain_depth: int = 2
    #: Auto callbacks per contact per day.
    max_auto_callbacks_per_contact_per_day: int = 1

    def rule(self, kind: str) -> Optional[ActionRule]:
        return self.rules.get(kind)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [r.to_dict() for r in self.rules.values()],
                "max_chain_depth": self.max_chain_depth,
                "max_auto_callbacks_per_contact_per_day":
                    self.max_auto_callbacks_per_contact_per_day}


def _template(doc: Doc, key: str, where: str) -> str:
    text = doc.str_(key)
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}
    except ValueError as exc:
        raise SpecError(f"is not a valid template: {exc}", field=f"{where}.{key}",
                        source=doc.source) from exc
    unknown = sorted(names - set(TEMPLATE_FIELDS))
    if unknown:
        raise SpecError(f"uses {', '.join(unknown)}; a template may use only "
                        f"{', '.join('{' + f + '}' for f in TEMPLATE_FIELDS)}",
                        field=f"{where}.{key}", source=doc.source)
    return text


def parse_actions(doc: Optional[Doc]) -> ActionsSpec:
    if doc is None:
        return ActionsSpec()
    raw = doc._raw("rules") or {}
    if not isinstance(raw, Mapping):
        raise SpecError("must be a mapping of action kind to its rule",
                        field="communications.actions.rules", source=doc.source)
    rules: dict[str, ActionRule] = {}
    for name, value in raw.items():
        where = f"communications.actions.rules.{name}"
        kind = KINDS.get(str(name))
        if kind is None:
            raise SpecError(f"{name!r} is not an action kind; known: {', '.join(KINDS)}",
                            field=where, source=doc.source)
        rule_doc = Doc(value if isinstance(value, Mapping) else {"tier": value},
                       source=doc.source, prefix=where)
        tier = rule_doc.choice("tier", TIERS, default=kind.default_tier) or kind.default_tier
        if tier not in kind.tiers:
            raise SpecError(f"may only be {' or '.join(kind.tiers)}: an agent never executes "
                            "this kind", field=f"{where}.tier", source=doc.source)
        rule = ActionRule(kind=kind.name, tier=tier, assignee=rule_doc.str_("assignee"),
                          subject=_template(rule_doc, "subject", where),
                          body=_template(rule_doc, "body", where))
        rule_doc.reject_unknown()
        if kind.needs_assignee and tier != "never" and not rule.assignee:
            raise SpecError("is required: the agent this work goes to",
                            field=f"{where}.assignee", source=doc.source)
        if kind.name == "follow_up_email" and tier != "never" and not (rule.subject and rule.body):
            raise SpecError("subject and body are required: the approved template",
                            field=where, source=doc.source)
        rules[kind.name] = rule
    spec = ActionsSpec(
        rules=rules,
        max_chain_depth=doc.int_("max_chain_depth", default=2, minimum=0, maximum=5),
        max_auto_callbacks_per_contact_per_day=doc.int_(
            "max_auto_callbacks_per_contact_per_day", default=1, minimum=0, maximum=10),
    )
    doc.reject_unknown()
    return spec


def parse_email(doc: Optional[Doc], *, allow_test: bool) -> EmailSpec:
    if doc is None:
        return EmailSpec()
    provider_id = doc.str_("provider", required=True)
    provider = get_provider(provider_id)
    if provider is None or provider.kind is not Kind.EMAIL:
        raise SpecError(f"{provider_id!r} is not a known email provider",
                        field="communications.email.provider", source=doc.source)
    if provider.test_only and not allow_test:
        raise SpecError(f"{provider_id!r} sends nothing; set allow_test_providers: true to use "
                        "it outside production", field="communications.email.provider",
                        source=doc.source)
    sender = doc.str_("from", required=True)
    if "@" not in sender:
        raise SpecError("must be the sender address, e.g. Acme <support@acme.example>",
                        field="communications.email.from", source=doc.source)
    reply_domain = doc.str_("reply_domain").lower()
    if reply_domain and not _DOMAIN.match(reply_domain):
        raise SpecError("must be a domain the email provider receives mail for",
                        field="communications.email.reply_domain", source=doc.source)
    raw = doc._raw("agents") or []
    if not isinstance(raw, list):
        raise SpecError("must be a list", field="communications.email.agents", source=doc.source)
    grants = []
    for index, item in enumerate(raw):
        where = f"communications.email.agents[{index}]"
        grant_doc = Doc(item, source=doc.source, prefix=where)
        grant = EmailGrant(
            agent=grant_doc.identifier("agent"), purposes=tuple(grant_doc.str_list("purposes")),
            contact_groups=tuple(grant_doc.str_list("contact_groups")),
            approval=grant_doc.choice("approval", EMAIL_APPROVAL, default="always") or "always",
            max_per_day=grant_doc.int_("max_per_day", default=20, minimum=1, maximum=1000))
        grant_doc.reject_unknown()
        if not (grant.purposes and grant.contact_groups):
            raise SpecError("purposes and contact_groups are required: an agent that may email "
                            "anyone about anything is not a grant", field=where, source=doc.source)
        if any(g.agent == grant.agent for g in grants):
            raise SpecError(f"{grant.agent!r} has two email grants", field=where, source=doc.source)
        grants.append(grant)
    spec = EmailSpec(
        provider=provider_id, sender=sender, reply_to=doc.str_("reply_to"),
        max_auto_per_contact_per_day=doc.int_("max_auto_per_contact_per_day", default=1,
                                              minimum=0, maximum=10),
        keep_body_days=doc.int_("keep_body_days", default=30, minimum=0, maximum=3650),
        reply_domain=reply_domain, agents=tuple(grants),
    )
    doc.reject_unknown()
    return spec


def check_purpose_actions(actions: tuple[str, ...], spec: ActionsSpec, where: str,
                          source) -> None:
    for name in actions:
        if name not in KINDS:
            raise SpecError(f"{name!r} is not an action kind", field=where, source=source)
        if spec.rule(name) is None:
            raise SpecError(f"{name!r} has no rule in communications.actions.rules; say "
                            "what tier it runs at before a purpose may propose it",
                            field=where, source=source)
