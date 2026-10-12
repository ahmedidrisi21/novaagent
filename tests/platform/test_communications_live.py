"""Live connections: mid-call lookups, the cascaded engine, and the media bridge.

Every WebSocket here is a scripted stand-in speaking the vendor's documented messages,
except one loopback test that runs the real ``websockets`` library through the bridge.
Waits are event-driven with generous deadlines; nothing asserts on elapsed time.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from datetime import datetime, timezone, UTC

import pytest

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.answers import NOT_FOUND, index_search, knowledge_answerer
from nova.communications.cascade import (
    GOODBYE_AFTER_OPT_OUT,
    CascadedCall,
    DeepgramCartesiaVoice,
    Responder,
)
from nova.communications.contracts import (
    AnswerPlan,
    CallHooks,
    MediaChannel,
    ProviderError,
    StreamTarget,
    VoiceSessionRequest,
)
from nova.communications.media import bridge
from nova.communications.mock import MockTelephony, MockVoice
from nova.communications.openai_live import LiveSideband
from nova.communications.twilio import TwilioTelephony
from nova.knowledge import ingest, load_catalog

NOON_UTC = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
SECRET = b"a-bridge-secret-of-enough-length"
_EOF = object()


class Scripted(MediaChannel):
    """A WebSocket the test drives: ``push`` what the far side says, read ``sent``."""

    def __init__(self, on_send=None) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list = []
        self.closed = False
        self._on_send = on_send

    def push(self, message) -> None:
        self.inbox.put(message if isinstance(message, (str, bytes)) else json.dumps(message))

    def send(self, message) -> None:
        if self.closed:
            raise EOFError("closed")
        self.sent.append(message)
        if self._on_send is not None:
            self._on_send(self, message)

    def recv(self, timeout=None):
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if item is _EOF:
            self.inbox.put(_EOF)
            raise EOFError("closed")
        return item

    def close(self) -> None:
        self.closed = True
        self.inbox.put(_EOF)

    def json_sent(self) -> list[dict]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


def wait_for(condition, what: str, deadline: float = 5.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# -- answers from company knowledge -------------------------------------------------------

HANDBOOK = """# Refund Policy

## Eligibility

A customer may request a refund within 30 days of purchase.
"""


@pytest.fixture
def knowledge(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "refunds.md").write_text(HANDBOOK, encoding="utf-8")
    (tmp_path / "knowledge.yaml").write_text(
        f"sources:\n  - id: handbook\n    title: Handbook\n    root: {docs}\n"
        '    include: ["**/*.md"]\n', encoding="utf-8")
    ingest(load_catalog(tmp_path), tmp_path / "k.db")
    return tmp_path / "k.db"


def test_a_question_is_answered_from_the_agents_granted_knowledge_and_audited(tmp_path, knowledge):
    audit = AuditLog(tmp_path / "a.jsonl", tenant_id="acme")
    answer = knowledge_answerer(call_id="c1", agent="support", sources=["handbook"],
                                search=index_search(knowledge), audit=audit)
    reply = answer("how many days do I have for a refund")
    assert "30 days" in reply and "refunds.md" in reply
    (event,) = [e for e in audit.read() if e.kind == "communications.knowledge.searched"]
    assert event.subject == "c1" and event.detail["citations"]


def test_an_agent_without_the_grant_hears_not_found_not_the_document(tmp_path, knowledge):
    audit = AuditLog(tmp_path / "a.jsonl", tenant_id="acme")
    other = knowledge_answerer(call_id="c1", agent="sales", sources=["pricing"],
                               search=index_search(knowledge), audit=audit)
    assert other("how many days do I have for a refund") == NOT_FOUND


# -- the GPT-Live sideband ----------------------------------------------------------------


def test_a_delegation_is_answered_with_what_the_caller_just_asked():
    channel = Scripted()
    asked: list[str] = []
    sideband = threading.Thread(
        target=LiveSideband(channel, lambda q: asked.append(q) or "Refunds: 30 days.").run)
    sideband.start()
    channel.push({"type": "session.input_transcript.delta", "delta": "What is your "})
    channel.push({"type": "session.input_transcript.delta", "delta": "refund window?"})
    channel.push({"type": "session.delegation.created", "delegation": {"id": "del_1"}})
    channel.push({"type": "session.closed"})
    sideband.join(timeout=5)

    assert asked == ["What is your refund window?"]
    said = [e for e in channel.json_sent() if e["type"] == "session.commentary.append"]
    assert [(e["delegation_id"], e["content"]) for e in said] == [("del_1", "Refunds: 30 days.")]


def test_a_failed_lookup_says_so_instead_of_dropping_the_call():
    channel = Scripted()

    def broken(question):
        raise RuntimeError("index offline")

    sideband = threading.Thread(target=LiveSideband(channel, broken).run)
    sideband.start()
    channel.push({"type": "session.input_transcript.delta", "delta": "price?"})
    channel.push({"type": "session.delegation.created", "delegation": {"id": "del_1"}})
    channel.push({"type": "session.closed"})
    sideband.join(timeout=5)
    said = [e["content"] for e in channel.json_sent() if e["type"] == "session.commentary.append"]
    assert said and "could not find" in said[0]


def test_a_slow_sideband_handshake_never_holds_up_the_webhook():
    from nova.communications.openai_live import OpenAILiveVoice

    release, opened, channel = threading.Event(), [], Scripted()

    def slow_connector(url, headers):
        release.wait(timeout=5)
        opened.append((url, headers["Authorization"]))
        return channel

    voice = OpenAILiveVoice(api_key="sk", project_id="proj_x", webhook_secret="whsec_eA==",
                            connector=slow_connector)
    voice.attach("sess_1", CallHooks(answer=lambda q: ""))  # returns while the handshake is pending
    assert not opened
    release.set()
    wait_for(lambda: opened, "the sideband to open")
    assert opened == [("wss://api.openai.com/v1/live/sessions/sess_1/attach", "Bearer sk")]
    channel.push({"type": "session.closed"})
    wait_for(lambda: channel.closed, "the sideband to close")

    def refused(url, headers):
        raise OSError("handshake refused")

    OpenAILiveVoice(api_key="sk", project_id="proj_x", webhook_secret="whsec_eA==",
                    connector=refused).attach("sess_2", CallHooks())  # logged, never raised


# -- the gateway wires lookups only where a grant exists ----------------------------------

SPEC = {
    "communications": {
        "allow_test_providers": True,
        "disclosure": "This is an AI assistant calling for Acme.",
        "voice_providers": ["mock-voice", "deepgram-cartesia"],
        "telephony_providers": ["mock-telephony"],
        "caller_ids": ["+15555550100"],
        "cascade": {"llm_base_url": "https://llm.example.test/v1", "llm_model": "small"},
        "purposes": [{"id": "support", "instructions": "Help with their open ticket."}],
        "agents": [
            {"agent": "sip-agent", "telephony": "mock-telephony", "caller_id": "+15555550100",
             "voice": {"primary": "mock-voice"}, "purposes": ["support"],
             "contact_groups": ["customers"], "approval": "never"},
            {"agent": "stream-agent", "telephony": "mock-telephony", "caller_id": "+15555550100",
             "voice": {"primary": "deepgram-cartesia", "voice": "cartesia-voice-1"},
             "purposes": ["support"], "contact_groups": ["customers"], "approval": "never"},
        ],
    }
}


class StubResponder(Responder):
    def __init__(self, replies=None, fail=False):
        self.calls: list[list[dict]] = []
        self._replies = list(replies or [])
        self._fail = fail

    def reply(self, messages):
        self.calls.append(messages)
        if self._fail:
            raise ProviderError("model down")
        return self._replies.pop(0) if self._replies else "Okay."


def tts_stand_in(hold: threading.Event):
    """Cartesia: answers each generation with one chunk, then ``done`` unless held."""

    def on_send(channel, raw):
        message = json.loads(raw)
        if "transcript" in message:
            context = message["context_id"]
            channel.push({"type": "chunk", "context_id": context,
                          "data": base64.b64encode(b"\xff" * 160).decode()})
            if not hold.is_set():
                channel.push({"type": "done", "done": True, "context_id": context})

    return on_send


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "comms.sqlite", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    carrier, sip = MockTelephony(), MockVoice()
    channels: dict[str, Scripted] = {}
    hold = threading.Event()
    responder = StubResponder()

    def connector(url, headers):
        if "deepgram" in url:
            channels["stt"] = Scripted()
            return channels["stt"]
        channels["tts"] = Scripted(on_send=tts_stand_in(hold))
        return channels["tts"]

    stream = DeepgramCartesiaVoice(deepgram_key="dg", cartesia_key="ca", responder=responder,
                                   media_url="wss://calls.example.test/media/deepgram-cartesia",
                                   connector=connector)
    lookups: list[str] = []

    def answers(record):
        if record.agent == "nobody":
            return None
        return lambda q: lookups.append(q) or "From refunds.md:5: refunds within 30 days."

    gateway = CallGateway(
        parse_communications(SPEC), store, AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
        {"mock-telephony": carrier}, voices={"mock-voice": sip, "deepgram-cartesia": stream},
        call_secret=SECRET, answers=answers, clock=lambda: NOON_UTC,
    )
    yield type("World", (), dict(gateway=gateway, carrier=carrier, sip=sip, channels=channels,
                                 hold=hold, responder=responder, lookups=lookups))
    store.close()


def answered(world, agent):
    gateway, carrier = world.gateway, world.carrier
    record = gateway.request_call(agent=agent, contact_id="c1", purpose="support",
                                  idempotency_key=agent).record
    headers, body = carrier.callback(record.provider_call_ref, CallStatus.ANSWERED,
                                     event_id=f"{agent}-answer")
    response = gateway.answer("mock-telephony", headers=headers, body=body)
    return record, json.loads(response.body)


def test_a_sip_session_is_accepted_with_delegation_and_gets_its_lookup(world):
    _record, plan = answered(world, "sip-agent")
    headers, body = world.sip.incoming(session_id="s1", event_id="e1", sip_headers=plan["headers"])
    assert world.gateway.voice_event("mock-voice", headers=headers, body=body).outcome == "applied"
    assert world.sip.accepted["s1"].delegation is True
    world.sip.attached["s1"].answer("refund window?")
    assert world.lookups == ["refund window?"]


def test_twilio_streams_a_cascaded_call_to_the_bridge_with_its_token():
    plan = AnswerPlan(say="AI calling.", connect=StreamTarget(
        "wss://calls.example.test/media/deepgram-cartesia", {"call_id": "c1", "token": "t&1"}))
    _, twiml = TwilioTelephony("AC1", "x", answer_url="https://h.test/a",
                               status_url="https://h.test/s").render_answer(plan)
    text = twiml.decode()
    assert '<Stream url="wss://calls.example.test/media/deepgram-cartesia">' in text
    assert '<Parameter name="token" value="t&amp;1"/>' in text
    assert text.index("<Say>") < text.index("<Connect>")


def test_a_stream_without_its_token_is_closed_before_anyone_speaks(world):
    record, _ = answered(world, "stream-agent")
    media = Scripted()
    media.push({"event": "connected"})
    media.push({"event": "start", "streamSid": "MZ1",
                "start": {"customParameters": {"call_id": record.call_id, "token": "guess"}}})
    runner = threading.Thread(target=bridge, args=(world.gateway, media, "/media/deepgram-cartesia"),
                              daemon=True)
    runner.start()
    runner.join(timeout=5)
    assert not runner.is_alive() and media.closed
    assert not media.sent and "stt" not in world.channels
    assert world.gateway.store.call(record.call_id).status is CallStatus.ANSWERED


# -- the cascaded call, end to end through the bridge ------------------------------------


def test_a_cascaded_call_greets_answers_from_knowledge_yields_to_the_caller_and_honours_opt_out(world):
    record, plan = answered(world, "stream-agent")
    assert plan["stream"] == "wss://calls.example.test/media/deepgram-cartesia"
    media = Scripted()
    media.push({"event": "connected"})
    media.push({"event": "start", "streamSid": "MZ1", "start": {"customParameters": plan["parameters"]}})
    call = threading.Thread(target=bridge, args=(world.gateway, media, "/media/deepgram-cartesia"),
                            daemon=True)
    call.start()

    def played():
        return [m for m in media.json_sent() if m.get("event") == "media"]

    # 1. The agent opens the call, in the agent's Cartesia voice, as mu-law at 8 kHz.
    wait_for(lambda: played(), "the opening line")
    assert world.gateway.store.call(record.call_id).status is CallStatus.SESSION_CONNECTED
    generation = json.loads(world.channels["tts"].sent[0])
    assert generation["voice"] == {"id": "cartesia-voice-1"}
    assert generation["output_format"] == {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": 8000}

    # 2. Caller audio reaches the recogniser untouched.
    media.push({"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(b"\x7f\x7e").decode()}})
    wait_for(lambda: b"\x7f\x7e" in world.channels["stt"].sent, "audio forwarded")

    # 3. A question is answered with company knowledge in front of the model.
    stt = world.channels["stt"]
    stt.push({"type": "Results", "is_final": True, "speech_final": True,
              "channel": {"alternatives": [{"transcript": "what's your refund window"}]}})
    wait_for(lambda: len(world.responder.calls) == 2, "the second reply")
    assert world.lookups == ["what's your refund window"]
    assert any("refunds within 30 days" in m["content"] for m in world.responder.calls[1])

    # 4. The caller cuts in: the reply in flight is cancelled and buffered audio cleared.
    world.hold.set()
    stt.push({"type": "Results", "is_final": True, "speech_final": True,
              "channel": {"alternatives": [{"transcript": "and shipping?"}]}})
    wait_for(lambda: len(world.responder.calls) == 3 and len(world.channels["tts"].sent) == 3,
             "the third reply to start")
    wait_for(lambda: len(played()) >= 3, "the third reply to play")
    stt.push({"type": "SpeechStarted", "channel": [0], "timestamp": 1.0})
    wait_for(lambda: any(m.get("event") == "clear" for m in media.json_sent()), "barge-in clear")
    assert any(json.loads(m).get("cancel") for m in world.channels["tts"].sent)

    # 5. "Stop calling me": suppressed at once, a goodbye, and the line is hung up.
    world.hold.clear()
    stt.push({"type": "Results", "is_final": True, "speech_final": True,
              "channel": {"alternatives": [{"transcript": "please stop calling me"}]}})
    wait_for(lambda: world.gateway.store.is_suppressed("c1"), "the opt-out")
    wait_for(lambda: world.carrier.ended, "the hang-up")
    goodbye = [json.loads(m) for m in world.channels["tts"].sent if "transcript" in json.loads(m)][-1]
    assert goodbye["transcript"] == GOODBYE_AFTER_OPT_OUT

    media.push({"event": "stop", "stop": {}})
    call.join(timeout=10)
    assert not call.is_alive() and media.closed


def test_a_reply_model_outage_apologises_and_hangs_up():
    media, stt, hold = Scripted(), Scripted(), threading.Event()
    tts = Scripted(on_send=tts_stand_in(hold))
    ended = []
    call = CascadedCall(
        media=media, stt=stt, tts=tts, stream_sid="MZ1",
        request=VoiceSessionRequest("c1", "acme", "a", "Help.", voice="v"),
        hooks=CallHooks(end_call=lambda: ended.append(True)),
        responder=StubResponder(fail=True),
    )
    runner = threading.Thread(target=call.run, daemon=True)
    runner.start()
    wait_for(lambda: ended, "the hang-up")
    spoken = [json.loads(m)["transcript"] for m in tts.sent if "transcript" in json.loads(m)]
    assert spoken and spoken[0].startswith("Sorry")
    media.close()
    runner.join(timeout=10)


# -- the real library, end to end ---------------------------------------------------------


def test_the_media_bridge_runs_on_the_real_websockets_library(world):
    pytest.importorskip("websockets")
    from websockets.sync.client import connect

    from nova.communications.media import build_media_server

    record, _ = answered(world, "stream-agent")
    server = build_media_server(world.gateway, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        with connect(f"ws://127.0.0.1:{port}/media/deepgram-cartesia") as client:
            client.send(json.dumps({"event": "connected"}))
            client.send(json.dumps({"event": "start", "streamSid": "MZ9", "start": {
                "customParameters": {"call_id": record.call_id, "token": "forged"}}}))
            with pytest.raises(Exception):
                client.recv(timeout=5)  # closed by the bridge, without a word
    finally:
        server.shutdown()
    assert world.gateway.store.call(record.call_id).status is CallStatus.ANSWERED


# -- the declaration ----------------------------------------------------------------------


def test_the_cascaded_engine_needs_its_reply_model_and_a_voice():
    from nova.errors import SpecError

    without_model = json.loads(json.dumps(SPEC))
    del without_model["communications"]["cascade"]
    with pytest.raises(SpecError, match="llm_base_url"):
        parse_communications(without_model)
    without_voice = json.loads(json.dumps(SPEC))
    del without_voice["communications"]["agents"][1]["voice"]["voice"]
    with pytest.raises(SpecError, match="Cartesia voice id"):
        parse_communications(without_voice)
