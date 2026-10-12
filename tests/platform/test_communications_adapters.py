"""Vendor adapters and the answered-call flow, against scripted stand-ins for each vendor.

Signatures are checked against the vendors' own published test vectors, so an adapter
cannot pass by agreeing with itself. Nothing here opens a socket to a vendor: each adapter
takes a transport, and the stand-in answers as the vendor documents.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC

import pytest

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.assemble import build_gateway, missing_credentials
from nova.communications.contracts import (
    AnswerPlan,
    FailureReason,
    PlaceCall,
    ProviderError,
    SipTarget,
    VoiceSessionRequest,
)
from nova.communications.mock import MockTelephony
from nova.communications.openai_live import OpenAILiveVoice
from nova.communications.signing import sign
from nova.communications.twilio import TwilioTelephony, signature
from nova.communications.webhooks import build_server, handle

NOON_UTC = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
PUBLIC = "https://hooks.example.test"
TOKEN = "twilio-auth-token"
WHSEC = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


# -- vendor test vectors ------------------------------------------------------------------


def test_twilio_signature_matches_twilios_own_vectors():
    uri = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": ["CA1234567890ABCDE"], "Digits": ["1234"], "From": ["+14158675309"],
              "To": ["+18005551212"], "Caller": ["+14158675309"]}
    assert signature("12345", uri, params) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    duplicates = urllib.parse.parse_qs("Sid=CA123&SidAccount=AC123&Digits=5678&Digits=1234&Digits=1234")
    assert signature("12345", uri, duplicates) == "IK+Dwps556ElfBT0I3Rgjkr1wJU="

    carrier = TwilioTelephony("AC1", "12345", answer_url=f"{PUBLIC}/a", status_url=f"{PUBLIC}/s")
    body = urllib.parse.urlencode({k: v[0] for k, v in params.items()}).encode()
    # A proxy that adds or strips the port must not break verification.
    assert carrier.verify({"X-Twilio-Signature": "RSOYDt4T1cUTdK1PDd93/VVr8B8="}, body,
                          "https://mycompany.com:1234/myapp.php?foo=1&bar=2")
    assert carrier.verify({"X-Twilio-Signature": "kvajT1Ptam85bY51eRf/AJRuM3w="}, body, uri)


def test_webhook_signature_matches_the_standard_webhooks_example():
    assert sign("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek",
                "1614265330", b'{"test": 2432232314}') == "g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


# -- stand-ins ----------------------------------------------------------------------------


@dataclass
class FakeTwilio:
    """Answers the REST API as Twilio documents it, and signs callbacks as Twilio would."""

    requests: list = field(default_factory=list)
    error: dict | None = None
    sids: int = 0

    def __call__(self, method, url, headers, body):
        form = urllib.parse.parse_qs(body.decode()) if body else {}
        self.requests.append((method, url, form))
        if url.endswith("/Calls.json"):
            if self.error is not None:
                error, self.error = self.error, None
                return 400, json.dumps(error).encode()
            self.sids += 1
            return 201, json.dumps({"sid": f"CA{self.sids:032d}"}).encode()
        return 200, b"{}"

    @staticmethod
    def signed(url, form):
        body = urllib.parse.urlencode(form).encode()
        params = urllib.parse.parse_qs(body.decode(), keep_blank_values=True)
        return {"X-Twilio-Signature": signature(TOKEN, url, params)}, body

    def dialled(self):
        return [form for _, url, form in self.requests if url.endswith("/Calls.json")]


@dataclass
class FakeOpenAI:
    requests: list = field(default_factory=list)
    status: int = 200
    now: float = 1_760_000_000.0
    #: Seconds an accept takes. Widens the window concurrent sessions race through.
    delay: float = 0.0

    def __call__(self, method, url, headers, body):
        self.requests.append((url, json.loads(body) if body else None))
        if self.delay and url.endswith("/accept"):
            time.sleep(self.delay)
        return self.status, b""

    def incoming(self, session_id, sip_headers, *, event_id="evt_1", age=0):
        body = json.dumps({
            "id": event_id, "type": "live.transport.incoming",
            "data": {"type": "sip", "session_id": session_id,
                     "sip_headers": [{"name": k, "value": v} for k, v in sip_headers.items()]},
        }).encode()
        timestamp = str(int(self.now - age))
        return {"webhook-id": event_id, "webhook-timestamp": timestamp,
                "webhook-signature": "v1," + sign(WHSEC, event_id, timestamp, body)}, body

    def accepts(self):
        return [(url, payload) for url, payload in self.requests if url.endswith("/accept")]


def twilio(fake):
    return TwilioTelephony("AC1", TOKEN, answer_url=f"{PUBLIC}/calls/twilio/answer",
                           status_url=f"{PUBLIC}/calls/twilio/status", transport=fake)


def live(fake):
    return OpenAILiveVoice(api_key="sk-test", project_id="proj_abc", webhook_secret=WHSEC,
                           transport=fake, clock=lambda: fake.now)


# -- every carrier honours the contract ---------------------------------------------------


class MockRig:
    def __init__(self):
        self.carrier = MockTelephony()

    def status(self, ref, status, event_id):
        headers, body = self.carrier.callback(ref, status, event_id=event_id)
        return headers, body, ""

    def fail_with_invalid_number(self):
        self.carrier.fail_next = FailureReason.INVALID_NUMBER


class TwilioRig:
    def __init__(self):
        self.fake = FakeTwilio()
        self.carrier = twilio(self.fake)

    def status(self, ref, status, event_id):
        word = {CallStatus.ANSWERED: "in-progress", CallStatus.COMPLETED: "completed"}[status]
        url = f"{PUBLIC}/calls/twilio/status?call=c1"
        headers, body = self.fake.signed(url, {"CallSid": ref, "CallStatus": word,
                                               "SequenceNumber": event_id})
        return headers, body, url

    def fail_with_invalid_number(self):
        self.fake.error = {"code": 21211, "message": "Invalid 'To' Phone Number"}


@pytest.mark.parametrize("rig", [MockRig, TwilioRig])
def test_carrier_contract(rig):
    rig = rig()
    carrier = rig.carrier
    request = PlaceCall("c1", "acme", "+13125550101", "+15555550100", 60)
    ref = carrier.place_call(request)
    assert ref

    headers, body, url = rig.status(ref, CallStatus.ANSWERED, "1")
    assert carrier.verify(headers, body, url)
    assert not carrier.verify(headers, body + b"&x=1", url)
    event = carrier.parse(headers, body, url)
    assert (event.provider, event.provider_call_ref, event.status) == (carrier.id, ref, CallStatus.ANSWERED)
    with pytest.raises(ValueError):
        carrier.parse({}, b"nonsense", "")

    content_type, rendered = carrier.render_answer(AnswerPlan(hang_up=True))
    assert content_type and rendered

    carrier.end_call(ref)
    carrier.end_call(ref)  # hanging up twice is not an error

    rig.fail_with_invalid_number()
    with pytest.raises(ProviderError) as refused:
        carrier.place_call(PlaceCall("c2", "acme", "+1", "+15555550100", 60))
    assert refused.value.reason is FailureReason.INVALID_NUMBER


# -- Twilio specifics ---------------------------------------------------------------------


def test_twilio_dials_with_machine_detection_a_time_limit_and_our_call_id():
    fake = FakeTwilio()
    twilio(fake).place_call(PlaceCall("call-9", "acme", "+13125550101", "+15555550100", 240))
    form = fake.dialled()[0]
    assert form["MachineDetection"] == ["Enable"]
    assert form["TimeLimit"] == ["240"]
    assert form["Url"] == [f"{PUBLIC}/calls/twilio/answer?call=call-9"]
    assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]


def test_twilio_reports_a_voicemail_greeting_as_a_machine():
    fake = FakeTwilio()
    url = f"{PUBLIC}/calls/twilio/answer?call=call-9"
    headers, body = fake.signed(url, {"CallSid": "CA1", "CallStatus": "in-progress",
                                      "AnsweredBy": "machine_start"})
    event = twilio(fake).parse(headers, body, url)
    assert (event.status, event.failure) == (CallStatus.FAILED, FailureReason.MACHINE_DETECTED)
    assert event.call_id_hint == "call-9"


def test_twilio_twiml_speaks_the_disclosure_verbatim_and_escaped():
    plan = AnswerPlan(say='We <record> & "transcribe"', language="en-US",
                      connect=SipTarget("sip:proj_abc@sip.api.openai.com;transport=tls",
                                        {"X-Call-Id": "c1", "X-Call-Token": "t"}))
    _, twiml = twilio(FakeTwilio()).render_answer(plan)
    text = twiml.decode()
    assert "We &lt;record&gt; &amp; \"transcribe\"" in text
    assert text.index("<Say") < text.index("<Dial>")
    assert "X-Call-Token=t" in text


# -- GPT-Live specifics -------------------------------------------------------------------


def test_live_refuses_a_stale_or_forged_webhook():
    fake = FakeOpenAI()
    voice = live(fake)
    headers, body = fake.incoming("sess_1", {})
    assert voice.verify(headers, body)
    assert not voice.verify(headers, body.replace(b"sess_1", b"sess_2"))
    stale_headers, stale_body = fake.incoming("sess_1", {}, age=301)
    assert not voice.verify(stale_headers, stale_body)


def test_live_accepts_with_the_purpose_instructions_and_the_agents_voice():
    fake = FakeOpenAI()
    live(fake).accept("sess_1", VoiceSessionRequest("c1", "acme", "agent", "Confirm the delivery.",
                                                    voice="cedar", language="es-MX"))
    url, payload = fake.accepts()[0]
    assert url.endswith("/live/sessions/sess_1/accept")
    session = payload["session"]
    assert session["type"] == "live" and session["audio"]["output"]["voice"] == "cedar"
    assert session["instructions"].startswith("Confirm the delivery.")
    assert "es-MX" in session["instructions"]


# -- the answered call, end to end --------------------------------------------------------

SPEC = {
    "communications": {
        "disclosure": "Hi, this is an AI assistant calling for Acme. This call is not recorded.",
        "voice_providers": ["openai-live"],
        "telephony_providers": ["twilio"],
        "caller_ids": ["+15555550100"],
        "purposes": [{"id": "delivery", "instructions": "Confirm the delivery window."}],
        "agents": [{
            "agent": "caller", "telephony": "twilio", "caller_id": "+15555550100",
            "voice": {"primary": "openai-live", "voice": "cedar"},
            "purposes": ["delivery"], "contact_groups": ["customers"], "approval": "never",
        }],
    }
}


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "comms.sqlite", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    tw, oa = FakeTwilio(), FakeOpenAI()
    gateway = CallGateway(
        parse_communications(SPEC), store, AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
        {"twilio": twilio(tw)}, voices={"openai-live": live(oa)},
        call_secret=b"a-bridge-secret-of-enough-length", clock=lambda: NOON_UTC,
    )
    yield gateway, tw, oa
    store.close()


def post(gateway, path, headers, body):
    return handle(gateway, PUBLIC, path, headers, body)


def answer(gateway, tw, record, answered_by="human"):
    path = f"/calls/twilio/answer?call={record.call_id}"
    headers, body = tw.signed(PUBLIC + path, {"CallSid": record.provider_call_ref,
                                              "CallStatus": "in-progress", "AnsweredBy": answered_by})
    return post(gateway, path, headers, body)


def bridged_headers(twiml):
    sip = urllib.parse.unquote(twiml.decode().split("<Sip>")[1].split("</Sip>")[0].replace("&amp;", "&"))
    return dict(urllib.parse.parse_qsl(sip.split("?", 1)[1]))


def test_a_person_hears_the_disclosure_then_the_engine_gets_the_purpose(world):
    gateway, tw, oa = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record

    status, content_type, twiml = answer(gateway, tw, record)
    assert (status, content_type) == (200, "text/xml")
    assert "This call is not recorded." in twiml.decode()
    sip_headers = bridged_headers(twiml)

    headers, body = oa.incoming("sess_1", sip_headers)
    assert post(gateway, "/voice/openai-live/events", headers, body)[0] == 200
    assert gateway.store.call(record.call_id).status is CallStatus.SESSION_CONNECTED
    (_, payload), = oa.accepts()
    assert payload["session"]["instructions"].startswith("Confirm the delivery window.")

    path = f"/calls/twilio/status?call={record.call_id}"
    headers, body = tw.signed(PUBLIC + path, {"CallSid": record.provider_call_ref,
                                              "CallStatus": "completed", "SequenceNumber": "3"})
    post(gateway, path, headers, body)
    assert gateway.store.call(record.call_id).status is CallStatus.COMPLETED


def test_a_voicemail_greeting_hears_nothing_and_no_engine_is_connected(world):
    gateway, tw, oa = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record
    _, _, twiml = answer(gateway, tw, record, answered_by="machine_end_beep")
    assert "<Hangup/>" in twiml.decode() and "<Say" not in twiml.decode()
    stored = gateway.store.call(record.call_id)
    assert (stored.status, stored.failure) == (CallStatus.FAILED, "machine_detected")

    headers, body = oa.incoming("sess_1", {"X-Call-Id": record.call_id,
                                            "X-Call-Token": gateway.call_token(record.call_id)})
    post(gateway, "/voice/openai-live/events", headers, body)
    assert not oa.accepts()


def test_a_session_claiming_a_call_without_its_token_is_rejected(world):
    gateway, tw, oa = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record
    answer(gateway, tw, record)
    headers, body = oa.incoming("sess_evil", {"X-Call-Id": record.call_id, "X-Call-Token": "guess"})
    post(gateway, "/voice/openai-live/events", headers, body)
    assert not oa.accepts()
    assert any(url.endswith("/sess_evil/reject") for url, _ in oa.requests)
    assert gateway.store.call(record.call_id).status is CallStatus.ANSWERED


def test_a_call_takes_one_voice_session_however_many_arrive_at_once(world):
    gateway, tw, oa = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record
    sip_headers = bridged_headers(answer(gateway, tw, record)[2])
    oa.delay = 0.2
    deliveries = [oa.incoming(f"sess_{i}", sip_headers, event_id=f"evt_{i}") for i in range(4)]
    barrier = threading.Barrier(len(deliveries))

    def deliver(headers, body):
        barrier.wait()
        gateway.voice_event("openai-live", headers=headers, body=body)

    threads = [threading.Thread(target=deliver, args=d) for d in deliveries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(oa.accepts()) == 1


def test_an_engine_that_refuses_the_session_ends_the_phone_leg(world):
    gateway, tw, oa = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record
    sip_headers = bridged_headers(answer(gateway, tw, record)[2])
    oa.status = 500
    headers, body = oa.incoming("sess_1", sip_headers)
    post(gateway, "/voice/openai-live/events", headers, body)
    assert gateway.store.call(record.call_id).status is CallStatus.FAILED
    assert any(url.endswith(f"/Calls/{record.provider_call_ref}.json") and form == {"Status": ["completed"]}
               for _, url, form in tw.requests)


def test_a_status_callback_that_beats_the_carrier_reference_still_lands(world):
    gateway, tw, _ = world
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="delivery",
                                  idempotency_key="k1").record
    gateway.store._write("UPDATE calls SET provider_call_ref = '' WHERE call_id = ?", record.call_id)
    path = f"/calls/twilio/status?call={record.call_id}"
    headers, body = tw.signed(PUBLIC + path, {"CallSid": "CA-early", "CallStatus": "ringing",
                                              "SequenceNumber": "1"})
    assert post(gateway, path, headers, body)[0] == 200
    stored = gateway.store.call(record.call_id)
    assert (stored.status, stored.provider_call_ref) == (CallStatus.RINGING, "CA-early")


def test_the_listener_refuses_an_unsigned_request(world):
    gateway, _, _ = world
    server = build_server(gateway, public_url=PUBLIC, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/calls/twilio/status",
            data=b"CallSid=CA1&CallStatus=completed", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 403
    finally:
        server.shutdown()


# -- assembly -----------------------------------------------------------------------------


def test_assembly_names_missing_credentials_and_wires_what_is_present(tmp_path):
    spec = parse_communications(SPEC)
    assert missing_credentials(spec, {}) == {
        "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"],
        "openai-live": ["OPENAI_API_KEY", "OPENAI_PROJECT_ID", "OPENAI_WEBHOOK_SECRET"],
        "bridge": ["CALL_BRIDGE_SECRET"],
    }
    env = {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "OPENAI_API_KEY": "sk",
           "OPENAI_PROJECT_ID": "proj_x", "OPENAI_WEBHOOK_SECRET": WHSEC,
           "CALL_BRIDGE_SECRET": "x" * 32}
    store = CommsStore(tmp_path / "c.sqlite", tenant_id="acme")
    gateway = build_gateway(spec, store=store, audit=AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                            env=env, public_url=PUBLIC)
    assert set(gateway.carriers) == {"twilio"} and set(gateway.voices) == {"openai-live"}
    store.close()
