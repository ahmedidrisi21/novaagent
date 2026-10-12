"""The weekly digest: counts that say how calls went, and nothing about who was called."""

from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.contracts import FailureReason
from nova.communications.insights import deliver_weekly, digest, last_week, render
from nova.communications.mock import MockEmail, MockTelephony
from nova.communications.spec import check_agents_exist
from nova.communications.sweep import sweep
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError

from tests.platform.conftest import EXAMPLE_BUNDLE
from tests.platform.test_communications_actions import Board

MONDAY = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)   # a Monday
LAST_WEEK = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)  # the Wednesday before

SPEC = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "public_url": "https://calls.example.test",
    "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "email": {"provider": "mock-email", "from": "Acme <a@acme.example>"},
    "budget": {"rates": {"mock-telephony": {"per_minute": 0.02},
                         "mock-voice": {"per_minute": 0.08}}},
    "insights": {"assignee": "ops", "email_to": ["team@acme.example"], "weekday": "monday"},
    "purposes": [{"id": "support"}],
    "agents": [{"agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"}, "purposes": ["support"],
                "contact_groups": ["customers"], "approval": "never",
                "max_concurrent_calls": 10, "max_calls_per_day": 50}],
}}


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    for n in (1, 2, 3):
        store.put_contact(Contact(f"c{n}", f"+1312555010{n}", "America/Chicago", ("customers",)))
        store.grant_consent(f"c{n}", "ai_voice", evidence="form", at="2026-10-01T00:00:00Z")
    clock = {"now": LAST_WEEK}
    rig = {"carrier": MockTelephony(), "board": Board(), "email": MockEmail()}

    def make(spec=None):
        return CallGateway(parse_communications(spec or SPEC), store,
                           AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                           {"mock-telephony": rig["carrier"]}, tasks=rig["board"],
                           email=rig["email"], clock=lambda: clock["now"])

    yield make, store, clock, rig
    store.close()


def a_week_of_calls(gateway, rig, store):
    done = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                idempotency_key="done").record
    store.set_voice(done.call_id, engine="mock-voice", at="2026-10-07T15:00:01.000Z")
    for status, event in ((CallStatus.ANSWERED, "a"), (CallStatus.SESSION_CONNECTED, "s")):
        store.advance(done.call_id, status, at=f"2026-10-07T15:00:0{event == 's' and 2 or 1}.000Z")
    headers, body = rig["carrier"].callback(done.provider_call_ref, CallStatus.COMPLETED,
                                            event_id="done-c", duration=125)
    gateway.ingest("mock-telephony", headers=headers, body=body)
    store.set_recording_consent(done.call_id, "granted", "1", at="2026-10-07T15:00:01.000Z")

    busy = gateway.request_call(agent="caller", contact_id="c2", purpose="support",
                                idempotency_key="busy").record
    headers, body = rig["carrier"].callback(busy.provider_call_ref, CallStatus.FAILED,
                                            event_id="busy-f", failure=FailureReason.BUSY)
    gateway.ingest("mock-telephony", headers=headers, body=body)

    store.revoke_consent("c3", "ai_voice")
    gateway.request_call(agent="caller", contact_id="c3", purpose="support", idempotency_key="no")
    gateway.opt_out("c2", reason="asked", source="test")
    store.suppress_channel("c1", "email", reason="unsubscribed", at="2026-10-07T16:00:00.000Z")
    store.record_engine_failure("mock-voice", done.call_id, "refused", at="2026-10-07T15:00:00.000Z")


def test_the_digest_counts_outcomes_and_names_nobody(world):
    make, store, clock, rig = world
    gateway = make()
    a_week_of_calls(gateway, rig, store)
    since, until, period = last_week(MONDAY)
    assert (since, until, period) == ("2026-10-05T00:00:00.000Z", "2026-10-12T00:00:00.000Z",
                                      "2026-W41")
    report = digest(gateway, since=since, until=until)
    calls = report["calls"]
    assert (calls["requested"], calls["connected"], calls["completed"], calls["blocked"]) == (3, 1, 1, 1)
    assert calls["failed"] == {"busy": 1} and calls["minutes"] == 3
    assert "consent" in report["blocked_reasons"][0]["reason"]
    assert report["recording_consent"]["granted"] == 1
    assert report["opt_outs"] == {"all_channels": 1, "email_unsubscribed": 1}
    assert report["engines"]["failures"] == {"mock-voice": 1}
    assert report["spend"]["estimated"] == pytest.approx(3 * 0.10)

    text = render(report, "acme") + json.dumps(report)
    for private in ("c1", "c2", "c3", "+1312555010", "5550101"):
        assert private not in text


def test_the_digest_goes_out_once_on_its_day_and_waits_until_it_can(world, tmp_path):
    make, store, clock, rig = world
    gateway = make()
    a_week_of_calls(gateway, rig, store)
    clock["now"] = MONDAY - timedelta(days=1)  # Sunday: not the day
    assert deliver_weekly(gateway) == ""

    clock["now"] = MONDAY
    gateway.tasks = None  # no board yet: nothing is marked sent
    assert deliver_weekly(gateway) == ""
    gateway.tasks = rig["board"]
    sweep(gateway)
    sweep(gateway)
    (item,) = rig["board"].items.values()
    assert item.assignee == "ops" and item.key == "insights:2026-W41"
    assert "3 requested, 1 connected" in item.body
    ((_, sent),) = rig["email"].sent.values()
    assert sent.to == "team@acme.example" and "2026-W41" in sent.subject
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert kinds.count("communications.insights.delivered") == 1


@pytest.mark.parametrize("insights, message", [
    ({"email_to": ["not-an-address"]}, "not an email address"),
    ({}, "name an assignee"),
    ({"weekday": "someday", "assignee": "ops"}, "must be one of"),
])
def test_the_spec_refuses_a_digest_it_cannot_deliver(insights, message):
    data = copy.deepcopy(SPEC)
    data["communications"]["insights"] = insights
    with pytest.raises(SpecError, match=message):
        parse_communications(data)


def test_emailing_the_digest_needs_a_sender_and_its_assignee_must_exist():
    data = copy.deepcopy(SPEC)
    del data["communications"]["email"]
    with pytest.raises(SpecError, match="sender"):
        parse_communications(data)
    with pytest.raises(SpecError, match="ops"):
        check_agents_exist(parse_communications(SPEC), ["caller"])


def test_insights_are_an_admin_route(tmp_path, runtime):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    comms = copy.deepcopy(SPEC)
    comms["communications"]["agents"][0]["agent"] = "customer-support"
    comms["communications"]["insights"]["assignee"] = "operations"
    (root / "communications.yaml").write_text(yaml.safe_dump(comms))
    from nova.spec import load_bundle

    bundle = load_bundle(root)
    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl",
                                                         tenant_id=bundle.tenant_id))
    assert not Principal(name="v", role="viewer").may("/communications/insights")
    report = control.handle("/platform/v1/communications/insights").body
    assert report["calls"]["requested"] == 0
    assert control.handle("/platform/v1/communications/insights", {"days": "x"}).status == 400
