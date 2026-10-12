"""What calls cost, the budget that stops them, and moving a call when its engine fails."""

from __future__ import annotations

import copy
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.contracts import AnswerPlan, Billing, SipTarget
from nova.communications.history import timeline
from nova.communications.mock import MockTelephony, MockVoice
from nova.communications.spec import check_agents_exist
from nova.communications.sweep import sweep
from nova.communications.twilio import TwilioTelephony
from nova.communications.usage import month_to_date
from nova.errors import SpecError

from tests.platform.test_communications_actions import Board

START = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

SPEC = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "voice_providers": ["mock-voice", "openai-live"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "purposes": [{"id": "support"}],
    "budget": {"monthly_limit": 1.0, "alert_at": [50, 100], "alert_assignee": "ops",
               "rates": {"mock-telephony": {"per_minute": 0.02},
                         "mock-voice": {"per_minute": 0.08}}},
    "agents": [{"agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice", "fallbacks": ["openai-live"]},
                "purposes": ["support"], "contact_groups": ["customers"], "approval": "never",
                "max_call_seconds": 300, "max_concurrent_calls": 20,
                "max_calls_per_day": 100}],
}}


def spec_with(**budget):
    data = copy.deepcopy(SPEC)
    data["communications"]["budget"].update(budget)
    return parse_communications(data)


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    clock = {"now": START}
    rig = {"carrier": MockTelephony(), "primary": MockVoice(), "fallback": MockVoice(),
           "board": Board()}

    def make(spec=None):
        return CallGateway(spec or parse_communications(SPEC), store,
                           AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                           {"mock-telephony": rig["carrier"]},
                           voices={"mock-voice": rig["primary"], "openai-live": rig["fallback"]},
                           call_secret=b"a-bridge-secret-of-enough-length", tasks=rig["board"],
                           clock=lambda: clock["now"])

    yield make, store, clock, rig
    store.close()


def call(gateway, rig, key, *, seconds=None):
    """A call dialled, answered, and (with ``seconds``) finished as the carrier reports it."""
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                  idempotency_key=key).record
    headers, body = rig["carrier"].callback(record.provider_call_ref, CallStatus.ANSWERED,
                                            event_id=f"{key}-a")
    plan = json.loads(gateway.answer("mock-telephony", headers=headers, body=body).body)
    if seconds is not None:
        headers, body = rig["carrier"].callback(record.provider_call_ref, CallStatus.COMPLETED,
                                                event_id=f"{key}-c", duration=seconds)
        gateway.ingest("mock-telephony", headers=headers, body=body)
    return gateway.store.call(record.call_id), plan


# -- usage and the budget ----------------------------------------------------------------------


def test_a_call_costs_its_billed_minutes_at_the_tenants_rates(world):
    make, _, _, rig = world
    gateway = make()
    call(gateway, rig, "k1", seconds=61)  # two billed minutes
    usage = month_to_date(gateway)
    assert (usage["calls"], usage["minutes"]) == (1, 2)
    assert usage["spend"] == pytest.approx(2 * (0.02 + 0.08))
    assert usage["by_agent"]["caller"]["minutes"] == 2


def test_calls_on_the_line_count_against_the_limit_and_the_limit_stops_new_ones(world):
    make, store, _, rig = world
    gateway = make(spec_with(monthly_limit=1.5))
    on_the_line, _ = call(gateway, rig, "live")  # 5 minutes reserved: 0.50
    call(gateway, rig, "done", seconds=600)        # 10 minutes: 1.00
    refused = gateway.request_call(agent="caller", contact_id="c1", purpose="support",
                                   idempotency_key="next")
    assert refused.record.status is CallStatus.POLICY_BLOCKED
    assert refused.decision.rule == "budget"
    # The call already on the line is left alone.
    assert store.call(on_the_line.call_id).status is CallStatus.ANSWERED
    assert rig["carrier"].ended == []


def test_the_carriers_bill_replaces_its_estimate_and_a_gap_is_audited(world, tmp_path):
    make, store, clock, rig = world
    gateway = make()
    record, _ = call(gateway, rig, "k1", seconds=60)
    rig["carrier"].unreachable = True
    clock["now"] = START + timedelta(minutes=15)
    sweep(gateway)
    assert store.call_usage(record.call_id)["billed_price"] is None  # asked again later

    rig["carrier"].unreachable = False
    rig["carrier"].billing[record.provider_call_ref] = Billing(60, 0.25, "USD")
    sweep(gateway)
    usage = month_to_date(gateway)
    assert usage["billed"] == pytest.approx(0.25)
    assert usage["spend"] == pytest.approx(0.25 + 0.08)  # billed carrier + estimated engine
    events = [json.loads(line)["kind"] for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert "communications.billing.mismatch" in events


def test_each_threshold_alerts_once_a_month(world, tmp_path):
    make, _, _, rig = world
    gateway = make()
    call(gateway, rig, "k1", seconds=300)  # 0.50 of 1.00
    sweep(gateway)
    sweep(gateway)
    (alert,) = rig["board"].items.values()
    assert alert.assignee == "ops" and "50%" in alert.title
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert kinds.count("communications.budget.alert") == 1


def test_a_follow_up_email_waits_when_the_budget_is_spent(world):
    from nova.communications.actions import run_due
    from nova.communications.mock import MockEmail

    make, store, _, rig = world
    data = copy.deepcopy(SPEC)
    comms = data["communications"]
    comms.update(public_url="https://calls.example.test",
                 email={"provider": "mock-email", "from": "Acme <a@acme.example>"},
                 extraction={"llm_base_url": "https://llm.example.test/v1", "llm_model": "m"},
                 actions={"rules": {"follow_up_email": {"tier": "auto", "subject": "s",
                                                        "body": "b"}}})
    comms["purposes"][0]["actions"] = ["follow_up_email"]
    comms["budget"]["monthly_limit"] = 0.1
    gateway = make(parse_communications(data))
    gateway.email, gateway.link_secret = MockEmail(), b"k" * 32
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              email="dana@example.com"))
    record, _ = call(gateway, rig, "k1", seconds=60)
    store.save_extraction(record.call_id, model="m", fields=[], summary="", error="", at="t",
                          transcript_until="", actions=[{
                              "contact_id": "c1", "kind": "follow_up_email", "tier": "auto",
                              "status": "pending", "quote": "q",
                              "params": {"to": "dana@example.com", "subject": "s", "body": "b"}}])
    run_due(gateway)
    (held,) = store.actions(call_id=record.call_id)
    assert held["status"] == "awaiting_approval" and "budget" in held["note"]
    assert gateway.email.sent == {}


def test_unpriced_providers_are_named_not_hidden(world):
    make, _, _, rig = world
    gateway = make(spec_with(rates={"mock-telephony": {"per_minute": 0.02}}))
    call(gateway, rig, "k1", seconds=60)
    assert month_to_date(gateway)["unpriced"] == ["mock-voice"]


def test_the_history_shows_what_each_finished_call_cost(world):
    make, store, _, rig = world
    gateway = make()
    call(gateway, rig, "k1", seconds=30)
    (row,) = timeline(store, contact_id="c1", spec=gateway.spec)
    assert row["cost"]["estimated"] == pytest.approx(0.10)


@pytest.mark.parametrize("budget, message", [
    ({"rates": {"telnyx": {"per_minute": 0.01}}}, "not one of this tenant's providers"),
    ({"rates": {"mock-voice": {"per_hour": 1}}}, "may set only"),
    ({"alert_at": [0]}, "percentages"),
    ({"monthly_limit": -5}, "0 or more"),
])
def test_the_budget_refuses_what_it_cannot_measure(budget, message):
    with pytest.raises(SpecError, match=message):
        spec_with(**budget)


def test_alerts_go_only_to_an_agent_the_bundle_defines():
    with pytest.raises(SpecError, match="ops"):
        check_agents_exist(parse_communications(SPEC), ["caller"])


# -- engine failover --------------------------------------------------------------------------


def session(gateway, voice, engine, plan, sid):
    headers, body = voice.incoming(session_id=sid, event_id=f"evt-{sid}", sip_headers=plan["headers"])
    return gateway.voice_event(engine, headers=headers, body=body)


def test_a_refused_session_moves_to_the_next_engine_without_hanging_up(world):
    make, store, _, rig = world
    gateway = make()
    record, plan = call(gateway, rig, "k1")
    rig["primary"].fail_next = True
    assert session(gateway, rig["primary"], "mock-voice", plan, "s1").outcome == "applied"
    ((ref, moved),) = rig["carrier"].redirected
    assert ref == record.provider_call_ref and moved.say == "One moment, please."
    assert store.call(record.call_id).voice_engine == "openai-live"
    assert rig["carrier"].ended == []

    # The fallback proves the same call and takes it.
    new_plan = {"headers": dict(moved.connect.headers)}
    assert session(gateway, rig["fallback"], "openai-live", new_plan, "s2").outcome == "applied"
    assert store.call(record.call_id).status is CallStatus.SESSION_CONNECTED


def test_when_every_engine_has_failed_the_call_ends_rather_than_loops(world):
    make, store, _, rig = world
    gateway = make()
    record, plan = call(gateway, rig, "k1")
    rig["primary"].fail_next = True
    session(gateway, rig["primary"], "mock-voice", plan, "s1")
    (_, moved), = rig["carrier"].redirected
    rig["fallback"].fail_next = True
    session(gateway, rig["fallback"], "openai-live", {"headers": dict(moved.connect.headers)}, "s2")
    assert len(rig["carrier"].redirected) == 1
    assert store.call(record.call_id).status is CallStatus.FAILED
    assert rig["carrier"].ended == [record.provider_call_ref]


def test_a_failing_engine_is_passed_over_for_new_calls_until_it_recovers(world):
    make, store, clock, rig = world
    gateway = make()
    for n in range(3):
        store.record_engine_failure("mock-voice", f"x{n}", "down", at="2026-10-12T14:55:00.000Z")
    _, plan = call(gateway, rig, "k1")
    assert plan["uri"] == rig["fallback"].bridge_target("", "").uri
    assert store.call(store.recent_calls(limit=1)[0].call_id).voice_engine == "openai-live"
    clock["now"] = START + timedelta(minutes=20)
    record, _ = call(gateway, rig, "k2")
    assert store.call(record.call_id).voice_engine == "mock-voice"


# -- Twilio ---------------------------------------------------------------------------------------


def test_twilio_redirects_a_live_call_and_reports_its_price():
    requests = []

    def transport(method, url, headers, body):
        requests.append((method, url, urllib.parse.parse_qs((body or b"").decode())))
        if method == "GET":
            return 200, json.dumps({"status": "completed", "duration": "61",
                                    "price": "-0.02700", "price_unit": "USD"}).encode()
        return 200, b"{}"

    twilio = TwilioTelephony("AC1", "t", answer_url="https://h.test/a",
                             status_url="https://h.test/s", transport=transport)
    twilio.redirect("CA1", AnswerPlan(say="One moment, please.",
                                      connect=SipTarget("sip:proj@sip.example", {})))
    method, url, form = requests[0]
    assert (method, url.rsplit("/", 1)[-1]) == ("POST", "CA1.json")
    assert "<Say>One moment, please.</Say><Dial><Sip>sip:proj@sip.example</Sip></Dial>" in \
        form["Twiml"][0]
    assert twilio.fetch_billing("CA1") == Billing(61, 0.027, "USD")

    signed = urllib.parse.urlencode({"CallSid": "CA1", "CallStatus": "completed",
                                     "CallDuration": "61", "SequenceNumber": "3"}).encode()
    assert twilio.parse({}, signed, "https://h.test/s").detail["duration_seconds"] == 61


def test_usage_is_an_admin_route_and_names_no_contact(tmp_path, runtime):
    import shutil

    import yaml

    from nova.control import ControlAPI
    from nova.control.auth import Principal
    from nova.spec import load_bundle

    from tests.platform.conftest import EXAMPLE_BUNDLE

    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    comms = copy.deepcopy(SPEC)
    comms["communications"]["agents"][0]["agent"] = "customer-support"
    comms["communications"]["budget"]["alert_assignee"] = "operations"
    (root / "communications.yaml").write_text(yaml.safe_dump(comms))
    bundle = load_bundle(root)
    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl",
                                                         tenant_id=bundle.tenant_id))
    assert not Principal(name="v", role="viewer").may("/communications/usage")
    usage = control.handle("/platform/v1/communications/usage").body
    assert (usage["limit"], usage["spend"], usage["remaining"]) == (1.0, 0.0, 1.0)
    assert "c1" not in json.dumps(usage)
