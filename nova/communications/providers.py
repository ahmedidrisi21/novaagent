"""The voice engines and phone carriers a tenant may connect, and how sure we are of each.

Two kinds, deliberately separate. A **voice engine** hears and speaks; a **carrier** owns the
phone number and the call leg. A real call needs one of each, and they bill separately, so a
tenant chooses them separately.

Every entry carries the evidence rule from ``nova/channels/providers.py``: ``supported=None``
is "unknown" and is never rendered as yes. Capabilities taken from a vendor's documentation
are ``DECLARED`` — nobody here has run them. Only the test double is ``LOCAL_VALIDATED``,
because the contract tests exercise it end to end. Nothing is ``FIELD_VALIDATED`` until a
real call has been placed through it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional

from nova.channels.providers import Capability, Verification


class Kind(str, Enum):
    VOICE = "voice"
    TELEPHONY = "telephony"
    EMAIL = "email"


#: The questions asked of every voice engine. A routing decision may only rely on a
#: capability named here, so an adapter cannot be chosen for something nobody checked.
VOICE_CAPABILITIES: tuple[str, ...] = (
    # One model hears and speaks. False means a cascaded STT -> agent -> TTS pipeline.
    "speech_to_speech",
    # The agent behind the call can run tools while the caller waits.
    "tool_calls",
    # The caller can interrupt and the engine stops speaking.
    "barge_in",
    # The vendor connects to a phone carrier itself (SIP or a documented carrier
    # integration). False means a media bridge is needed.
    "native_phone",
    # Audio is processed inside the tenant's own cloud account.
    "audio_stays_in_tenant_cloud",
    # A text transcript is produced as part of the conversation, not reconstructed.
    "text_transcript",
)

#: The questions asked of every carrier.
TELEPHONY_CAPABILITIES: tuple[str, ...] = (
    "outbound",
    "inbound",
    "transfer",
    "answering_machine_detection",
    "recording",
    "signed_webhooks",
)

#: The questions asked of every email provider.
EMAIL_CAPABILITIES: tuple[str, ...] = (
    # A send retried with the same key sends once.
    "idempotent_send",
    "delivery_events",
    "bounce_events",
    "signed_webhooks",
)

CAPABILITY_NAMES: Mapping[Kind, tuple[str, ...]] = {
    Kind.VOICE: VOICE_CAPABILITIES,
    Kind.TELEPHONY: TELEPHONY_CAPABILITIES,
    Kind.EMAIL: EMAIL_CAPABILITIES,
}


@dataclass(frozen=True)
class CommsProvider:
    """One voice engine or carrier, described in the customer's vocabulary."""

    id: str
    kind: Kind
    label: str
    capabilities: Mapping[str, Capability] = field(default_factory=dict)
    verification: Verification = Verification.DECLARED
    #: Where the implementation lives, or where the vendor documents it. Empty means no
    #: adapter exists yet, which the dashboard says out loud.
    implementation: str = ""
    caveat: str = ""
    #: A test double places no real call. Refused in a bundle unless the tenant opts in,
    #: so a production tenant cannot ship an agent whose "calls" go nowhere.
    test_only: bool = False

    def capability(self, name: str) -> Capability:
        return self.capabilities.get(name, Capability(supported=None))

    def to_dict(self) -> dict:
        names = CAPABILITY_NAMES[self.kind]
        return {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label,
            "verification": self.verification.value,
            "implementation": self.implementation,
            "caveat": self.caveat,
            "test_only": self.test_only,
            "capabilities": {name: self.capability(name).to_dict() for name in names},
        }


def _vendor(supported: Optional[bool], note: str = "") -> Capability:
    """A capability as the vendor documents it. Nobody here has exercised it."""
    return Capability(supported=supported, verification=Verification.DECLARED, note=note)


def _tested(supported: bool) -> Capability:
    return Capability(supported=supported, verification=Verification.LOCAL_VALIDATED)


PROVIDERS: tuple[CommsProvider, ...] = (
    # -- voice engines -------------------------------------------------------------------
    CommsProvider(
        id="openai-live",
        kind=Kind.VOICE,
        label="OpenAI GPT-Live",
        capabilities={
            "speech_to_speech": _vendor(True),
            # Delegations arrive on the sideband and are answered by a read-only knowledge
            # lookup — "tools" here means looking things up, never acting.
            "tool_calls": Capability(
                supported=True,
                verification=Verification.LOCAL_VALIDATED,
                note="read-only knowledge lookups over the sideband; no actions mid-call",
            ),
            "barge_in": _vendor(True),
            "native_phone": _tested(True),
            "audio_stays_in_tenant_cloud": _vendor(False),
            "text_transcript": _vendor(True, "from session events; not collected yet"),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/openai_live.py",
        caveat="Exercised against a scripted stand-in only; no live call has been placed.",
    ),
    CommsProvider(
        id="elevenlabs-agents",
        kind=Kind.VOICE,
        label="ElevenLabs Agents",
        capabilities={
            "speech_to_speech": _vendor(True, "managed pipeline"),
            "tool_calls": _vendor(True),
            "barge_in": _vendor(True),
            "native_phone": _vendor(True, "vendor documents Twilio and SIP"),
            "audio_stays_in_tenant_cloud": _vendor(False),
            "text_transcript": _vendor(True),
        },
        caveat="The hosted agent runs its own LLM; the tenant's agent is reached through tools.",
    ),
    CommsProvider(
        id="bedrock-sonic",
        kind=Kind.VOICE,
        label="Amazon Nova Sonic (Bedrock)",
        capabilities={
            "speech_to_speech": _vendor(True),
            "tool_calls": _vendor(True),
            "barge_in": _vendor(True),
            "native_phone": _vendor(False, "needs a media bridge from the carrier"),
            "audio_stays_in_tenant_cloud": _vendor(True, "runs in the tenant's AWS account"),
            "text_transcript": _vendor(True, "from session events"),
        },
        caveat="Region and language availability differ per account; check before enabling.",
    ),
    CommsProvider(
        id="deepgram-cartesia",
        kind=Kind.VOICE,
        label="Cascaded: Deepgram STT, the agent, Cartesia TTS",
        capabilities={
            "speech_to_speech": _tested(False),
            # Lookups are read-only knowledge passages handed to the reply model per turn.
            "tool_calls": Capability(
                supported=True,
                verification=Verification.LOCAL_VALIDATED,
                note="read-only knowledge lookups per turn; no actions mid-call",
            ),
            "barge_in": _tested(True),
            "native_phone": _tested(False),
            "audio_stays_in_tenant_cloud": _vendor(False),
            "text_transcript": _tested(True),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/cascade.py",
        caveat=(
            "Exercised against scripted stand-ins only; no live call has been placed. "
            "Slightly higher latency than a speech-to-speech engine; measure before choosing."
        ),
    ),
    # -- carriers ------------------------------------------------------------------------
    CommsProvider(
        id="twilio",
        kind=Kind.TELEPHONY,
        label="Twilio Voice",
        capabilities={
            "outbound": _tested(True),
            "inbound": _vendor(True, "vendor supports it; this adapter does not yet"),
            "transfer": _vendor(True, "vendor supports it; this adapter does not yet"),
            "answering_machine_detection": _tested(True),
            "recording": _vendor(True, "vendor supports it; this adapter does not yet"),
            "signed_webhooks": _tested(True),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/twilio.py",
        caveat=(
            "Exercised against a scripted stand-in only; no live call has been placed. "
            "The runtime's SMS adapter reads Twilio credentials from an agent's own "
            "environment. An agent holding them can reach Twilio without this gateway; "
            "keep voice credentials out of agent environments."
        ),
    ),
    CommsProvider(
        id="telnyx",
        kind=Kind.TELEPHONY,
        label="Telnyx Voice",
        capabilities={
            "outbound": _vendor(True),
            "inbound": _vendor(True),
            "transfer": _vendor(True),
            "answering_machine_detection": _vendor(True),
            "recording": _vendor(True),
            "signed_webhooks": _vendor(True),
        },
    ),
    # -- email -----------------------------------------------------------------------------
    CommsProvider(
        id="resend",
        kind=Kind.EMAIL,
        label="Resend",
        capabilities={
            "idempotent_send": _tested(True),
            "delivery_events": _tested(True),
            "bounce_events": _tested(True),
            "signed_webhooks": _tested(True),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/resend.py",
        caveat="Exercised against a scripted stand-in only; no live email has been sent.",
    ),
    # -- test doubles --------------------------------------------------------------------
    CommsProvider(
        id="mock-voice",
        kind=Kind.VOICE,
        label="Test voice engine (no audio)",
        capabilities={
            "speech_to_speech": _tested(True),
            "tool_calls": _tested(False),
            "barge_in": _tested(False),
            "native_phone": _tested(True),
            "audio_stays_in_tenant_cloud": _tested(True),
            "text_transcript": _tested(False),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/mock.py",
        test_only=True,
    ),
    CommsProvider(
        id="mock-telephony",
        kind=Kind.TELEPHONY,
        label="Test carrier (places no real call)",
        capabilities={
            "outbound": _tested(True),
            "inbound": _tested(False),
            "transfer": _tested(False),
            "answering_machine_detection": _tested(True),
            "recording": _tested(False),
            "signed_webhooks": _tested(True),
        },
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/mock.py",
        test_only=True,
    ),
    CommsProvider(
        id="mock-email",
        kind=Kind.EMAIL,
        label="Test email provider (sends nothing)",
        capabilities={name: _tested(True) for name in EMAIL_CAPABILITIES},
        verification=Verification.LOCAL_VALIDATED,
        implementation="nova/communications/mock.py",
        test_only=True,
    ),
)

PROVIDERS_BY_ID: Mapping[str, CommsProvider] = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str) -> Optional[CommsProvider]:
    return PROVIDERS_BY_ID.get(provider_id)


def catalogue(kind: Optional[Kind] = None) -> tuple[CommsProvider, ...]:
    return tuple(p for p in PROVIDERS if kind is None or p.kind is kind)
