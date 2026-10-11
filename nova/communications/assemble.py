"""Build a working gateway from a tenant's declaration and the gateway's own environment.

The environment here is the **gateway process's**, never an agent's. Those are different
processes on purpose: an agent that held these credentials could dial around every rule in
``gate.py``.

========================  ============================================================
Variable                  Used for
========================  ============================================================
``TWILIO_ACCOUNT_SID``    Twilio carrier
``TWILIO_AUTH_TOKEN``     Twilio carrier; also verifies Twilio's callbacks
``OPENAI_API_KEY``        GPT-Live voice engine
``OPENAI_PROJECT_ID``     GPT-Live SIP address (``proj_...``)
``OPENAI_WEBHOOK_SECRET`` verifies GPT-Live's webhooks (``whsec_...``)
``CALL_BRIDGE_SECRET``    signs the call token carried across the SIP bridge (32+ chars)
========================  ============================================================

A provider the tenant allows but whose credentials are absent is left out, and
:func:`missing_credentials` names what is missing — a call through it then fails with a
clear reason instead of the gateway refusing to start.
"""

from __future__ import annotations

from typing import Mapping

from nova.audit import AuditLog
from nova.communications.contracts import TelephonyProvider, VoiceSessionProvider
from nova.communications.gateway import CallGateway
from nova.communications.openai_live import OpenAILiveVoice
from nova.communications.spec import CommunicationsSpec
from nova.communications.store import CommsStore
from nova.communications.twilio import TwilioTelephony

REQUIRED_ENV: Mapping[str, tuple[str, ...]] = {
    "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    "openai-live": ("OPENAI_API_KEY", "OPENAI_PROJECT_ID", "OPENAI_WEBHOOK_SECRET"),
}
SECRET_ENV = "CALL_BRIDGE_SECRET"


def missing_credentials(spec: CommunicationsSpec, env: Mapping[str, str]) -> dict[str, list[str]]:
    """Per allowed provider with an adapter, the variable names still unset. Names only."""
    out: dict[str, list[str]] = {}
    for provider in (*spec.telephony_providers, *spec.voice_providers):
        names = REQUIRED_ENV.get(provider)
        if names:
            missing = [name for name in names if not env.get(name)]
            if missing:
                out[provider] = missing
    if spec.voice_providers and len(env.get(SECRET_ENV, "")) < 32:
        out["bridge"] = [SECRET_ENV]
    return out


def build_gateway(
    spec: CommunicationsSpec,
    *,
    store: CommsStore,
    audit: AuditLog,
    env: Mapping[str, str],
    public_url: str,
) -> CallGateway:
    base = public_url.rstrip("/")
    missing = missing_credentials(spec, env)

    carriers: dict[str, TelephonyProvider] = {}
    if "twilio" in spec.telephony_providers and "twilio" not in missing:
        carriers["twilio"] = TwilioTelephony.from_env(
            env, answer_url=f"{base}/calls/twilio/answer", status_url=f"{base}/calls/twilio/status",
        )

    voices: dict[str, VoiceSessionProvider] = {}
    if "bridge" not in missing and "openai-live" in spec.voice_providers \
            and "openai-live" not in missing:
        voices["openai-live"] = OpenAILiveVoice.from_env(env)

    return CallGateway(
        spec, store, audit, carriers,
        voices=voices,
        call_secret=env.get(SECRET_ENV, "").encode("utf-8") if voices else b"",
    )
