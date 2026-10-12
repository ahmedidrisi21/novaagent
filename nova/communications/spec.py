"""What a tenant declares about phone calls: providers, numbers, purposes, and who may call.

An agent gets nothing by default. A profile here is the grant: which purposes it may call
for, which contact groups it may reach, from which number, through which carrier and voice
engine, and how many calls it may hold. A model changing its mind does not widen any of it,
and neither does changing the voice engine — the grant and the voice are separate fields
on purpose.

**No secret is expressible here**, for the same reason as ``channels.yaml``: this file is in
version control. Carrier and voice credentials live with the gateway, never in a bundle and
never in an agent's environment.

Recording is off unless a profile turns it on, and turning it on requires a written legal
basis: the field is the record that somebody decided, not a checkbox. Consent is then asked
on every call, by the carrier, before any model is on the line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from nova._fields import Doc
from nova.communications.providers import Kind, get_provider
from nova.communications.spec_budget import BudgetSpec, parse_budget
from nova.communications.spec_delegation import DelegationSpec, parse_delegation
from nova.communications.spec_actions import (
    ActionsSpec,
    EmailSpec,
    check_purpose_actions,
    parse_actions,
    parse_email,
)
from nova.errors import SpecError

COMMUNICATIONS_FILE = "communications.yaml"

#: E.164: a plus, a country code, at most 15 digits in all.
E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

FORBIDDEN_KEYS = frozenset(
    {"token", "api_key", "secret", "password", "credential", "auth_token", "account_sid"}
)

APPROVAL_MODES = ("always", "first_contact", "never")

#: Consent kinds a contact can hold. ``ai_voice`` is needed for every outbound call placed
#: by an agent; ``marketing_written`` additionally for a marketing purpose.
CONSENT_AI_VOICE = "ai_voice"
CONSENT_MARKETING_WRITTEN = "marketing_written"
CONSENT_KINDS = (CONSENT_AI_VOICE, CONSENT_MARKETING_WRITTEN)


FIELD_KINDS = ("text", "number", "boolean", "date", "choice")
_ATTRIBUTE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


@dataclass(frozen=True)
class FieldSpec:
    """One thing a call of this purpose should establish, and where it may be written.

    ``contact_attribute`` names the contact field a value may update; absent, the value is
    kept on the call record only. ``auto_apply`` is the confidence at or above which a
    value updates the contact without a person reviewing it — it never overwrites a value
    a person entered, whatever the confidence.
    """

    name: str
    kind: str = "text"
    description: str = ""
    options: tuple[str, ...] = ()
    contact_attribute: str = ""
    auto_apply: float = 0.9

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "kind": self.kind,
                               "description": self.description, "auto_apply": self.auto_apply}
        if self.options:
            out["options"] = list(self.options)
        if self.contact_attribute:
            out["contact_attribute"] = self.contact_attribute
        return out


def _field(doc: Doc, where: str) -> FieldSpec:
    name = doc.str_("name", required=True)
    if not _ATTRIBUTE.match(name):
        raise SpecError("must be lowercase letters, digits and underscores",
                        field=f"{where}.name", source=doc.source)
    kind = doc.choice("kind", FIELD_KINDS, default="text") or "text"
    options = tuple(doc.str_list("options"))
    if (kind == "choice") != bool(options):
        raise SpecError("options are required for kind choice, and only for it",
                        field=f"{where}.options", source=doc.source)
    attribute = doc.str_("contact_attribute")
    if attribute and not _ATTRIBUTE.match(attribute):
        raise SpecError("must be lowercase letters, digits and underscores",
                        field=f"{where}.contact_attribute", source=doc.source)
    raw = doc._raw("auto_apply")
    if raw is None:
        auto = 0.9
    elif isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 1:
        raise SpecError("must be a confidence between 0 and 1 (1 means always review)",
                        field=f"{where}.auto_apply", source=doc.source)
    else:
        auto = float(raw)
    spec = FieldSpec(name=name, kind=kind, description=doc.str_("description"),
                     options=options, contact_attribute=attribute, auto_apply=auto)
    doc.reject_unknown()
    return spec


@dataclass(frozen=True)
class ExtractionSpec:
    """The model that reads finished calls. Its key lives with the gateway."""

    llm_base_url: str = ""
    llm_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass(frozen=True)
class Purpose:
    id: str
    description: str = ""
    #: A marketing purpose needs prior express written consent on top of AI-voice consent.
    marketing: bool = False
    #: What the voice engine is told the call is for and how to conduct it. Tenant text,
    #: reviewed like any other prompt; the model never writes its own.
    instructions: str = ""
    #: What a call of this purpose should establish. Empty means no extraction.
    fields: tuple[FieldSpec, ...] = ()
    #: Post-call action kinds a call of this purpose may propose. Their tiers are the
    #: tenant's, in ``communications.actions``.
    actions: tuple[str, ...] = ()

    def required_consents(self) -> tuple[str, ...]:
        if self.marketing:
            return (CONSENT_AI_VOICE, CONSENT_MARKETING_WRITTEN)
        return (CONSENT_AI_VOICE,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "marketing": self.marketing,
            "instructions": self.instructions,
            "fields": [f.to_dict() for f in self.fields],
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class VoiceChoice:
    primary: str
    fallbacks: tuple[str, ...] = ()
    language: str = ""
    voice: str = ""

    @property
    def ordered(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "fallbacks": list(self.fallbacks),
            "language": self.language,
            "voice": self.voice,
        }


RECORDING_REFUSAL = ("continue", "end")

DEFAULT_CONSENT_PROMPT = (
    "May we record this call? Press 1 or say yes to allow it. Press 2 or say no to "
    "continue without recording."
)


@dataclass(frozen=True)
class RecordingSpec:
    """Whether an agent's calls may be recorded, and on what terms."""

    enabled: bool = False
    #: Why recording is lawful for this purpose and market, in the tenant's words.
    legal_basis: str = ""
    retention_days: int = 90
    #: What happens when the person says no: carry on unrecorded, or end politely.
    on_refusal: str = "continue"

    def to_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "legal_basis": self.legal_basis,
                "retention_days": self.retention_days, "on_refusal": self.on_refusal}


@dataclass(frozen=True)
class CallingProfile:
    """One agent's phone grant."""

    agent: str
    telephony: str
    caller_id: str
    voice: VoiceChoice
    purposes: tuple[str, ...]
    contact_groups: tuple[str, ...]
    outbound: bool = True
    approval: str = "always"
    max_call_seconds: int = 300
    max_concurrent_calls: int = 1
    max_calls_per_day: int = 20
    recording: RecordingSpec = field(default_factory=RecordingSpec)
    #: Days a redacted transcript is kept after extraction. 0, the default, deletes it as
    #: soon as the call's fields are extracted: the structured record is what the
    #: business needs, and a transcript nobody reads is only a liability.
    keep_transcript_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "recording": self.recording.to_dict(),
            "keep_transcript_days": self.keep_transcript_days,
            "agent": self.agent,
            "outbound": self.outbound,
            "telephony": self.telephony,
            "caller_id": self.caller_id,
            "voice": self.voice.to_dict(),
            "purposes": list(self.purposes),
            "contact_groups": list(self.contact_groups),
            "approval": self.approval,
            "max_call_seconds": self.max_call_seconds,
            "max_concurrent_calls": self.max_concurrent_calls,
            "max_calls_per_day": self.max_calls_per_day,
        }


@dataclass(frozen=True)
class CascadeSpec:
    """The cascaded engine's reply model and speech models. Keys live with the gateway."""

    llm_base_url: str = ""
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    cartesia_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


#: The engine whose voice names a Cartesia voice id and whose replies need a model.
CASCADED_ENGINE = "deepgram-cartesia"


@dataclass(frozen=True)
class CommunicationsSpec:
    voice_providers: tuple[str, ...] = ()
    telephony_providers: tuple[str, ...] = ()
    caller_ids: tuple[str, ...] = ()
    #: Recipient-local hours outbound calls may start in, as minutes after midnight.
    window_start: int = 8 * 60
    window_end: int = 21 * 60
    purposes: Mapping[str, Purpose] = field(default_factory=dict)
    profiles: tuple[CallingProfile, ...] = ()
    allow_test_providers: bool = False
    #: Spoken by the carrier, word for word, before any model is on the line.
    disclosure: str = ""
    cascade: CascadeSpec = field(default_factory=CascadeSpec)
    #: The public HTTPS address carriers and voice engines call back on. Deployment
    #: configuration, so it can be overridden on the command line.
    public_url: str = ""
    #: Asked by the carrier before recording, when an agent's profile records.
    consent_prompt: str = DEFAULT_CONSENT_PROMPT
    extraction: ExtractionSpec = field(default_factory=ExtractionSpec)
    actions: ActionsSpec = field(default_factory=ActionsSpec)
    email: EmailSpec = field(default_factory=EmailSpec)
    delegation: DelegationSpec = field(default_factory=DelegationSpec)
    budget: BudgetSpec = field(default_factory=BudgetSpec)

    @property
    def declared(self) -> bool:
        return bool(self.profiles or self.purposes or self.delegation.declared)

    def profile(self, agent: str) -> Optional[CallingProfile]:
        for profile in self.profiles:
            if profile.agent == agent:
                return profile
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_providers": list(self.voice_providers),
            "telephony_providers": list(self.telephony_providers),
            "caller_ids": list(self.caller_ids),
            "calling_hours": {"start": _fmt(self.window_start), "end": _fmt(self.window_end)},
            "allow_test_providers": self.allow_test_providers,
            "disclosure": self.disclosure,
            "cascade": self.cascade.to_dict(),
            "public_url": self.public_url,
            "consent_prompt": self.consent_prompt,
            "extraction": self.extraction.to_dict(),
            "actions": self.actions.to_dict(),
            "email": self.email.to_dict(),
            "delegation": self.delegation.to_dict(),
            "budget": self.budget.to_dict(),
            "purposes": [p.to_dict() for p in self.purposes.values()],
            "agents": [p.to_dict() for p in self.profiles],
        }


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _refuse_secrets(data: Any, *, source: Optional[Path], path: str = "") -> None:
    if isinstance(data, Mapping):
        for key, value in data.items():
            where = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_KEYS:
                raise SpecError(
                    "credentials do not belong in a bundle — they live with the "
                    "communications gateway, never in version control",
                    field=where,
                    source=source,
                )
            _refuse_secrets(value, source=source, path=where)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            _refuse_secrets(item, source=source, path=f"{path}[{index}]")


def _hhmm(doc: Doc, key: str, default: int) -> int:
    value = doc.str_(key)
    if not value:
        return default
    match = _HHMM.match(value)
    if not match:
        raise SpecError(
            f"{value!r} is not a time — write HH:MM, 24-hour",
            field=f"communications.calling_hours.{key}",
            source=doc.source,
        )
    return int(match.group(1)) * 60 + int(match.group(2))


def _providers(doc: Doc, key: str, kind: Kind, allow_test: bool) -> tuple[str, ...]:
    ids = tuple(doc.str_list(key))
    for provider_id in ids:
        provider = get_provider(provider_id)
        if provider is None or provider.kind is not kind:
            raise SpecError(
                f"{provider_id!r} is not a known {kind.value} provider",
                field=f"communications.{key}",
                source=doc.source,
            )
        if provider.test_only and not allow_test:
            raise SpecError(
                f"{provider_id!r} places no real calls; set allow_test_providers: true "
                "to use it outside production",
                field=f"communications.{key}",
                source=doc.source,
            )
    return ids


def _profile(doc: Doc, index: int, spec_so_far: Mapping[str, Any]) -> CallingProfile:
    where = f"communications.agents[{index}]"

    def fail(key: str, message: str) -> SpecError:
        return SpecError(message, field=f"{where}.{key}", source=doc.source)

    agent = doc.identifier("agent")
    telephony = doc.str_("telephony", required=True)
    if telephony not in spec_so_far["telephony_providers"]:
        raise fail("telephony", f"{telephony!r} is not in this tenant's telephony_providers")

    caller_id = doc.str_("caller_id", required=True)
    if caller_id not in spec_so_far["caller_ids"]:
        raise fail(
            "caller_id",
            f"{caller_id!r} is not one of this tenant's caller_ids — an agent may only "
            "call from a number the tenant declared",
        )

    voice_doc = doc.child("voice", required=True)
    voice = VoiceChoice(
        primary=voice_doc.str_("primary", required=True),
        fallbacks=tuple(voice_doc.str_list("fallbacks")),
        language=voice_doc.str_("language"),
        voice=voice_doc.str_("voice"),
    )
    voice_doc.reject_unknown()
    if CASCADED_ENGINE in voice.ordered and not voice.voice:
        raise fail("voice.voice", f"is required with {CASCADED_ENGINE!r}: the Cartesia voice id")
    for engine in voice.ordered:
        if engine not in spec_so_far["voice_providers"]:
            raise fail("voice", f"{engine!r} is not in this tenant's voice_providers")
    if len(set(voice.ordered)) != len(voice.ordered):
        raise fail("voice.fallbacks", "repeats the primary or another fallback")

    purposes = tuple(doc.str_list("purposes"))
    if not purposes:
        raise fail("purposes", "is required — an agent that may call for any reason is not a grant")
    for purpose in purposes:
        if purpose not in spec_so_far["purposes"]:
            raise fail("purposes", f"{purpose!r} is not a declared purpose")

    groups = tuple(doc.str_list("contact_groups"))
    if not groups:
        raise fail("contact_groups", "is required — name the contact groups this agent may call")

    profile = CallingProfile(
        agent=agent,
        outbound=doc.bool_("outbound", default=True),
        telephony=telephony,
        caller_id=caller_id,
        voice=voice,
        purposes=purposes,
        contact_groups=groups,
        approval=doc.choice("approval", APPROVAL_MODES, default="always") or "always",
        max_call_seconds=doc.int_("max_call_seconds", default=300, minimum=30, maximum=3600),
        max_concurrent_calls=doc.int_("max_concurrent_calls", default=1, minimum=1, maximum=100),
        max_calls_per_day=doc.int_("max_calls_per_day", default=20, minimum=1, maximum=10000),
        recording=_recording(doc.child("recording"), where),
        keep_transcript_days=doc.int_("keep_transcript_days", default=0, minimum=0, maximum=3650),
    )
    doc.reject_unknown()
    return profile


def _recording(doc: Optional[Doc], where: str) -> RecordingSpec:
    if doc is None:
        return RecordingSpec()
    spec = RecordingSpec(
        enabled=doc.bool_("enabled", default=False),
        legal_basis=doc.str_("legal_basis"),
        retention_days=doc.int_("retention_days", default=90, minimum=1, maximum=3650),
        on_refusal=doc.choice("on_refusal", RECORDING_REFUSAL, default="continue") or "continue",
    )
    doc.reject_unknown()
    if spec.enabled and len(spec.legal_basis) < 20:
        raise SpecError(
            "is required to record calls: say why recording is lawful for this purpose and "
            "market (and who reviewed it), in at least a sentence",
            field=f"{where}.recording.legal_basis", source=doc.source,
        )
    return spec


def parse_communications(
    data: Any, *, source: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> CommunicationsSpec:
    _refuse_secrets(data, source=source)
    root = Doc(data, source=source, env=env)
    doc = root.child("communications")
    root.reject_unknown()
    if doc is None:
        return CommunicationsSpec()

    allow_test = doc.bool_("allow_test_providers", default=False)
    voice = _providers(doc, "voice_providers", Kind.VOICE, allow_test)
    telephony = _providers(doc, "telephony_providers", Kind.TELEPHONY, allow_test)

    caller_ids = tuple(doc.str_list("caller_ids"))
    for number in caller_ids:
        if not E164.match(number):
            raise SpecError(
                f"{number!r} is not an E.164 number (+ and country code, digits only)",
                field="communications.caller_ids",
                source=source,
            )

    hours = doc.child("calling_hours")
    start, end = 8 * 60, 21 * 60
    if hours is not None:
        start = _hhmm(hours, "start", start)
        end = _hhmm(hours, "end", end)
        hours.reject_unknown()
        if start >= end:
            raise SpecError(
                "start must be before end", field="communications.calling_hours", source=source
            )

    actions = parse_actions(doc.child("actions"))
    email = parse_email(doc.child("email"), allow_test=allow_test)
    delegation = parse_delegation(doc.child("delegation"))
    budget = parse_budget(doc.child("budget"), providers={
        *voice, *telephony, *((email.provider,) if email.provider else ())})
    purposes: dict[str, Purpose] = {}
    raw_purposes = doc._raw("purposes") or []
    if not isinstance(raw_purposes, list):
        raise SpecError("must be a list", field="communications.purposes", source=source)
    for index, item in enumerate(raw_purposes):
        pdoc = Doc(item, source=source, prefix=f"communications.purposes[{index}]", env=env)
        where = f"communications.purposes[{index}]"
        raw_fields = pdoc._raw("fields") or []
        if not isinstance(raw_fields, list):
            raise SpecError("must be a list", field=f"{where}.fields", source=source)
        fields = tuple(
            _field(Doc(item, source=source, prefix=f"{where}.fields[{i}]", env=env),
                   f"{where}.fields[{i}]")
            for i, item in enumerate(raw_fields)
        )
        if len({f.name for f in fields}) != len(fields):
            raise SpecError("field names must be unique", field=f"{where}.fields", source=source)
        purpose = Purpose(
            id=pdoc.identifier("id"),
            description=pdoc.str_("description"),
            marketing=pdoc.bool_("marketing", default=False),
            instructions=pdoc.str_("instructions"),
            fields=fields,
            actions=tuple(pdoc.str_list("actions")),
        )
        pdoc.reject_unknown()
        check_purpose_actions(purpose.actions, actions, f"{where}.actions", source)
        if purpose.id in purposes:
            raise SpecError(
                f"purpose {purpose.id!r} is declared twice",
                field=f"communications.purposes[{index}]",
                source=source,
            )
        purposes[purpose.id] = purpose

    so_far = {
        "voice_providers": voice,
        "telephony_providers": telephony,
        "caller_ids": caller_ids,
        "purposes": purposes,
    }
    profiles: list[CallingProfile] = []
    raw_agents = doc._raw("agents") or []
    if not isinstance(raw_agents, list):
        raise SpecError("must be a list", field="communications.agents", source=source)
    for index, item in enumerate(raw_agents):
        pdoc = Doc(item, source=source, prefix=f"communications.agents[{index}]", env=env)
        profile = _profile(pdoc, index, so_far)
        if any(p.agent == profile.agent for p in profiles):
            raise SpecError(
                f"agent {profile.agent!r} has two calling profiles",
                field=f"communications.agents[{index}].agent",
                source=source,
            )
        profiles.append(profile)
    disclosure = doc.str_("disclosure")
    public_url = doc.str_("public_url")
    consent_prompt = doc.str_("consent_prompt") or DEFAULT_CONSENT_PROMPT
    extraction = ExtractionSpec()
    extraction_doc = doc.child("extraction")
    if extraction_doc is not None:
        extraction = ExtractionSpec(llm_base_url=extraction_doc.str_("llm_base_url"),
                                    llm_model=extraction_doc.str_("llm_model"))
        extraction_doc.reject_unknown()
    if any(p.fields or p.actions for p in purposes.values()) and not (
        extraction.llm_base_url.startswith("https://") and extraction.llm_model
    ):
        raise SpecError("a purpose declares fields or actions, so extraction.llm_base_url "
                        "(https://) and "
                        "extraction.llm_model are required — the model that reads finished calls",
                        field="communications.extraction", source=source)
    if public_url and not public_url.startswith("https://"):
        raise SpecError("must be an https:// URL — carriers only call back over HTTPS",
                        field="communications.public_url", source=source)
    cascade = CascadeSpec()
    cascade_doc = doc.child("cascade")
    if cascade_doc is not None:
        cascade = CascadeSpec(
            llm_base_url=cascade_doc.str_("llm_base_url"),
            llm_model=cascade_doc.str_("llm_model"),
            stt_model=cascade_doc.str_("stt_model"),
            tts_model=cascade_doc.str_("tts_model"),
            cartesia_version=cascade_doc.str_("cartesia_version"),
        )
        cascade_doc.reject_unknown()
    doc.reject_unknown()
    if CASCADED_ENGINE in voice and not (cascade.llm_base_url and cascade.llm_model):
        raise SpecError(
            f"{CASCADED_ENGINE!r} needs cascade.llm_base_url and cascade.llm_model — the "
            "model that writes the agent's replies",
            field="communications.cascade",
            source=source,
        )
    if cascade.llm_base_url and not cascade.llm_base_url.startswith("https://"):
        raise SpecError("must be an https:// URL", field="communications.cascade.llm_base_url",
                        source=source)
    email_rule = actions.rule("follow_up_email")
    if email_rule is not None and email_rule.tier != "never" and not email.provider:
        raise SpecError("follow-up emails are configured, so communications.email (provider "
                        "and from) is required", field="communications.email", source=source)
    handoff = actions.rule("handoff")
    if handoff is not None and handoff.tier != "never":
        for profile in profiles:
            if any("handoff" in purposes[p].actions for p in profile.purposes) and \
                    not delegation.may(profile.agent, handoff.assignee):
                raise SpecError(
                    f"{profile.agent!r} may propose handoffs to {handoff.assignee!r}, so "
                    "communications.delegation needs a route between them",
                    field="communications.delegation.routes", source=source)
    if email.provider and not public_url:
        raise SpecError("is required to send email: every email carries an unsubscribe link "
                        "to it", field="communications.public_url", source=source)
    if profiles and not disclosure:
        raise SpecError(
            "is required once any agent may call — the words spoken before the agent, "
            "saying it is an AI and who it calls for",
            field="communications.disclosure",
            source=source,
        )

    return CommunicationsSpec(
        voice_providers=voice,
        telephony_providers=telephony,
        caller_ids=caller_ids,
        window_start=start,
        window_end=end,
        purposes=purposes,
        profiles=tuple(profiles),
        allow_test_providers=allow_test,
        disclosure=disclosure,
        cascade=cascade,
        public_url=public_url,
        consent_prompt=consent_prompt,
        extraction=extraction,
        actions=actions,
        email=email,
        delegation=delegation,
        budget=budget,
    )


def load_communications(
    bundle_root: Path, *, env: Optional[Mapping[str, str]] = None
) -> CommunicationsSpec:
    """Read ``communications.yaml``. Absent means no agent may call, which is the default."""
    import yaml

    path = bundle_root / COMMUNICATIONS_FILE
    if not path.is_file():
        return CommunicationsSpec()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"could not be read: {exc.strerror or exc}", source=path) from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"is not valid YAML: {exc}", source=path) from exc
    return parse_communications(data or {}, source=path, env=env)


def check_agents_exist(spec: CommunicationsSpec, agent_ids: Sequence[str]) -> None:
    known = set(agent_ids)
    if spec.budget.alert_assignee and spec.budget.alert_assignee not in known:
        raise SpecError(f"budget alerts go to {spec.budget.alert_assignee!r}, which this bundle "
                        "does not define", field="communications.budget.alert_assignee")
    for agent in sorted(spec.delegation.participants() - known):
        raise SpecError(f"delegation route names {agent!r}, which this bundle does not define",
                        field="communications.delegation.routes")
    for rule in spec.actions.rules.values():
        if rule.assignee and rule.assignee not in known:
            raise SpecError(
                f"{rule.kind} work goes to {rule.assignee!r}, which this bundle does not define",
                field=f"communications.actions.rules.{rule.kind}.assignee",
            )
    for profile in spec.profiles:
        if profile.agent not in known:
            raise SpecError(
                f"calling profile for {profile.agent!r}, which this bundle does not define",
                field="communications.agents",
            )
