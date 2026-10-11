"""From a finished call to company data: transcripts, verified fields, governed writes."""

from __future__ import annotations

import copy
import json
import shutil
import threading
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.cascade import Responder
from nova.communications.contracts import ProviderError
from nova.communications.extraction import process_call, redact, verify
from nova.communications.mock import MockTelephony
from nova.communications.openai_live import LiveSideband
from nova.communications.spec import FieldSpec
from nova.communications.sweep import sweep
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE
from tests.platform.test_communications_live import Scripted

START = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

SPEC = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "extraction": {"llm_base_url": "https://llm.example.test/v1", "llm_model": "reader"},
    "purposes": [{"id": "sales", "fields": [
        {"name": "budget", "kind": "number", "contact_attribute": "budget", "auto_apply": 0.8},
        {"name": "interest", "kind": "choice", "options": ["high", "low"],
         "contact_attribute": "interest"},
        {"name": "next_call", "kind": "date"},
    ]}],
    "agents": [{"agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"}, "purposes": ["sales"],
                "contact_groups": ["customers"], "approval": "never"}],
}}


def spec_with(**profile):
    data = copy.deepcopy(SPEC)
    data["communications"]["agents"][0].update(profile)
    return parse_communications(data)


class Reader(Responder):
    """The extraction model's stand-in: answers with a scripted reading."""

    def __init__(self, reading=None, fail=False):
        self.reading, self.fail, self.calls = reading or {}, fail, []

    def reply(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise ProviderError("model down")
        return "```json\n" + json.dumps(self.reading) + "\n```"


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    clock = {"now": START}

    def make(spec=None, reader=None):
        carrier = MockTelephony()
        gateway = CallGateway(spec or parse_communications(SPEC), store,
                              AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                              {"mock-telephony": carrier}, extractor=reader,
                              clock=lambda: clock["now"])
        record = gateway.request_call(agent="caller", contact_id="c1", purpose="sales",
                                      idempotency_key=f"k{len(store.recent_calls())}").record
        return gateway, carrier, record

    yield make, store, clock
    store.close()


def finish(gateway, carrier, record, turns):
    said = gateway.transcriber(record)
    for speaker, text in turns:
        said(speaker, text)
    headers, body = carrier.callback(record.provider_call_ref, CallStatus.COMPLETED, event_id="done")
    gateway.ingest("mock-telephony", headers=headers, body=body)


TURNS = [("agent", "Hi, is now a good time to talk about the upgrade?"),
         ("caller", "Sure. We'd budget around 12,000 for it and we're very keen.")]
READING = {"summary": "Interested in the upgrade.", "fields": {
    "budget": {"value": "12,000", "confidence": 0.92, "quote": "budget around 12,000"},
    "interest": {"value": "High", "confidence": 0.7, "quote": "we're very keen"},
    "next_call": {"value": "next week", "confidence": 0.9, "quote": "next week"},
}}


def test_redaction_removes_numbers_before_anything_is_written():
    assert redact("card 4111 1111 1111 1111 exp 12/27") == "card [number] exp 12/27"
    assert redact("ssn 123-45-6789") == "ssn [number]"


def test_a_confident_quoted_value_is_written_with_its_evidence_and_a_weak_one_is_queued(world):
    make, store, _ = world
    gateway, carrier, record = make(reader=Reader(READING))
    finish(gateway, carrier, record, TURNS)
    assert sweep(gateway)  # extraction runs from the sweep

    budget = store.attribute("c1", "budget")
    assert (budget["value"], budget["source"], budget["call_id"]) == ("12000", "call", record.call_id)
    assert budget["quote"] == "budget around 12,000"
    assert store.attribute("c1", "interest") is None
    (queued,) = store.reviews()
    assert (queued["name"], queued["value"]) == ("interest", "high")  # coerced to the option
    fields = {f["name"]: f for f in store.extraction(record.call_id)["fields"]}
    assert "next_call" not in fields  # "next week" is not a date: not recorded at all
    assert store.turns(record.call_id) == []  # deleted once extracted, by default


def test_an_invented_quote_never_writes_however_confident_the_model_is(world):
    make, store, _ = world
    reading = {"fields": {"budget": {"value": 99000, "confidence": 1.0,
                                     "quote": "our budget is 99,000"}}}
    gateway, carrier, record = make(reader=Reader(reading))
    finish(gateway, carrier, record, TURNS)
    process_call(gateway, record.call_id, gateway.extractor)
    assert store.attribute("c1", "budget") is None
    (queued,) = store.reviews()
    assert queued["confidence"] == 0.0 and "quote not found" in queued["reason"]


def test_a_call_never_overwrites_a_value_a_person_entered(world):
    make, store, _ = world
    store.set_attribute("c1", "budget", "8000", source="person", at="2026-10-01T00:00:00Z")
    gateway, carrier, record = make(reader=Reader(READING))
    finish(gateway, carrier, record, TURNS)
    process_call(gateway, record.call_id, gateway.extractor)
    assert store.attribute("c1", "budget")["value"] == "8000"
    budget = [r for r in store.reviews() if r["name"] == "budget"][0]
    assert (budget["current_value"], budget["reason"]) == ("8000", "a person entered the current value")


def test_an_approved_review_becomes_a_persons_value_and_cannot_be_decided_twice(world):
    from nova.communications.extraction import decide_review

    make, store, _ = world
    gateway, carrier, record = make(reader=Reader(READING))
    finish(gateway, carrier, record, TURNS)
    process_call(gateway, record.call_id, gateway.extractor)
    (queued,) = store.reviews()
    decide_review(gateway, queued["review_id"], approve=True, actor="alice")
    assert store.attribute("c1", "interest")["source"] == "person"
    with pytest.raises(ValueError):
        decide_review(gateway, queued["review_id"], approve=False, actor="bob")


def test_a_spoken_opt_out_is_honoured_without_any_model(world):
    make, store, _ = world
    gateway, carrier, record = make(spec=parse_communications(
        {"communications": {**SPEC["communications"], "purposes": [{"id": "sales"}]}}))
    finish(gateway, carrier, record, [("caller", "Please take me off your list.")])
    sweep(gateway)
    assert store.is_suppressed("c1")


def test_a_failed_model_writes_nothing_and_is_not_retried_in_a_loop(world):
    make, store, _ = world
    reader = Reader(fail=True)
    gateway, carrier, record = make(reader=reader)
    finish(gateway, carrier, record, TURNS)
    sweep(gateway)
    sweep(gateway)
    assert len(reader.calls) == 1
    assert store.extraction(record.call_id)["error"] == "model down"
    assert store.attributes("c1") == {} and store.reviews() == []


def test_a_transcript_waits_for_a_model_and_a_kept_one_expires(world):
    make, store, clock = world
    gateway, carrier, record = make(spec=spec_with(keep_transcript_days=7), reader=None)
    finish(gateway, carrier, record, TURNS)
    sweep(gateway)
    assert store.turns(record.call_id)  # no model configured yet: nothing lost

    gateway.extractor = Reader(READING)
    sweep(gateway)
    assert store.turns(record.call_id)  # kept for 7 days
    clock["now"] = START + timedelta(days=8)
    sweep(gateway)
    assert store.turns(record.call_id) == []


def test_live_transcript_fragments_become_whole_turns():
    channel, turns = Scripted(), []
    sideband = threading.Thread(target=LiveSideband(channel, None, None,
                                                    lambda who, text: turns.append((who, text))).run)
    sideband.start()
    for kind, delta in (("output", "Hello, is this "), ("output", "a good time?"),
                        ("input", "Yes, "), ("input", "go ahead."), ("output", "Great.")):
        channel.push({"type": f"session.{kind}_transcript.delta", "delta": delta})
    channel.push({"type": "session.closed"})
    sideband.join(timeout=5)
    assert turns == [("agent", "Hello, is this a good time?"), ("caller", "Yes, go ahead."),
                     ("agent", "Great.")]


def test_fields_need_a_reading_model_and_choices_need_options():
    without_model = copy.deepcopy(SPEC)
    del without_model["communications"]["extraction"]
    with pytest.raises(SpecError, match="extraction"):
        parse_communications(without_model)
    bad = copy.deepcopy(SPEC)
    bad["communications"]["purposes"][0]["fields"] = [{"name": "tier", "kind": "choice"}]
    with pytest.raises(SpecError, match="options"):
        parse_communications(bad)


def test_verification_types_every_value_against_its_field():
    fields = (FieldSpec("n", "number"), FieldSpec("ok", "boolean"), FieldSpec("d", "date"))
    reading = {"fields": {"n": {"value": "1,250.5", "confidence": 0.9, "quote": "1,250.50"},
                          "ok": {"value": "yes", "confidence": 0.9, "quote": "that works"},
                          "d": {"value": "2026-11-03", "confidence": 2, "quote": "november third"}}}
    got = {v["name"]: v for v in verify(fields, reading, "caller: 1,250.50 that works november third")}
    assert (got["n"]["value"], got["ok"]["value"], got["d"]["value"]) == ("1250.5", "true", "2026-11-03")
    assert got["d"]["confidence"] == 1.0


# -- the review queue over the Control API ------------------------------------------------


def test_reviews_are_listed_and_decided_by_admins_only(tmp_path, runtime, monkeypatch):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    comms = copy.deepcopy(SPEC)
    comms["communications"]["agents"][0]["agent"] = "customer-support"
    (root / "communications.yaml").write_text(yaml.safe_dump(comms))
    bundle = load_bundle(root)
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    review_id = store.enqueue_review(contact_id="c1", name="interest", value="high",
                                     current_value="", call_id="call-1", quote="we're keen",
                                     confidence=0.6, reason="below threshold",
                                     at="2026-10-12T00:00:00Z")
    store.close()
    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl", tenant_id=bundle.tenant_id))

    assert not Principal(name="r", role="viewer").may("/communications/reviews")
    (listed,) = control.handle("/platform/v1/communications/reviews").body["reviews"]
    assert listed["quote"] == "we're keen"
    path = f"/platform/v1/communications/reviews/{review_id}/decide"
    viewer = Principal(name="r", role="viewer")
    assert control.write(path, viewer, {"action": "approve"}).status == 403
    admin = Principal(name="alice", role="admin")
    assert control.write(path, admin, {"action": "approve"}).body["decided_by"] == "alice"
    assert control.write(path, admin, {"action": "approve"}).status == 409
    contact = control.handle("/platform/v1/communications/contacts/c1").body
    assert contact["fields"]["interest"]["source"] == "person"
