"""Recording: consent asked by the carrier, payment details never captured, deletion on time."""

from __future__ import annotations

import json
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone, UTC

import pytest

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.cascade import CascadedCall, Responder
from nova.communications.contracts import AnswerPlan, CallHooks, ConsentPrompt, VoiceSessionRequest
from nova.communications.mock import MockTelephony, MockVoice
from nova.communications.recording import (
    GRANTED,
    REFUSED,
    UNCLEAR,
    PaymentGuard,
    classify_consent,
    consent_reply,
    forget_contact,
    recording_event,
)
from nova.communications.sweep import sweep
from nova.communications.twilio import TwilioTelephony
from nova.errors import SpecError

from tests.platform.test_communications_live import Scripted, tts_stand_in, wait_for

START = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize("digits, speech, outcome", [
    ("1", "", GRANTED),
    ("2", "", REFUSED),
    ("", "Yes.", GRANTED),
    ("", "sure, go ahead", GRANTED),
    ("", "no problem", GRANTED),
    ("", "No thanks.", REFUSED),
    ("", "I'd rather not", REFUSED),
    ("", "yes... actually no", UNCLEAR),
    ("", "no, no problem", UNCLEAR),
    ("", "what is this about?", UNCLEAR),
    ("", "", UNCLEAR),
    ("7", "", UNCLEAR),
])
def test_only_an_explicit_yes_is_consent(digits, speech, outcome):
    assert classify_consent(digits, speech)[0] == outcome


def test_a_payment_guard_pauses_before_the_number_and_resumes_after_ordinary_talk():
    now = [0.0]
    actions: list[str] = []
    guard = PaymentGuard(lambda: actions.append("pause"), lambda: actions.append("resume"),
                         clock=lambda: now[0])
    guard.heard("agent", "I can take your card number now")
    guard.heard("caller", "it's 4111 1111")
    now[0] = 10
    guard.heard("caller", "and the expiry is next year")
    now[0] = 25
    guard.heard("caller", "that's everything thanks")
    now[0] = 31
    guard.heard("caller", "what else do you need from me")
    assert actions == ["pause", "resume"]


# -- the declaration ----------------------------------------------------------------------

def spec(on_refusal="continue", **recording):
    return {"communications": {
        "allow_test_providers": True,
        "disclosure": "This is an AI assistant calling for Acme.",
        "consent_prompt": "May we record? Press 1 or say yes.",
        "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
        "caller_ids": ["+15555550100"], "purposes": [{"id": "support"}],
        "agents": [{
            "agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
            "voice": {"primary": "mock-voice"}, "purposes": ["support"],
            "contact_groups": ["customers"], "approval": "never",
            "recording": {"enabled": True, "on_refusal": on_refusal, "retention_days": 30,
                          "legal_basis": "Quality assurance; all-party consent asked on every "
                                         "call; reviewed by counsel 2026-10.", **recording},
        }],
    }}


def test_recording_cannot_be_switched_on_without_a_stated_legal_basis():
    with pytest.raises(SpecError, match="legal_basis"):
        parse_communications(spec(legal_basis="ok"))


# -- the consent flow at the carrier -------------------------------------------------------


@pytest.fixture
def make(tmp_path):
    stores = []

    def build(on_refusal="continue"):
        store = CommsStore(tmp_path / f"c{len(stores)}.db", tenant_id="acme")
        stores.append(store)
        store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
        store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
        carrier, voice, clock = MockTelephony(), MockVoice(), {"now": START}
        gateway = CallGateway(parse_communications(spec(on_refusal)), store,
                              AuditLog(tmp_path / f"a{len(stores)}.jsonl", tenant_id="acme"),
                              {"mock-telephony": carrier}, voices={"mock-voice": voice},
                              call_secret=b"a-bridge-secret-of-enough-length",
                              clock=lambda: clock["now"])
        record = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                      idempotency_key="k1").record
        headers, body = carrier.callback(record.provider_call_ref, CallStatus.ANSWERED, event_id="a")
        plan = json.loads(gateway.answer("mock-telephony", headers=headers, body=body).body)
        return gateway, carrier, voice, record, plan, clock

    yield build
    for store in stores:
        store.close()


def reply(gateway, carrier, record, **answer):
    headers, body = carrier.signed({"call_ref": record.provider_call_ref, **answer})
    return json.loads(consent_reply(gateway, "mock-telephony", headers=headers, body=body).body)


def test_consent_is_asked_before_any_engine_and_a_yes_starts_one_recording(make):
    gateway, carrier, _, record, plan, _ = make()
    assert plan == {"hang_up": False, "say": "This is an AI assistant calling for Acme.",
                    "ask_consent": "May we record? Press 1 or say yes."}
    assert not carrier.recordings

    connected = reply(gateway, carrier, record, digits="1")
    assert connected["uri"] == "sip:agent@voice.test"
    assert list(carrier.recordings.values()) == ["recording"]
    stored = gateway.store.call(record.call_id)
    assert (stored.recording_consent, stored.recording_evidence) == ("granted", "pressed 1")

    reply(gateway, carrier, record, digits="1")  # the carrier retries the callback
    assert len(carrier.recordings) == 1


@pytest.mark.parametrize("answer", [{"speech": "no thanks"}, {}])
def test_a_no_or_silence_means_an_unrecorded_call(make, answer):
    gateway, carrier, _, record, _, _ = make()
    connected = reply(gateway, carrier, record, **answer)
    assert connected["uri"] and not carrier.recordings
    assert gateway.store.call(record.call_id).recording_consent in ("refused", "unclear")


def test_a_profile_that_ends_on_refusal_says_goodbye_and_hangs_up(make):
    gateway, carrier, _, record, _, _ = make(on_refusal="end")
    assert reply(gateway, carrier, record, speech="no")["hang_up"] is True
    assert gateway.store.call(record.call_id).status is CallStatus.CANCELLED


def test_the_guard_on_a_sip_session_pauses_the_carrier_recording(make):
    gateway, carrier, voice, record, _, _ = make()
    connected = reply(gateway, carrier, record, digits="1")
    headers, body = voice.incoming(session_id="s1", event_id="e1", sip_headers=connected["headers"])
    gateway.voice_event("mock-voice", headers=headers, body=body)
    hooks = voice.attached["s1"]
    assert voice.accepted["s1"].delegation is False and hooks.answer is None
    hooks.heard("caller", "my card number is 4242 4242")
    assert list(carrier.recordings.values()) == ["paused"]
    (recording,) = gateway.store.recordings(call_id=record.call_id)
    assert recording["state"] == "paused"


def test_recordings_are_deleted_when_retention_ends_and_on_request(make):
    gateway, carrier, _, record, _, clock = make()
    reply(gateway, carrier, record, digits="1")
    (ref,) = carrier.recordings
    headers, body = carrier.signed({"recording_ref": ref, "call_ref": record.provider_call_ref,
                                    "call_id": record.call_id, "status": "completed",
                                    "duration": 95})
    assert recording_event(gateway, "mock-telephony", headers=headers, body=body).outcome == "applied"
    assert gateway.store.recordings(call_id=record.call_id)[0]["duration_seconds"] == 95

    clock["now"] = START + timedelta(days=29)
    sweep(gateway)
    assert carrier.recordings[ref] == "recording"  # inside retention: kept
    clock["now"] = START + timedelta(days=31)
    sweep(gateway)
    assert carrier.recordings[ref] == "deleted"
    assert gateway.store.recordings(call_id=record.call_id) == []


def test_a_contacts_recordings_can_be_deleted_on_request(make):
    gateway, carrier, _, record, _, _ = make()
    reply(gateway, carrier, record, digits="1")
    assert forget_contact(gateway, "c1", actor="dpo") == 1
    assert list(carrier.recordings.values()) == ["deleted"]


# -- the cascaded engine feeds the guard partial results ---------------------------------


class Quiet(Responder):
    def reply(self, messages):
        return "Hello."


def test_the_cascaded_engine_reports_partial_speech_before_the_utterance_ends():
    media, stt, hold = Scripted(), Scripted(), threading.Event()
    tts = Scripted(on_send=tts_stand_in(hold))
    heard: list[tuple[str, str]] = []
    call = CascadedCall(media=media, stt=stt, tts=tts, stream_sid="MZ1",
                        request=VoiceSessionRequest("c1", "acme", "a", "Help.", voice="v"),
                        hooks=CallHooks(heard=lambda who, text: heard.append((who, text))),
                        responder=Quiet())
    runner = threading.Thread(target=call.run, daemon=True)
    runner.start()
    wait_for(lambda: ("agent", "Hello.") in heard, "the agent's words")
    stt.push({"type": "Results", "is_final": False, "speech_final": False,
              "channel": {"alternatives": [{"transcript": "my card is 4111 1111"}]}})
    wait_for(lambda: ("caller", "my card is 4111 1111") in heard, "the partial result")
    media.close()
    runner.join(timeout=10)


# -- Twilio --------------------------------------------------------------------------------


def test_twilio_records_both_parties_pauses_by_skipping_and_asks_with_silence_reported():
    requests = []

    def transport(method, url, headers, body):
        requests.append((method, url, urllib.parse.parse_qs(body.decode()) if body else {}))
        if url.endswith("/Recordings.json"):
            return 201, json.dumps({"sid": "RE1"}).encode()
        return (404, b"{}") if method == "DELETE" else (200, b"{}")

    twilio = TwilioTelephony("AC1", "t", answer_url="https://h.test/calls/twilio/answer",
                             status_url="https://h.test/calls/twilio/status", transport=transport)
    assert twilio.start_recording("c1", "CA1") == "RE1"
    _, url, form = requests[0]
    assert url.endswith("/Calls/CA1/Recordings.json")
    assert form["RecordingChannels"] == ["dual"]
    assert form["RecordingStatusCallback"] == ["https://h.test/calls/twilio/recording?call=c1"]
    twilio.set_recording_paused("CA1", "RE1", True)
    assert requests[1][2] == {"Status": ["paused"], "PauseBehavior": ["skip"]}
    twilio.delete_recording("RE1")  # already gone at the carrier: not an error
    assert requests[2][0] == "DELETE"

    _, twiml = twilio.render_answer(AnswerPlan(say="AI calling.", consent=ConsentPrompt("c1", "Record?")))
    text = twiml.decode()
    assert 'actionOnEmptyResult="true"' in text
    assert 'action="https://h.test/calls/twilio/consent?call=c1"' in text
    assert "<Dial" not in text and "<Connect" not in text
    _, goodbye = twilio.render_answer(AnswerPlan(hang_up=True, say="Goodbye."))
    assert goodbye.decode().endswith("<Say>Goodbye.</Say><Hangup/></Response>")
