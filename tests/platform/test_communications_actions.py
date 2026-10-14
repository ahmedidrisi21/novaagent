"""Acting on what a call produced: the plan, the tiers, email, and the limits on all of it."""

from __future__ import annotations

import copy
import json
import shutil
import urllib.parse
from datetime import datetime, timedelta, timezone, UTC

import pytest
import yaml

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.actions import decide_plan, plan_view, run_due
from nova.communications.extraction import process_call
from nova.communications.followup_email import unsubscribe_token
from nova.communications.mail import EmailStatus, OutboundEmail
from nova.communications.mock import MockEmail, MockTelephony
from nova.communications.resend import ResendEmail
from nova.communications.spec import check_agents_exist
from nova.communications.sweep import sweep
from nova.communications.webhooks import handle
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE
from tests.platform.test_communications_extraction import Reader

START = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 10:00 in Chicago
PUBLIC = "https://calls.example.test"

SPEC = {"communications": {
    "allow_test_providers": True,
    "disclosure": "This is an AI assistant calling for Acme.",
    "public_url": PUBLIC,
    "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "extraction": {"llm_base_url": "https://llm.example.test/v1", "llm_model": "reader"},
    "email": {"provider": "mock-email", "from": "Acme <support@acme.example>"},
    "actions": {"rules": {
        "escalate": {"tier": "auto", "assignee": "lead"},
        "task": {"tier": "auto", "assignee": "ops"},
        "callback": "auto",
        "follow_up_email": {"tier": "approve", "subject": "Following up: {detail}",
                            "body": "Hi {name},\n\nAs promised: {summary}"},
        "commercial_change": "never",
    }},
    "purposes": [{"id": "sales", "actions": ["escalate", "task", "callback", "follow_up_email",
                                             "commercial_change"]}],
    "agents": [{"agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"}, "purposes": ["sales"],
                "contact_groups": ["customers"], "approval": "never"}],
}}

TURNS = [("agent", "Is now a good time to talk about the upgrade?"),
         ("caller", "Yes. Send me the pricing by email, and call me back tomorrow at 10:30."),
         ("caller", "Also I want a refund on last month.")]


def spec_with(**changes):
    data = copy.deepcopy(SPEC)
    for path, value in changes.items():
        target = data["communications"]
        *parents, leaf = path.split(".")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return parse_communications(data)


class Board:
    """The runtime's board: idempotent on the item key, as the runtime contract requires."""

    def __init__(self):
        self.items = {}

    def __call__(self, item):
        self.items.setdefault(item.key, item)
        return f"task-{list(self.items).index(item.key) + 1}"


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              name="Dana", email="dana@example.com"))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    clock = {"now": START}
    rig = {"email": MockEmail(), "board": Board(), "carrier": MockTelephony()}

    def make(reading, spec=None):
        gateway = CallGateway(spec or parse_communications(SPEC), store,
                              AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                              {"mock-telephony": rig["carrier"]}, extractor=Reader(reading),
                              email=rig["email"], tasks=rig["board"], link_secret=b"k" * 32,
                              clock=lambda: clock["now"])
        record = gateway.request_call(agent="caller", contact_id="c1", purpose="sales",
                                      idempotency_key=f"k{len(store.recent_calls())}").record
        said = gateway.transcriber(record)
        for speaker, text in TURNS:
            said(speaker, text)
        headers, body = rig["carrier"].callback(record.provider_call_ref, CallStatus.COMPLETED,
                                                event_id=f"done-{record.call_id}")
        gateway.ingest("mock-telephony", headers=headers, body=body)
        process_call(gateway, record.call_id, gateway.extractor)
        return gateway, record

    yield make, store, clock, rig
    store.close()


def action(kind, quote, **extra):
    return {"kind": kind, "quote": quote, **extra}


EMAIL = action("follow_up_email", "Send me the pricing by email", detail="pricing for the upgrade")
CALLBACK = action("callback", "call me back tomorrow at 10:30", when="2026-10-13T10:30")
REFUND = action("commercial_change", "I want a refund on last month", detail="refund request")


def by_kind(store, call_id):
    return {a["kind"]: a for a in store.actions(call_id=call_id)}


# -- tiers -----------------------------------------------------------------------------------


def test_each_proposal_lands_at_the_tenants_tier_never_the_models(world):
    make, store, _, _ = world
    _gateway, record = make({"summary": "Wants pricing.", "actions": [
        EMAIL, CALLBACK, REFUND,
        action("task", "Send me the pricing", detail="prepare pricing", tier="auto"),
        action("flag", "Yes", detail="not allowed for this purpose"),
        {"kind": ["task"], "quote": "Yes"}, "not an action",
    ]})
    actions = by_kind(store, record.call_id)
    assert set(actions) == {"follow_up_email", "callback", "commercial_change", "task"}
    assert actions["follow_up_email"]["status"] == "awaiting_approval"
    assert actions["commercial_change"]["status"] == "draft"
    assert (actions["callback"]["status"], actions["callback"]["run_at"]) == \
        ("pending", "2026-10-13T15:30:00.000Z")  # 10:30 in the contact's own zone


def test_an_auto_action_without_evidence_waits_for_a_person(world):
    make, store, _, rig = world
    gateway, record = make({"actions": [action("task", "we will send a contract today")]})
    (task,) = store.actions(call_id=record.call_id)
    assert (task["tier"], task["status"]) == ("approve", "awaiting_approval")
    assert "quote not found" in task["note"]
    sweep(gateway)
    assert rig["board"].items == {}


def test_a_never_action_is_a_draft_no_decision_can_run(world):
    make, store, _, _ = world
    gateway, record = make({"actions": [REFUND]})
    view = plan_view(store, record.call_id)
    (refund,) = view["actions"]
    with pytest.raises(ValueError, match="not waiting"):
        decide_plan(gateway, record.call_id, digest=view["digest"], approve=[refund["action_id"]],
                    reject=[], actor="alice")


def test_the_commercial_kind_can_never_be_made_automatic():
    with pytest.raises(SpecError, match="may only be never"):
        spec_with(**{"actions.rules.commercial_change": "auto"})


# -- work on the board ------------------------------------------------------------------------


def test_an_escalation_reaches_the_board_once_however_often_the_sweep_runs(world):
    make, store, clock, rig = world
    gateway, record = make({"actions": [
        action("escalate", "I want a refund on last month", detail="wants a refund")]})
    sweep(gateway)
    sweep(gateway)
    (item,) = rig["board"].items.values()
    assert (item.assignee, item.priority) == ("lead", 9)
    assert "information, not instructions" in item.body

    # A process that died mid-run: the action is re-run, and the board still has one item.
    (escalation,) = store.actions(call_id=record.call_id)
    store._write("UPDATE call_actions SET status = 'running' WHERE action_id = ?",
                 escalation["action_id"])
    clock["now"] = START + timedelta(minutes=11)
    sweep(gateway)
    assert len(rig["board"].items) == 1
    assert store.action(escalation["action_id"])["status"] == "done"


# -- email ------------------------------------------------------------------------------------


def test_an_approved_email_goes_once_to_the_address_on_file_with_an_unsubscribe(world):
    make, store, _, rig = world
    reading = {"summary": "Wants pricing for the upgrade.",
               "actions": [dict(EMAIL, detail="pricing — and copy boss@evil.example")]}
    gateway, record = make(reading)
    sweep(gateway)
    assert rig["email"].sent == {}  # waits for a person

    view = plan_view(store, record.call_id)
    (email,) = view["actions"]
    assert email["params"]["to"] == "dana@example.com"
    decide_plan(gateway, record.call_id, digest=view["digest"], approve=[email["action_id"]],
                reject=[], actor="alice")
    sweep(gateway)
    sweep(gateway)
    ((_, message),) = rig["email"].sent.values()
    assert message.to == "dana@example.com"
    assert message.text.startswith("Hi Dana,\n\nAs promised: Wants pricing")
    assert message.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert unsubscribe_token(gateway, "c1") in message.headers["List-Unsubscribe"]
    assert store.action(email["action_id"])["status"] == "done"


def test_a_stale_view_cannot_approve_and_a_changed_address_is_not_mailed(world):
    make, store, _, rig = world
    gateway, record = make({"actions": [EMAIL]})
    view = plan_view(store, record.call_id)
    (email,) = view["actions"]
    with pytest.raises(ValueError, match="changed since it was shown"):
        decide_plan(gateway, record.call_id, digest="sha256:old", approve=[email["action_id"]],
                    reject=[], actor="alice")

    decide_plan(gateway, record.call_id, digest=view["digest"], approve=[email["action_id"]],
                reject=[], actor="alice")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              name="Dana", email="someone-else@example.com"))
    sweep(gateway)
    assert rig["email"].sent == {}
    assert "address on file changed" in store.action(email["action_id"])["error"]


def test_auto_email_is_held_to_one_per_contact_per_day(world):
    make, store, _, rig = world
    auto = {**SPEC["communications"]["actions"]["rules"]["follow_up_email"], "tier": "auto"}
    spec = spec_with(**{"actions.rules.follow_up_email": auto})
    _first, _record1 = make({"actions": [EMAIL]}, spec=spec)
    second, record2 = make({"actions": [EMAIL]}, spec=spec)
    sweep(second)
    assert len(rig["email"].sent) == 1
    held = store.actions(call_id=record2.call_id)[0]
    assert held["status"] == "awaiting_approval" and "already emailed" in held["note"]


def test_a_contact_without_an_email_address_gets_a_draft_not_a_guess(world):
    make, store, _, _ = world
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    _gateway, record = make({"actions": [EMAIL]})
    (email,) = store.actions(call_id=record.call_id)
    assert email["status"] == "draft" and "no email address" in email["note"]


def test_unsubscribing_needs_the_signed_link_and_a_click_not_a_fetch(world):
    make, store, _, _ = world
    gateway, record = make({"actions": [EMAIL]})
    path = "/email/mock-email/unsubscribe?" + urllib.parse.urlencode(
        {"c": "c1", "t": unsubscribe_token(gateway, "c1")})
    forged = "/email/mock-email/unsubscribe?c=c1&t=" + "0" * 32

    assert handle(gateway, PUBLIC, forged, {}, b"")[0] == 404
    status, _, page = handle(gateway, PUBLIC, path, {}, b"", method="GET")
    assert status == 200 and b"<form method=post" in page
    assert not store.channel_suppressed("c1", "email")  # a scanner's GET changes nothing

    assert handle(gateway, PUBLIC, path, {}, b"List-Unsubscribe=One-Click")[0] == 200
    assert store.channel_suppressed("c1", "email")
    assert not store.is_suppressed("c1")  # email only: calls are a different consent
    assert store.actions(call_id=record.call_id)[0]["status"] == "cancelled"


def test_a_hard_bounce_stops_email_and_a_redelivered_event_is_applied_once(world):
    make, store, _, rig = world
    gateway, record = make({"actions": [EMAIL]})
    view = plan_view(store, record.call_id)
    decide_plan(gateway, record.call_id, digest=view["digest"],
                approve=[view["actions"][0]["action_id"]], reject=[], actor="alice")
    sweep(gateway)
    ((ref, _),) = rig["email"].sent.values()

    headers, body = rig["email"].event(ref, EmailStatus.BOUNCED, event_id="evt-1")
    assert handle(gateway, PUBLIC, "/email/mock-email/events", headers, body)[0] == 200
    assert handle(gateway, PUBLIC, "/email/mock-email/events", headers, body)[0] == 200
    assert handle(gateway, PUBLIC, "/email/mock-email/events", {}, body)[0] == 403
    (sent,) = store.emails(contact_id="c1")
    assert sent["status"] == "bounced" and store.channel_suppressed("c1", "email")


# -- callbacks ---------------------------------------------------------------------------------


def test_a_callback_waits_for_its_time_and_still_passes_the_gate(world):
    make, store, clock, rig = world
    gateway, record = make({"actions": [CALLBACK]})
    placed = len(rig["carrier"].placed)
    sweep(gateway)
    assert len(rig["carrier"].placed) == placed  # not yet due

    store.revoke_consent("c1", "ai_voice")
    clock["now"] = START + timedelta(days=1, minutes=31)
    sweep(gateway)
    (callback,) = store.actions(call_id=record.call_id)
    assert callback["status"] == "failed" and "consent" in callback["error"]
    assert len(rig["carrier"].placed) == placed


def test_callbacks_cannot_chain_past_the_limit_on_their_own(world):
    make, store, clock, rig = world
    spec = spec_with(**{"actions.max_chain_depth": 1})
    gateway, record = make({"actions": [CALLBACK]}, spec=spec)
    clock["now"] = START + timedelta(days=1, minutes=31)
    sweep(gateway)
    spawned = store.actions(call_id=record.call_id)[0]["result"]
    assert store.call(spawned) is not None

    # The callback call itself proposes another callback: depth 2, over the limit of 1.
    gateway.extractor = Reader({"actions": [dict(CALLBACK, when="2026-10-14T10:30")]})
    said = gateway.transcriber(store.call(spawned))
    for speaker, text in TURNS:
        said(speaker, text)
    headers, body = rig["carrier"].callback(store.call(spawned).provider_call_ref,
                                            CallStatus.COMPLETED, event_id="done-spawned")
    gateway.ingest("mock-telephony", headers=headers, body=body)
    process_call(gateway, spawned, gateway.extractor)
    clock["now"] = START + timedelta(days=2, minutes=31)
    sweep(gateway)
    (second,) = store.actions(call_id=spawned)
    assert second["status"] == "awaiting_approval" and "maximum depth" in second["note"]


def test_a_spoken_opt_out_cancels_what_was_about_to_reach_them(world):
    make, store, _, _rig = world
    gateway, record = make({"actions": [CALLBACK]})
    gateway.opt_out("c1", reason="asked", source="test")
    (callback,) = store.actions(call_id=record.call_id)
    assert callback["status"] == "cancelled"


# -- the spec ----------------------------------------------------------------------------------


@pytest.mark.parametrize("change, message", [
    ({"actions.rules.follow_up_email": {"tier": "approve", "subject": "{phone}", "body": "x"}},
     "may use only"),
    ({"purposes": [{"id": "sales", "actions": ["meeting"]}]}, "not an action kind"),
    ({"actions.rules": {}}, "has no rule"),
    ({"email": None}, "communications.email"),
    ({"public_url": ""}, "unsubscribe link"),
    ({"actions.rules.task": {"tier": "auto"}}, "assignee"),
])
def test_the_spec_refuses_what_cannot_be_governed(change, message):
    data = copy.deepcopy(SPEC)
    for path, value in change.items():
        target = data["communications"]
        *parents, leaf = path.split(".")
        for key in parents:
            target = target[key]
        if value is None:
            del target[leaf]
        else:
            target[leaf] = value
    with pytest.raises(SpecError, match=message):
        parse_communications(data)


def test_work_goes_only_to_agents_the_bundle_defines():
    with pytest.raises(SpecError, match="lead"):
        check_agents_exist(parse_communications(SPEC), ["caller", "ops"])


# -- Resend --------------------------------------------------------------------------------------


class FakeResend:
    def __init__(self):
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, json.loads(body)))
        return 200, b'{"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"}'


def test_resend_sends_idempotently_and_verifies_svix_signatures():
    fake = FakeResend()
    resend = ResendEmail(api_key="re_x", webhook_secret="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
                         transport=fake, clock=lambda: 1614265330)
    ref = resend.send(OutboundEmail(email_id="e1", idempotency_key="nova-email-a-1",
                                    sender="Acme <a@acme.example>", to="d@example.com",
                                    subject="Hi", text="Body", headers={"List-Unsubscribe": "<u>"}))
    ((method, url, headers, payload),) = fake.requests
    assert (method, url, ref) == ("POST", "https://api.resend.com/emails",
                                  "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794")
    assert headers["Idempotency-Key"] == "nova-email-a-1"
    assert payload["to"] == ["d@example.com"] and payload["headers"]["List-Unsubscribe"] == "<u>"
    assert payload["tags"] == [{"name": "nova_email_id", "value": "e1"}]

    # The Standard Webhooks example vector, under Svix's header names.
    signed = {"svix-id": "msg_p5jXN8AQM9LWM0D4loKWxJek", "svix-timestamp": "1614265330",
              "svix-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="}
    assert resend.verify(signed, b'{"test": 2432232314}')
    assert not resend.verify(signed, b'{"test": 2432232315}')
    assert not resend.verify({**signed, "svix-timestamp": "1614264000"}, b'{"test": 2432232314}')


def test_resend_tells_a_hard_bounce_from_a_soft_one():
    resend = ResendEmail(api_key="re_x", webhook_secret="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")

    def bounce(kind):
        return json.dumps({"type": "email.bounced", "data": {
            "email_id": "r1", "tags": {"nova_email_id": "e1"},
            "bounce": {"type": kind, "message": "mailbox full"}}}).encode()

    (hard,) = resend.parse({"svix-id": "m1"}, bounce("Permanent"))
    (soft,) = resend.parse({"svix-id": "m2"}, bounce("Transient"))
    assert (hard.status, hard.email_id_hint) == (EmailStatus.BOUNCED, "e1")
    assert soft.status is EmailStatus.DELAYED


# -- the Control API -----------------------------------------------------------------------------


def seeded(tmp_path, runtime):
    """A bundle, and one finished call whose plan has an email waiting for a person."""
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    comms = copy.deepcopy(SPEC)
    comms["communications"]["agents"][0]["agent"] = "customer-support"
    for rule in comms["communications"]["actions"]["rules"].values():
        if isinstance(rule, dict) and "assignee" in rule:
            rule["assignee"] = "operations"
    (root / "communications.yaml").write_text(yaml.safe_dump(comms))
    bundle = load_bundle(root)
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              email="dana@example.com"))
    store._write("INSERT INTO calls (call_id, tenant_id, idempotency_key, agent, contact_id, "
                 "purpose, telephony, action_digest, status, created_at, updated_at) VALUES "
                 "('call-1', ?, 'k', 'customer-support', 'c1', 'sales', 'mock-telephony', '', "
                 "'completed', 't', 't')", bundle.tenant_id)
    store.save_extraction("call-1", model="m", fields=[], summary="", error="", at="t",
                          transcript_until="", actions=[{
                              "contact_id": "c1", "kind": "follow_up_email", "tier": "approve",
                              "status": "awaiting_approval", "quote": "send me details",
                              "params": {"to": "dana@example.com", "subject": "s", "body": "b"}}])
    store.close()
    return root, bundle


def test_plans_are_listed_and_decided_by_admins_against_the_digest(tmp_path, runtime):
    _, bundle = seeded(tmp_path, runtime)
    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl",
                                                         tenant_id=bundle.tenant_id))

    assert not Principal(name="v", role="viewer").may("/communications/actions")
    (plan,) = control.handle("/platform/v1/communications/actions").body["plans"]
    action_id = plan["actions"][0]["action_id"]
    path = "/platform/v1/communications/calls/call-1/plan"
    viewer, admin = Principal(name="v", role="viewer"), Principal(name="alice", role="admin")
    body = {"digest": plan["digest"], "approve": [action_id]}
    assert control.write(path, viewer, body).status == 403
    assert control.write(path, admin, {**body, "digest": "sha256:stale"}).status == 409
    decided = control.write(path, admin, body).body
    assert decided["actions"][0]["decided_by"] == "alice"
    stats = control.handle("/platform/v1/communications/actions/stats").body["kinds"]
    assert stats["follow_up_email"]["approved_by_person"] == 1
    assert stats["follow_up_email"]["tier"] == "approve"


def test_nova_calls_shows_a_plan_and_decides_it_against_its_digest(tmp_path, runtime, home,
                                                                   capsys):
    from nova.cli import main

    root, _ = seeded(tmp_path, runtime)
    calls = ["--home", str(home), "calls"]
    assert main([*calls, "actions", str(root)]) == 0
    assert "follow_up_email" in capsys.readouterr().out
    assert main([*calls, "actions", str(root), "--call", "call-1", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    action_id = view["actions"][0]["action_id"]
    assert main([*calls, "decide-actions", str(root), "call-1", "--digest", "sha256:stale",
                 "--approve", action_id]) == 1
    assert main([*calls, "decide-actions", str(root), "call-1", "--digest", view["digest"],
                 "--reject", action_id, "--actor", "alice"]) == 0
    assert "rejected" in capsys.readouterr().out
