"""Operating calls: reconciliation, contact import, operator actions, the call monitor
and ``nova calls``."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone, UTC

import pytest
import yaml

from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.contacts import import_contacts
from nova.communications.contracts import FailureReason
from nova.communications.mock import MockTelephony
from nova.communications.sweep import sweep
from nova.communications.twilio import TwilioTelephony
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE

START = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)  # 10:00 in Chicago

COMMS = {
    "communications": {
        "allow_test_providers": True,
        "disclosure": "This is an AI assistant calling for Acme.",
        "public_url": "https://calls.example.test",
        "voice_providers": ["mock-voice"],
        "telephony_providers": ["mock-telephony"],
        "caller_ids": ["+15555550100"],
        "purposes": [{"id": "support"}],
        "agents": [
            {"agent": "customer-support", "telephony": "mock-telephony",
             "caller_id": "+15555550100", "voice": {"primary": "mock-voice"},
             "purposes": ["support"], "contact_groups": ["customers"], "approval": "never",
             "max_call_seconds": 300, "max_concurrent_calls": 1},
            {"agent": "operations", "telephony": "mock-telephony",
             "caller_id": "+15555550100", "voice": {"primary": "mock-voice"},
             "purposes": ["support"], "contact_groups": ["customers"], "approval": "always"},
        ],
    }
}


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    clock, carrier = Clock(), MockTelephony()
    audit = AuditLog(tmp_path / "audit.jsonl", tenant_id="acme")
    gateway = CallGateway(parse_communications(COMMS), store, audit,
                          {"mock-telephony": carrier}, clock=clock)
    yield gateway, carrier, clock
    store.close()


def place(gateway, key="k1", agent="customer-support"):
    return gateway.request_call(agent=agent, contact_id="c1", purpose="support",
                                idempotency_key=key).record


# -- reconciliation -----------------------------------------------------------------------


def test_a_call_whose_callbacks_never_came_is_settled_by_asking_the_carrier(world):
    gateway, carrier, clock = world
    record = place(gateway)
    carrier.carrier_status[record.provider_call_ref] = CallStatus.COMPLETED

    clock.advance(minutes=3)
    assert sweep(gateway) == []  # inside its time limit: nobody asks yet

    clock.advance(minutes=10)
    (change,) = sweep(gateway)
    assert (change.before, change.after) == ("initiated", "completed")
    assert gateway.store.call(record.call_id).status is CallStatus.COMPLETED
    # The slot it held is free again: the agent may call.
    assert place(gateway, key="k2").status is CallStatus.INITIATED


def test_a_live_call_within_its_own_time_limit_costs_no_carrier_request(world):
    gateway, carrier, clock = world
    record = place(gateway)
    headers, body = carrier.callback(record.provider_call_ref, CallStatus.ANSWERED, event_id="e1")
    gateway.ingest("mock-telephony", headers=headers, body=body)
    clock.advance(minutes=7)  # past the grace period, inside 300 s + grace of no news
    assert sweep(gateway) == [] and carrier.fetched == []
    clock.advance(minutes=4)
    sweep(gateway)
    assert carrier.fetched == [record.provider_call_ref]


def test_an_unreachable_carrier_is_never_taken_to_mean_the_call_ended(world):
    gateway, carrier, clock = world
    record = place(gateway)
    carrier.unreachable = True
    clock.advance(hours=1)
    (change,) = sweep(gateway)
    assert change.before == change.after == "initiated"
    assert gateway.store.call(record.call_id).status is CallStatus.INITIATED


def test_a_call_the_carrier_never_confirmed_is_failed_with_that_reason(world):
    gateway, _, clock = world
    record = place(gateway)
    gateway.store._write("UPDATE calls SET provider_call_ref = '' WHERE call_id = ?", record.call_id)
    clock.advance(minutes=11)
    sweep(gateway)
    stored = gateway.store.call(record.call_id)
    assert (stored.status, stored.failure) == (CallStatus.FAILED, FailureReason.PROVIDER_FAILURE.value)
    assert "never confirmed" in stored.reason


def test_an_approval_nobody_gives_expires(world):
    gateway, carrier, clock = world
    record = place(gateway, agent="operations")
    clock.advance(hours=23)
    sweep(gateway)
    assert gateway.store.call(record.call_id).status is CallStatus.APPROVAL_REQUIRED
    clock.advance(hours=2)
    sweep(gateway)
    assert gateway.store.call(record.call_id).status is CallStatus.CANCELLED
    assert gateway.approve(record.call_id, approver="alice").record.status is CallStatus.CANCELLED
    assert not carrier.placed


# -- operator actions ---------------------------------------------------------------------


def test_ending_a_pending_call_cancels_it_and_a_live_one_hangs_up_at_the_carrier(world):
    gateway, carrier, _ = world
    pending = place(gateway, agent="operations")
    assert gateway.end_call(pending.call_id, actor="alice").status is CallStatus.CANCELLED
    live = place(gateway, key="k2")
    gateway.end_call(live.call_id, actor="alice", reason="wrong number")
    assert carrier.ended == [live.provider_call_ref]
    with pytest.raises(ValueError):
        gateway.end_call(live.call_id, actor="")


# -- contact import -----------------------------------------------------------------------


def test_contacts_import_with_evidence_and_bad_rows_are_named_not_guessed(tmp_path, world):
    gateway, _, _ = world
    csv_text = (
        "contact_id,phone,timezone,groups,ai_voice_consent,marketing_consent\n"
        "c2,+13125550102,America/New_York,customers;vip,webform-88,\n"
        "c3,312-555-0103,America/Chicago,customers,,\n"
        "c4,+13125550104,Mars/Base,customers,,\n"
        "c5,+13125550105,America/Chicago,customers,yes,\n"
    )
    report = import_contacts(csv_text, store=gateway.store, audit=gateway.audit, at="2026-10-12T00:00:00Z")
    assert (report.imported, report.consents) == (1, 1)
    assert [e.split(":")[0] for e in report.errors] == ["line 3", "line 4", "line 5"]
    assert "evidence" in report.errors[2]
    assert gateway.store.consents("c2") == frozenset({"ai_voice"})
    assert gateway.store.contact("c5") is None
    logged = (tmp_path / "audit.jsonl").read_text()
    assert "+13125550102" not in logged  # counts, never numbers


# -- the call monitor and the operator's buttons ------------------------------------------


@pytest.fixture
def api(tmp_path, runtime, monkeypatch):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(COMMS))
    bundle = load_bundle(root)
    audit = AuditLog(tmp_path / "control-audit.jsonl", tenant_id=bundle.tenant_id)
    carrier = MockTelephony()

    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    store.close()

    def gateway(control, actor):
        return CallGateway(bundle.communications,
                           CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id),
                           control.audit.with_actor(actor), {"mock-telephony": carrier},
                           clock=lambda: START)

    monkeypatch.setattr("nova.control.api_calls._gateway", gateway)
    control = ControlAPI(bundle, runtime, audit=audit)
    pending = gateway(control, "agent").request_call(
        agent="operations", contact_id="c1", purpose="support", idempotency_key="k1").record
    return control, carrier, pending, audit


ADMIN = Principal(name="alice", role="admin")
VIEWER = Principal(name="reader", role="viewer")


def test_the_call_monitor_and_its_buttons_are_for_admins_only(api):
    control, carrier, pending, _ = api
    assert not VIEWER.may("/communications/calls")
    assert ADMIN.may("/communications/calls")
    path = f"/platform/v1/communications/calls/{pending.call_id}/decide"
    assert control.write(path, VIEWER, {"action": "approve"}).status == 403
    assert not carrier.placed


def test_an_admin_approval_dials_once_and_is_recorded_under_their_name(api):
    control, carrier, pending, audit = api
    listed = control.handle("/platform/v1/communications/calls").body
    assert [c["status"] for c in listed["calls"]] == ["approval_required"]

    path = f"/platform/v1/communications/calls/{pending.call_id}/decide"
    first = control.write(path, ADMIN, {"action": "approve"})
    again = control.write(path, ADMIN, {"action": "approve"})
    assert (first.status, first.body["status"]) == (200, "initiated")
    assert again.status == 409 and len(carrier.placed) == 1
    approved = [e for e in audit.read() if e.kind == "communications.call.approved"]
    assert approved[0].actor == "alice"

    assert control.write("/platform/v1/communications/calls/nope/decide", ADMIN,
                         {"action": "end"}).status == 404
    assert control.write(path, ADMIN, {"action": "dial-someone-else"}).status == 400


# -- the CLI ------------------------------------------------------------------------------


def test_nova_calls_imports_contacts_and_lists_calls(tmp_path, capsys):
    from nova.cli import main

    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(COMMS))
    home = tmp_path / "home"
    home.mkdir()
    contacts = tmp_path / "contacts.csv"
    contacts.write_text("contact_id,phone,timezone,groups,ai_voice_consent\n"
                        "c9,+13125550109,America/Chicago,customers,form-9\n")

    assert main(["--home", str(home), "calls", "import-contacts", str(root), str(contacts)]) == 0
    assert "imported 1 contact(s), 1 consent record(s)" in capsys.readouterr().out
    assert main(["--home", str(home), "calls", "consent", str(root), "grant", "c9",
                 "marketing_written"]) == 2  # no evidence, no consent
    assert "--evidence is required" in capsys.readouterr().out
    assert main(["--home", str(home), "calls", "list", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_twilio_reports_a_calls_real_status_for_reconciliation():
    requests = []

    def transport(method, url, headers, body):
        requests.append((method, url))
        if url.endswith("/Calls/CAgone.json"):
            return 404, b"{}"
        return 200, json.dumps({"status": "no-answer"}).encode()

    twilio = TwilioTelephony("AC1", "t", answer_url="https://h.test/a",
                             status_url="https://h.test/s", transport=transport)
    event = twilio.fetch_status("CA1")
    assert (event.status, event.failure) == (CallStatus.FAILED, FailureReason.NO_ANSWER)
    assert requests[0] == ("GET", "https://api.twilio.com/2010-04-01/Accounts/AC1/Calls/CA1.json")
    assert twilio.fetch_status("CAgone") is None
