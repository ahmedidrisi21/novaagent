"""What a tenant declares about phone calls: providers, numbers, purposes, and who may call.

An agent gets nothing by default. A profile here is the grant: which purposes it may call
for, which contact groups it may reach, from which number, through which carrier and voice
engine, and how many calls it may hold. A model changing its mind does not widen any of it,
and neither does changing the voice engine — the grant and the voice are separate fields
on purpose.

**No secret is expressible here**, for the same reason as ``channels.yaml``: this file is in
version control. Carrier and voice credentials live with the gateway, never in a bundle and
never in an agent's environment.

Recording is deliberately absent. It needs consent capture and pause/resume, which do not
exist yet, so an unknown ``recording:`` key is refused rather than accepted and ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from nova._fields import Doc
from nova.communications.providers import Kind, get_provider
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


@dataclass(frozen=True)
class Purpose:
    id: str
    description: str = ""
    #: A marketing purpose needs prior express written consent on top of AI-voice consent.
    marketing: bool = False
    #: What the voice engine is told the call is for and how to conduct it. Tenant text,
    #: reviewed like any other prompt; the model never writes its own.
    instructions: str = ""

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

    def to_dict(self) -> dict[str, Any]:
        return {
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

    @property
    def declared(self) -> bool:
        return bool(self.profiles or self.purposes)

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
    )
    doc.reject_unknown()
    return profile


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

    purposes: dict[str, Purpose] = {}
    raw_purposes = doc._raw("purposes") or []
    if not isinstance(raw_purposes, list):
        raise SpecError("must be a list", field="communications.purposes", source=source)
    for index, item in enumerate(raw_purposes):
        pdoc = Doc(item, source=source, prefix=f"communications.purposes[{index}]", env=env)
        purpose = Purpose(
            id=pdoc.identifier("id"),
            description=pdoc.str_("description"),
            marketing=pdoc.bool_("marketing", default=False),
            instructions=pdoc.str_("instructions"),
        )
        pdoc.reject_unknown()
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
    for profile in spec.profiles:
        if profile.agent not in known:
            raise SpecError(
                f"calling profile for {profile.agent!r}, which this bundle does not define",
                field="communications.agents",
            )
