"""Telnyx Call Control: signed webhooks, the dial, and an answer plan carried out by command."""

from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.contracts import AnswerPlan, FailureReason, PlaceCall, SipTarget
from nova.communications.media import bridge
from nova.communications.mock import MockVoice
from nova.communications.telnyx import TelnyxTelephony
from nova.communications.webhooks import handle
from nova.errors import SpecError

from tests.platform.test_communications_live import Scripted, wait_for

NOW = 1_791_817_200  # 2026-10-12T15:00:00Z
START = datetime.fromtimestamp(NOW, tz=timezone.utc)
PUBLIC = "https://calls.example.test"
EVENTS = "/calls/telnyx/events"
SECRET = b"a-bridge-secret-of-enough-length"

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC_KEY = base64.b64encode(PRIVATE.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()

SPEC = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "voice_providers": ["mock-voice", "deepgram-cartesia"], "telephony_providers": ["telnyx"],
    "caller_ids": ["+15555550100"],
    "telnyx": {"connection_id": "1234567890"},
    "cascade": {"llm_base_url": "https://llm.example.test/v1", "llm_model": "m"},
    "purposes": [{"id": "support", "instructions": "Help with the order."}],
    "agents": [{"agent": "caller", "telephony": "telnyx", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"}, "purposes": ["support"],
                "contact_groups": ["customers"], "approval": "never"},
               {"agent": "streamer", "telephony": "telnyx", "caller_id": "+15555550100",
                "voice": {"primary": "deepgram-cartesia", "voice": "v-1"},
                "purposes": ["support"], "contact_groups": ["customers"], "approval": "never"}],
}}


class FakeTelnyx:
    """Answers Call Control as Telnyx documents it."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.calls = 0
        self.refuse_with = 0

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "Bearer KEY"
        payload = json.loads(body) if body else {}
        self.requests.append((method, url.split("/v2/", 1)[1], payload))
        if self.refuse_with:
            return self.refuse_with, json.dumps({"errors": [{"detail": "Call has already ended"}]}).encode()
        if method == "POST" and url.endswith("/v2/calls"):
            self.calls += 1
            return 200, json.dumps({"data": {"call_control_id": f"v3:cc-{self.calls}",
                                             "is_alive": False}}).encode()
        return 200, b'{"data": {"result": "ok"}}'

    def actions(self):
        return [(path.rsplit("/", 1)[-1], payload) for _, path, payload in self.requests
                if "/actions/" in path]


def state(payload_state: str) -> dict:
    return json.loads(base64.b64decode(payload_state))


def webhook(kind, ref, *, client_state="", event_id=None, at=NOW, **payload):
    body = json.dumps({"data": {
        "record_type": "event", "event_type": kind, "id": event_id or f"{kind}-{ref}",
        "occurred_at": payload.pop("occurred_at", "2026-10-12T15:00:00Z"),
        "payload": {"call_control_id": ref, "client_state": client_state, **payload}}}).encode()
    timestamp = str(at)
    signature = base64.b64encode(PRIVATE.sign(timestamp.encode() + b"|" + body)).decode()
    return {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp}, body


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    fake = FakeTelnyx()
    telnyx = TelnyxTelephony(api_key="KEY", public_key=PUBLIC_KEY, connection_id="1234567890",
                             webhook_url=f"{PUBLIC}{EVENTS}", transport=fake, clock=lambda: NOW)
    from nova.communications.cascade import DeepgramCartesiaVoice, Responder

    class Quiet(Responder):
        def reply(self, messages):
            return ""

    voice = MockVoice()
    stream = DeepgramCartesiaVoice(deepgram_key="d", cartesia_key="c", responder=Quiet(),
                                   media_url="wss://calls.example.test/media/deepgram-cartesia",
                                   connector=lambda url, headers: Scripted())
    gateway = CallGateway(parse_communications(SPEC), store,
                          AuditLog(tmp_path / "a.jsonl", tenant_id="acme"), {"telnyx": telnyx},
                          voices={"mock-voice": voice, "deepgram-cartesia": stream},
                          call_secret=SECRET, clock=lambda: START)
    yield gateway, fake, voice
    store.close()


def post(gateway, headers, body):
    return handle(gateway, PUBLIC, EVENTS, headers, body)


# -- the vendor contract -------------------------------------------------------------------------


def test_only_a_fresh_signature_from_the_accounts_key_is_accepted(world):
    gateway, _, _ = world
    telnyx = gateway.carriers["telnyx"]
    headers, body = webhook("call.initiated", "v3:cc-1")
    assert telnyx.verify(headers, body, "")
    assert not telnyx.verify(headers, body + b" ", "")
    stale_headers, stale_body = webhook("call.initiated", "v3:cc-1", at=NOW - 301)
    assert not telnyx.verify(stale_headers, stale_body, "")
    other = Ed25519PrivateKey.generate()
    forged = dict(headers, **{"telnyx-signature-ed25519": base64.b64encode(
        other.sign(headers["telnyx-timestamp"].encode() + b"|" + body)).decode()})
    assert not telnyx.verify(forged, body, "")
    assert post(gateway, {}, body)[0] == 403


def test_the_dial_carries_our_call_id_and_asks_for_machine_detection(world):
    gateway, fake, _ = world
    telnyx = gateway.carriers["telnyx"]
    ref = telnyx.place_call(PlaceCall("call-1", "acme", "+13125550101", "+15555550100", 300))
    (_, path, payload), = fake.requests
    assert (path, ref) == ("calls", "v3:cc-1")
    assert payload["connection_id"] == "1234567890" and payload["command_id"] == "call-1"
    assert payload["answering_machine_detection"] == "detect"
    assert payload["webhook_url"] == f"{PUBLIC}{EVENTS}"
    assert state(payload["client_state"]) == {"c": "call-1", "amd": 1}


# -- a call, end to end --------------------------------------------------------------------------


def test_a_person_hears_the_disclosure_in_full_then_the_engine_takes_the_call(world):
    gateway, fake, voice = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                  idempotency_key="k1").record
    dial_state = fake.requests[0][2]["client_state"]
    ref = record.provider_call_ref

    assert post(gateway, *webhook("call.answered", ref, client_state=dial_state))[0] == 200
    assert fake.actions() == []  # machine detection still listening: nothing said yet
    post(gateway, *webhook("call.machine.detection.ended", ref, client_state=dial_state,
                           result="human"))
    ((action, speak),) = fake.actions()
    assert action == "speak" and speak["payload"] == "This is an AI assistant calling for Acme."
    held = state(speak["client_state"])
    assert held["c"] == record.call_id and held["next"][0]["op"] == "transfer"

    # The transfer waits for the disclosure to finish.
    post(gateway, *webhook("call.speak.ended", ref, client_state=speak["client_state"]))
    action, transfer = fake.actions()[-1]
    assert action == "transfer" and transfer["to"].startswith("sip:")
    headers = {h["name"]: h["value"] for h in transfer["custom_headers"]}
    assert headers["X-Call-Id"] == record.call_id

    session_headers, session_body = voice.incoming(session_id="s1", event_id="e1",
                                                   sip_headers=headers)
    assert gateway.voice_event("mock-voice", headers=session_headers,
                               body=session_body).outcome == "applied"

    post(gateway, *webhook("call.hangup", ref, client_state=transfer["client_state"],
                           hangup_cause="normal_clearing", start_time="2026-10-12T15:00:00Z",
                           occurred_at="2026-10-12T15:02:05Z"))
    assert gateway.store.call(record.call_id).status is CallStatus.COMPLETED
    assert gateway.store.call_usage(record.call_id)["duration_seconds"] == 125


def test_a_voicemail_greeting_is_hung_up_on_without_a_word(world):
    gateway, fake, _ = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                  idempotency_key="k1").record
    dial_state = fake.requests[0][2]["client_state"]
    post(gateway, *webhook("call.machine.detection.ended", record.provider_call_ref,
                           client_state=dial_state, result="machine"))
    assert [action for action, _ in fake.actions()] == ["hangup"]
    stored = gateway.store.call(record.call_id)
    assert (stored.status, stored.failure) == (CallStatus.FAILED,
                                               FailureReason.MACHINE_DETECTED.value)


def test_a_forged_continue_never_runs_a_step(world):
    gateway, fake, _ = world
    _, body = webhook("call.speak.ended", "v3:cc-9", client_state=base64.b64encode(json.dumps(
        {"next": [{"op": "transfer", "to": "sip:attacker@evil.example", "headers": {}}]}
    ).encode()).decode())
    assert post(gateway, {"telnyx-signature-ed25519": "AAAA", "telnyx-timestamp": str(NOW)},
                body)[0] == 403
    assert fake.actions() == []


def test_a_stream_engine_gets_rtp_mulaw_and_the_bridge_reads_telnyxs_start(world):
    gateway, fake, _ = world
    record = gateway.request_call(agent="streamer", contact_id="c1", purpose="support",
                                  idempotency_key="k1").record
    gateway.carriers["telnyx"].redirect(record.provider_call_ref, AnswerPlan(
        connect=gateway.voices["deepgram-cartesia"].bridge_target(
            record.call_id, gateway.call_token(record.call_id))))
    ((action, start),) = fake.actions()
    assert action == "streaming_start"
    assert (start["stream_bidirectional_mode"], start["stream_bidirectional_codec"]) == \
        ("rtp", "PCMU")
    params = {p["name"]: p["value"] for p in start["custom_parameters"]}
    assert params["call_id"] == record.call_id

    # The bridge proves the call from Telnyx's start message, as it does from Twilio's.
    gateway.store.advance(record.call_id, CallStatus.ANSWERED, at="2026-10-12T15:00:01.000Z")
    gateway.store.set_voice(record.call_id, engine="deepgram-cartesia",
                            at="2026-10-12T15:00:01.000Z")
    channel = Scripted()
    channel.push({"event": "connected", "version": "1.0.0"})
    channel.push({"event": "start", "stream_id": "st-1", "start": {
        "call_control_id": record.provider_call_ref, "custom_parameters": start["custom_parameters"],
        "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}}})
    channel.push({"event": "stop", "stream_id": "st-1"})
    bridge(gateway, channel, "/media/deepgram-cartesia")
    wait_for(lambda: gateway.store.call(record.call_id).voice_session_id == "st-1", "binding")


def test_ending_a_call_that_already_ended_is_not_an_error(world):
    gateway, fake, _ = world
    fake.refuse_with = 422
    gateway.carriers["telnyx"].end_call("v3:cc-1")


def test_a_recording_profile_is_refused_on_a_carrier_that_cannot_record():
    data = copy.deepcopy(SPEC)
    data["communications"]["agents"][0]["recording"] = {
        "enabled": True, "legal_basis": "All-party consent asked on every call, reviewed."}
    with pytest.raises(SpecError, match="cannot record"):
        parse_communications(data)
    del data["communications"]["telnyx"]
    data["communications"]["agents"][0].pop("recording")
    with pytest.raises(SpecError, match="connection_id"):
        parse_communications(data)


def test_failover_moves_a_telnyx_call_by_command(world):
    gateway, fake, _ = world
    data = copy.deepcopy(SPEC)
    data["communications"]["voice_providers"].append("openai-live")
    data["communications"]["agents"][0]["voice"]["fallbacks"] = ["openai-live"]
    gateway.spec = parse_communications(data)
    gateway.voices["openai-live"] = MockVoice()
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                  idempotency_key="k1").record
    gateway.store.advance(record.call_id, CallStatus.ANSWERED, at="2026-10-12T15:00:01.000Z")
    gateway.store.set_voice(record.call_id, engine="mock-voice", at="2026-10-12T15:00:01.000Z")
    assert gateway.engine_failed(record.call_id, "mock-voice", "refused")
    assert [action for action, _ in fake.actions()] == ["speak"]
    nxt = state(fake.actions()[0][1]["client_state"])["next"][0]
    assert nxt["op"] == "transfer" and nxt["headers"]["X-Call-Id"] == record.call_id
    assert isinstance(gateway.voices["openai-live"].bridge_target("x", "y"), SipTarget)


def test_assembly_names_telnyx_and_elevenlabs_keys_and_wires_them(tmp_path):
    from nova.communications.assemble import build_gateway, missing_credentials

    data = copy.deepcopy(SPEC)
    data["communications"]["voice_providers"] = ["elevenlabs-agents"]
    data["communications"]["elevenlabs"] = {"agent_id": "agent_abcdefgh12"}
    for agent in data["communications"]["agents"]:
        agent["voice"] = {"primary": "elevenlabs-agents"}
    spec = parse_communications(data)
    assert missing_credentials(spec, {}) == {
        "telnyx": ["TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"],
        "elevenlabs-agents": ["ELEVENLABS_API_KEY"], "bridge": ["CALL_BRIDGE_SECRET"]}
    env = {"TELNYX_API_KEY": "KEY", "TELNYX_PUBLIC_KEY": PUBLIC_KEY, "ELEVENLABS_API_KEY": "xi",
           "CALL_BRIDGE_SECRET": "s" * 32}
    store = CommsStore(tmp_path / "c.db", tenant_id="acme")
    gateway = build_gateway(spec, store=store, audit=AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                            env=env, public_url=PUBLIC)
    assert set(gateway.carriers) == {"telnyx"} and set(gateway.voices) == {"elevenlabs-agents"}
    assert gateway.voices["elevenlabs-agents"].bridge_target("c", "t").url == \
        "wss://calls.example.test/media/elevenlabs-agents"
    store.close()
