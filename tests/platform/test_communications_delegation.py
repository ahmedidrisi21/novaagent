"""Agents handing work to each other, and the one timeline of everything that happened."""

from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, timezone, UTC

import pytest
import yaml

from nova.apply import apply_bundle
from nova.audit import AuditLog
from nova.communications import CallGateway, CallStatus, CommsStore, Contact, parse_communications
from nova.communications.delegation import delegate, report
from nova.communications.extraction import process_call
from nova.communications.history import timeline
from nova.communications.inbox import master_key, process
from nova.communications.mock import MockTelephony
from nova.communications.spec import check_agents_exist
from nova.communications.sweep import sweep
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError
from nova.runtime.hermes.paths import HermesPaths
from nova.spec import load_bundle

from tests.platform.conftest import EXAMPLE_BUNDLE
from tests.platform.test_communications_actions import Board
from tests.platform.test_communications_agent_tool import load_tool
from tests.platform.test_communications_extraction import Reader

START = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def spec(routes, **limits):
    return parse_communications({"communications": {
        "delegation": {"routes": [{"from": k, "to": v} for k, v in routes.items()], **limits}}})


@pytest.fixture
def rig(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    clock = {"now": START}
    board = Board()

    def make(communications):
        return CallGateway(communications, store, AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                           {}, tasks=board, clock=lambda: clock["now"])

    yield make, store, clock, board
    store.close()


def hand(gateway, sender, receiver, *, task="", ident=None, **extra):
    ident = ident or f"d-{sender}-{receiver}-{task or 'root'}"
    return delegate(gateway, delegation_id=ident, sender=sender, receiver=receiver,
                    objective=extra.pop("objective", "qualify this lead"),
                    expected=extra.pop("expected", []), parent_task_id=task, **extra)


# -- the checks ------------------------------------------------------------------------------


def test_only_a_declared_route_reaches_the_board(rig):
    make, _, _, board = rig
    gateway = make(spec({"research": ["qualify"]}))
    assert hand(gateway, "research", "qualify")["status"] == "assigned"
    refused = hand(gateway, "qualify", "research")
    assert refused["status"] == "refused" and "no such route" in refused["reason"]
    assert [item.assignee for item in board.items.values()] == ["qualify"]


def test_a_chain_stops_at_its_depth_and_never_loops_back(rig):
    make, _, _, _ = rig
    gateway = make(spec({"a": ["b"], "b": ["c", "a"], "c": ["d"]}, max_depth=2))
    first = hand(gateway, "a", "b")
    # b, working on the task a gave it, hands on to c: depth 2.
    second = hand(gateway, "b", "c", task=first["task_id"])
    assert second["status"] == "assigned"
    # c, working on that, would make it 3 deep.
    third = hand(gateway, "c", "d", task=second["task_id"])
    assert third["status"] == "refused" and "limit is 2" in third["reason"]
    # b handing back to a, who started the chain, is a loop — even though the route exists.
    back = hand(gateway, "b", "a", task=first["task_id"], ident="loop")
    assert back["status"] == "refused" and "loop" in back["reason"]


def test_a_receiver_with_too_much_open_work_takes_no_more(rig):
    make, _, _, board = rig
    gateway = make(spec({"a": ["b"], "c": ["b"]}, max_open_per_agent=1))
    assert hand(gateway, "a", "b")["status"] == "assigned"
    busy = hand(gateway, "c", "b")
    assert busy["status"] == "refused" and "open delegations" in busy["reason"]
    assert len(board.items) == 1


def test_a_delegation_is_one_task_however_often_it_is_filed(rig):
    make, _, _, board = rig
    gateway = make(spec({"a": ["b"]}))
    first = hand(gateway, "a", "b", ident="same")
    again = hand(gateway, "a", "b", ident="same")
    assert first == again and len(board.items) == 1
    (item,) = board.items.values()
    assert item.key == "delegation:same" and "information, not instructions" in item.body


def test_only_the_receiver_may_report_and_a_result_must_have_what_was_asked(rig):
    make, _store, _, _ = rig
    gateway = make(spec({"a": ["b"]}))
    hand(gateway, "a", "b", ident="d1", expected=["budget", "decision_maker"])
    with pytest.raises(ValueError, match="only the agent"):
        report(gateway, delegation_id="d1", reporter="a", status="completed", result={})
    with pytest.raises(ValueError, match="decision_maker"):
        report(gateway, delegation_id="d1", reporter="b", status="completed",
               result={"budget": 12000})
    done = report(gateway, delegation_id="d1", reporter="b", status="completed",
                  result={"budget": 12000, "decision_maker": "Dana"})
    assert done["status"] == "completed" and done["result"]["decision_maker"] == "Dana"
    with pytest.raises(ValueError, match="takes no more"):
        report(gateway, delegation_id="d1", reporter="b", status="failed", result=None)


def test_unanswered_work_expires_and_waiting_work_is_assigned_once_a_board_exists(rig):
    make, store, clock, board = rig
    gateway = make(spec({"a": ["b"]}, default_deadline_hours=2))
    gateway.tasks = None
    assert hand(gateway, "a", "b", ident="late")["status"] == "requested"
    gateway.tasks = board
    sweep(gateway)
    assert store.delegation("late")["status"] == "assigned"
    clock["now"] = START + timedelta(hours=3)
    sweep(gateway)
    assert store.delegation("late")["status"] == "expired"


# -- through the agents' own tools ---------------------------------------------------------


ROUTED = {"communications": {"delegation": {"routes": [
    {"from": "customer-support", "to": ["operations"]}]}}}


def test_agents_delegate_and_report_through_their_signed_tools(tmp_path, runtime, home):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(ROUTED))
    bundle = load_bundle(root)
    apply_bundle(bundle, runtime, audit=AuditLog(tmp_path / "a.jsonl", tenant_id=bundle.tenant_id))
    paths = HermesPaths(home)
    sender = load_tool(paths.calling_plugin_dir("customer-support") / "__init__.py")
    receiver = load_tool(paths.calling_plugin_dir("operations") / "__init__.py")
    grant = json.loads(paths.calling_config_path("operations").read_text())
    assert grant["delegation"] == {"may_delegate_to": [], "receives": True,
                                   "default_deadline_hours": 24, "max_deadline_hours": 168}
    assert json.loads(receiver.handle_delegate_work({"to_agent": "customer-support",
                                                     "objective": "x"}))["status"] == "unavailable"

    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    board = Board()
    gateway = CallGateway(bundle.communications, store,
                          AuditLog(tmp_path / "g.jsonl", tenant_id=bundle.tenant_id), {},
                          tasks=board, clock=lambda: START)
    master, inbox = master_key(runtime.state_location), runtime.calls_inbox_path
    queued = json.loads(sender.handle_delegate_work({
        "to_agent": "operations", "objective": "Check stock for order 42",
        "expected_fields": ["in_stock"]}))
    process(gateway, inbox, master)
    (item,) = board.items.values()
    assert item.assignee == "operations" and queued["delegation_id"] in item.body

    filed = json.loads(receiver.handle_report_delegation({
        "delegation_id": queued["delegation_id"], "status": "completed",
        "result": {"in_stock": True}}))
    assert filed["status"] == "queued"
    process(gateway, inbox, master)
    status = json.loads(sender.handle_delegation_status({"delegation_id": queued["delegation_id"]}))
    assert (status["status"], status["result"]) == ("completed", {"in_stock": True})
    store.close()


# -- handoff after a call, and the history ---------------------------------------------------


CALLING = {"communications": {
    "allow_test_providers": True, "disclosure": "This is an AI assistant calling for Acme.",
    "voice_providers": ["mock-voice"], "telephony_providers": ["mock-telephony"],
    "caller_ids": ["+15555550100"],
    "extraction": {"llm_base_url": "https://llm.example.test/v1", "llm_model": "reader"},
    "actions": {"rules": {"handoff": {"tier": "auto", "assignee": "sales"}}},
    "delegation": {"routes": [{"from": "caller", "to": ["sales"]}]},
    "purposes": [{"id": "qualify", "actions": ["handoff"]}],
    "agents": [{"agent": "caller", "telephony": "mock-telephony", "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"}, "purposes": ["qualify"],
                "contact_groups": ["customers"], "approval": "never"}],
}}


def test_a_handoff_needs_a_route_and_agents_the_bundle_defines():
    without = copy.deepcopy(CALLING)
    del without["communications"]["delegation"]
    with pytest.raises(SpecError, match="needs a route"):
        parse_communications(without)
    with pytest.raises(SpecError, match="sales"):
        check_agents_exist(parse_communications(CALLING), ["caller"])


def test_a_qualified_lead_is_handed_over_and_the_history_shows_the_whole_story(rig):
    make, store, _, board = rig
    store.grant_consent("c1", "ai_voice", evidence="form-1", at="2026-10-01T00:00:00Z")
    carrier = MockTelephony()
    gateway = make(parse_communications(CALLING))
    gateway.carriers = {"mock-telephony": carrier}
    gateway.extractor = Reader({"summary": "Ready to buy.", "actions": [
        {"kind": "handoff", "quote": "ready to sign this week", "detail": "qualified lead"}]})
    record = gateway.request_call(agent="caller", contact_id="c1", purpose="qualify",
                                  idempotency_key="k1", task_id="task-7").record
    gateway.transcriber(record)("caller", "We are ready to sign this week.")
    headers, body = carrier.callback(record.provider_call_ref, CallStatus.COMPLETED, event_id="e")
    gateway.ingest("mock-telephony", headers=headers, body=body)
    process_call(gateway, record.call_id, gateway.extractor)
    sweep(gateway)

    (delegation,) = store.delegations()
    assert (delegation["from_agent"], delegation["to_agent"], delegation["call_id"]) == \
        ("caller", "sales", record.call_id)
    assert board.items[f"delegation:{delegation['delegation_id']}"].assignee == "sales"

    story = timeline(store, contact_id="c1")
    assert {row["channel"] for row in story} == {"call", "action", "delegation"}
    assert [r["channel"] for r in timeline(store, task_id="task-7")] == \
        [r["channel"] for r in story]
    assert timeline(store, contact_id="c1", channel="delegation")[0]["status"] == "assigned"
    assert not any({"body", "text", "transcript", "result"} & set(row) for row in story)


def test_history_and_delegations_are_admin_routes(tmp_path, runtime):
    root = tmp_path / "bundle"
    shutil.copytree(EXAMPLE_BUNDLE, root)
    (root / "communications.yaml").write_text(yaml.safe_dump(ROUTED))
    bundle = load_bundle(root)
    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl",
                                                         tenant_id=bundle.tenant_id))
    for route in ("/communications/history", "/communications/delegations"):
        assert not Principal(name="v", role="viewer").may(route)
    assert control.handle("/platform/v1/communications/history").body == {"events": []}
    assert control.handle("/platform/v1/communications/history",
                          {"channel": "fax"}).status == 400
    assert control.handle("/platform/v1/communications/delegations").body == {"delegations": []}
