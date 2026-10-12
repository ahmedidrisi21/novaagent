"""ElevenLabs Agents over the media bridge, against a scripted agent speaking its protocol."""

from __future__ import annotations

import json
import threading

import pytest

from nova.communications.contracts import CallHooks, ProviderError, VoiceSessionRequest
from nova.communications.elevenlabs import LOOKUP_TOOL, OPT_OUT_UPDATE, AgentCall, ElevenLabsVoice

from tests.platform.test_communications_live import Scripted, wait_for

REQUEST = VoiceSessionRequest(call_id="call-1", tenant_id="acme", agent="caller",
                              instructions="Help with the order.", voice="voice-7",
                              language="en-US", delegation=True)
METADATA = {"type": "conversation_initiation_metadata",
            "conversation_initiation_metadata_event": {
                "conversation_id": "conv_1", "user_input_audio_format": "ulaw_8000",
                "agent_output_audio_format": "ulaw_8000"}}


class Rig:
    def __init__(self):
        self.media, self.agent = Scripted(), Scripted()
        self.said, self.heard, self.opted, self.ended, self.asked = [], [], [], [], []
        self.hooks = CallHooks(
            answer=lambda q: (self.asked.append(q), "Refunds within 30 days.")[1],
            opt_out=self.opted.append, end_call=lambda: self.ended.append(True),
            heard=lambda who, text: self.heard.append((who, text)),
            said=lambda who, text: self.said.append((who, text)))
        self.clock = [0.0]

    def start(self) -> threading.Thread:
        call = AgentCall(media=self.media, agent=self.agent, stream_sid="MZ1", request=REQUEST,
                         hooks=self.hooks, clock=lambda: self.clock[0])
        thread = threading.Thread(target=call.run, daemon=True)
        thread.start()
        return thread

    def to_agent(self) -> list[dict]:
        return [json.loads(m) for m in self.agent.sent]

    def to_caller(self) -> list[dict]:
        return self.media.json_sent()


@pytest.fixture
def rig():
    return Rig()


def test_the_call_opens_with_our_instructions_voice_and_call_id(rig):
    rig.agent.push(METADATA)
    thread = rig.start()
    rig.media.push({"event": "stop"})
    thread.join(timeout=5)
    opening = rig.to_agent()[0]
    assert opening["type"] == "conversation_initiation_client_data"
    override = opening["conversation_config_override"]
    assert override["agent"]["prompt"]["prompt"].startswith("Help with the order.")
    assert LOOKUP_TOOL in override["agent"]["prompt"]["prompt"]
    assert (override["agent"]["language"], override["tts"]["voice_id"]) == ("en", "voice-7")
    assert opening["dynamic_variables"]["nova_call_id"] == "call-1"


def test_an_agent_on_the_wrong_audio_format_is_refused_before_the_caller_hears_noise(rig):
    rig.agent.push({**METADATA, "conversation_initiation_metadata_event": {
        **METADATA["conversation_initiation_metadata_event"],
        "agent_output_audio_format": "pcm_16000"}})
    call = AgentCall(media=rig.media, agent=rig.agent, stream_sid="MZ1", request=REQUEST,
                     hooks=rig.hooks)
    raised = []

    def run():
        try:
            call.run()
        except ProviderError as exc:
            raised.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=2)
    rig.media.push({"event": "stop"})  # ends the call if it wrongly started
    thread.join(timeout=5)
    assert raised and "ulaw_8000" in str(raised[0])
    assert not [m for m in rig.to_caller() if m.get("event") == "media"]


def test_audio_relays_untouched_both_ways_and_barge_in_clears_playback(rig):
    rig.agent.push(METADATA)
    thread = rig.start()
    rig.media.push({"event": "media", "media": {"track": "inbound", "payload": "AAEC"}})
    rig.agent.push({"type": "audio", "audio_event": {"audio_base_64": "f39+", "event_id": 1}})
    rig.agent.push({"type": "interruption", "interruption_event": {"event_id": 2}})
    rig.agent.push({"type": "ping", "ping_event": {"event_id": 3, "ping_ms": 20}})
    wait_for(lambda: {"type": "pong", "event_id": 3} in rig.to_agent(), "pong")
    wait_for(lambda: len(rig.to_caller()) == 2, "relayed audio and clear")
    assert {"user_audio_chunk": "AAEC"} in rig.to_agent()
    assert rig.to_caller() == [
        {"event": "media", "streamSid": "MZ1", "media": {"payload": "f39+"}},
        {"event": "clear", "streamSid": "MZ1"}]
    rig.media.push({"event": "stop"})
    thread.join(timeout=5)


def test_lookups_answer_from_knowledge_and_any_other_tool_is_refused(rig):
    rig.agent.push(METADATA)
    thread = rig.start()
    rig.agent.push({"type": "client_tool_call", "client_tool_call": {
        "tool_name": LOOKUP_TOOL, "tool_call_id": "t1", "parameters": {"question": "refunds?"},
        "expects_response": True}})
    rig.agent.push({"type": "client_tool_call", "client_tool_call": {
        "tool_name": "issue_refund", "tool_call_id": "t2", "parameters": {"amount": 500}}})
    wait_for(lambda: len([m for m in rig.to_agent() if m.get("type") == "client_tool_result"]) == 2,
             "tool results")
    results = {m["tool_call_id"]: m for m in rig.to_agent() if m.get("type") == "client_tool_result"}
    assert (results["t1"]["result"], results["t1"]["is_error"]) == ("Refunds within 30 days.", False)
    assert results["t2"]["is_error"] is True and rig.asked == ["refunds?"]
    rig.media.push({"event": "stop"})
    thread.join(timeout=5)


def test_a_spoken_opt_out_suppresses_at_once_and_the_call_ends_after_goodbye(rig):
    rig.agent.push(METADATA)
    thread = rig.start()
    rig.agent.push({"type": "agent_response", "agent_response_event": {
        "agent_response": "Hi, is now a good time?", "event_id": 1}})
    rig.agent.push({"type": "user_transcript", "user_transcription_event": {
        "user_transcript": "Please stop calling me.", "event_id": 2}})
    wait_for(lambda: rig.opted, "the opt-out")
    assert {"type": "contextual_update", "text": OPT_OUT_UPDATE} in rig.to_agent()
    assert rig.said == [("agent", "Hi, is now a good time?"), ("caller", "Please stop calling me.")]
    assert not rig.ended
    rig.clock[0] = 9.0  # the goodbye window passes
    wait_for(lambda: rig.ended, "the call to end")
    thread.join(timeout=5)


def test_a_private_agent_is_reached_through_a_signed_url_not_the_key(rig):
    requests, opened = [], []

    def transport(method, url, headers, body):
        requests.append((method, url, headers))
        return 200, b'{"signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=a&conversation_signature=s"}'

    def connector(url, headers):
        opened.append((url, headers))
        rig.agent.push(METADATA)
        return rig.agent

    voice = ElevenLabsVoice(api_key="xi-key", agent_id="agent_abcdefgh12", transport=transport,
                            connector=connector, media_url="wss://calls.example.test/media/el")
    rig.media.push({"event": "stop"})
    voice.converse(rig.media, "MZ1", REQUEST, rig.hooks)
    ((method, url, headers),) = requests
    assert method == "GET" and "agent_id=agent_abcdefgh12" in url
    assert headers == {"xi-api-key": "xi-key"}
    assert opened[0][0].startswith("wss://") and opened[0][1] == {}  # no key on the socket
