"""Build a working gateway from a tenant's declaration and the gateway's own environment.

The environment here is the **gateway process's**, never an agent's. Those are different
processes on purpose: an agent that held these credentials could dial around every rule in
``gate.py``.

============================  ============================================================
Variable                      Used for
============================  ============================================================
``TWILIO_ACCOUNT_SID``        Twilio carrier
``TWILIO_AUTH_TOKEN``         Twilio carrier; also verifies Twilio's callbacks
``OPENAI_API_KEY``            GPT-Live voice engine
``OPENAI_PROJECT_ID``         GPT-Live SIP address (``proj_...``)
``OPENAI_WEBHOOK_SECRET``     verifies GPT-Live's webhooks (``whsec_...``)
``DEEPGRAM_API_KEY``          cascaded engine: speech recognition
``CARTESIA_API_KEY``          cascaded engine: speech synthesis
``CASCADE_LLM_API_KEY``       cascaded engine: the reply model at ``cascade.llm_base_url``
``TELNYX_API_KEY``            Telnyx carrier (Call Control v2)
``TELNYX_PUBLIC_KEY``         verifies Telnyx's Ed25519-signed webhooks (base64)
``ELEVENLABS_API_KEY``        ElevenLabs Agents voice engine
``CALL_BRIDGE_SECRET``        signs the call token carried across the bridge (32+ chars)
``EXTRACTION_LLM_API_KEY``    reads finished calls into fields, at ``extraction.llm_base_url``
``RESEND_API_KEY``            Resend: sends follow-up emails
``RESEND_WEBHOOK_SECRET``     verifies Resend's delivery webhooks (``whsec_...``)
``EMAIL_LINK_SECRET``         signs the unsubscribe link in every email (32+ chars)
``CALCOM_API_KEY``           Cal.com: lists open times, books and cancels meetings
``CALCOM_WEBHOOK_SECRET``     verifies Cal.com's booking webhooks
============================  ============================================================

Non-secret settings — which reply model, which speech models — are in
``communications.yaml``, never here.

A provider the tenant allows but whose credentials are absent is left out, and
:func:`missing_credentials` names what is missing — a call through it then fails with a
clear reason instead of the gateway refusing to start.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Sequence

from nova.audit import AuditLog
from nova.communications.answers import index_search, knowledge_answerer
from nova.communications.cascade import (
    DEFAULT_CARTESIA_VERSION,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    ChatResponder,
    DeepgramCartesiaVoice,
)
from nova.communications.contracts import TelephonyProvider, VoiceEngine
from nova.communications.extraction import extractor_from
from nova.communications.gateway import CallGateway
from nova.communications import ed25519
from nova.communications.elevenlabs import DEFAULT_HOST, ElevenLabsVoice
from nova.communications.openai_live import OpenAILiveVoice
from nova.communications.spec_vendors import ELEVENLABS
from nova.communications.telnyx import TelnyxTelephony
from nova.communications.calcom import CalcomCalendar
from nova.communications.resend import ResendEmail
from nova.communications.spec import CASCADED_ENGINE, CommunicationsSpec
from nova.communications.store import CommsStore
from nova.communications.twilio import TwilioTelephony

if TYPE_CHECKING:
    from nova.runtime.base import WorkItem

REQUIRED_ENV: Mapping[str, tuple[str, ...]] = {
    "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    "openai-live": ("OPENAI_API_KEY", "OPENAI_PROJECT_ID", "OPENAI_WEBHOOK_SECRET"),
    "deepgram-cartesia": ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CASCADE_LLM_API_KEY"),
    "resend": ("RESEND_API_KEY", "RESEND_WEBHOOK_SECRET"),
    "calcom": ("CALCOM_API_KEY", "CALCOM_WEBHOOK_SECRET"),
    "telnyx": ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"),
    "elevenlabs-agents": ("ELEVENLABS_API_KEY",),
}
SECRET_ENV = "CALL_BRIDGE_SECRET"
LINK_SECRET_ENV = "EMAIL_LINK_SECRET"


def missing_credentials(spec: CommunicationsSpec, env: Mapping[str, str]) -> dict[str, list[str]]:
    """Per allowed provider with an adapter, the variable names still unset. Names only."""
    out: dict[str, list[str]] = {}
    email = (spec.email.provider,) if spec.email.provider else ()
    calendar = (spec.meetings.provider,) if spec.meetings.provider else ()
    for provider in (*spec.telephony_providers, *spec.voice_providers, *email, *calendar):
        names = REQUIRED_ENV.get(provider)
        if names:
            missing = [name for name in names if not env.get(name)]
            if missing:
                out[provider] = missing
    if spec.voice_providers and len(env.get(SECRET_ENV, "")) < 32:
        out["bridge"] = [SECRET_ENV]
    if "telnyx" in spec.telephony_providers and ed25519.available():
        out.setdefault("telnyx", []).append("the 'cryptography' package")
    if email and len(env.get(LINK_SECRET_ENV, "")) < 32:
        out["email-links"] = [LINK_SECRET_ENV]
    return out


def build_gateway(
    spec: CommunicationsSpec,
    *,
    store: CommsStore,
    audit: AuditLog,
    env: Mapping[str, str],
    public_url: str,
    knowledge_index: Optional[Path] = None,
    knowledge_grants: Optional[Mapping[str, Sequence[str]]] = None,
    tasks: Optional[Callable[[WorkItem], str]] = None,
) -> CallGateway:
    """``public_url`` serves both listeners: HTTPS webhooks at its root, and the media
    bridge at the same host over ``wss://`` under ``/media/``.

    ``knowledge_index`` and ``knowledge_grants`` (agent id -> source ids, from the bundle)
    enable mid-call lookups. Without them calls run on their instructions alone.
    ``tasks`` puts post-call work on the runtime's board; without it, work actions wait.
    """
    base = public_url.rstrip("/")
    missing = missing_credentials(spec, env)

    carriers: dict[str, TelephonyProvider] = {}
    if "twilio" in spec.telephony_providers and "twilio" not in missing:
        carriers["twilio"] = TwilioTelephony.from_env(
            env, answer_url=f"{base}/calls/twilio/answer", status_url=f"{base}/calls/twilio/status",
        )

    if "telnyx" in spec.telephony_providers and "telnyx" not in missing:
        carriers["telnyx"] = TelnyxTelephony.from_env(
            env, connection_id=spec.vendors.telnyx_connection_id,
            webhook_url=f"{base}/calls/telnyx/events")

    voices: dict[str, VoiceEngine] = {}
    if "bridge" not in missing:
        if "openai-live" in spec.voice_providers and "openai-live" not in missing:
            voices["openai-live"] = OpenAILiveVoice.from_env(env)
        if CASCADED_ENGINE in spec.voice_providers and CASCADED_ENGINE not in missing:
            cascade = spec.cascade
            voices[CASCADED_ENGINE] = DeepgramCartesiaVoice(
                deepgram_key=env["DEEPGRAM_API_KEY"],
                cartesia_key=env["CARTESIA_API_KEY"],
                responder=ChatResponder(base_url=cascade.llm_base_url,
                                        api_key=env["CASCADE_LLM_API_KEY"],
                                        model=cascade.llm_model),
                media_url=base.replace("https://", "wss://", 1) + f"/media/{CASCADED_ENGINE}",
                stt_model=cascade.stt_model or DEFAULT_STT_MODEL,
                tts_model=cascade.tts_model or DEFAULT_TTS_MODEL,
                cartesia_version=cascade.cartesia_version or DEFAULT_CARTESIA_VERSION,
            )
        if ELEVENLABS in spec.voice_providers and ELEVENLABS not in missing:
            voices[ELEVENLABS] = ElevenLabsVoice(
                api_key=env["ELEVENLABS_API_KEY"], agent_id=spec.vendors.elevenlabs_agent_id,
                host=spec.vendors.elevenlabs_host or DEFAULT_HOST,
                media_url=base.replace("https://", "wss://", 1) + f"/media/{ELEVENLABS}")

    answers = None
    if knowledge_index is not None and knowledge_grants:
        search = index_search(Path(knowledge_index))
        grants = {agent: tuple(sources) for agent, sources in knowledge_grants.items()}

        def answers(record):
            sources = grants.get(record.agent, ())
            if not sources:
                return None
            return knowledge_answerer(call_id=record.call_id, agent=record.agent,
                                      sources=sources, search=search, audit=audit)

    email = None
    if spec.email.provider == "resend" and "resend" not in missing:
        email = ResendEmail.from_env(env)

    calendar = None
    if spec.meetings.provider == "calcom" and "calcom" not in missing:
        calendar = CalcomCalendar.from_env(env)

    return CallGateway(
        spec, store, audit, carriers,
        voices=voices,
        call_secret=env.get(SECRET_ENV, "").encode("utf-8") if voices else b"",
        answers=answers,
        extractor=extractor_from(spec, env),
        email=email,
        calendar=calendar,
        tasks=tasks,
        link_secret=env.get(LINK_SECRET_ENV, "").encode("utf-8"),
        public_url=public_url,
    )
