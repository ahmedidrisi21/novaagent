"""The call gateway: one dial per request, approval bound to the action, callbacks applied once.

Every test drives the real store, gate and audit log through the test carrier. Nothing is
mocked below the carrier seam, because the bugs that matter here — a retry that dials twice,
a webhook that regresses a finished call, an approval that outlives the action it approved —
live in the interaction between those pieces.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from nova.audit import AuditLog
from nova.communications import (
    CallGateway,
    CallStatus,
    CommsStore,
    Contact,
    FailureReason,
    ProviderError,
    parse_communications,
)
from nova.communications.contracts import PlaceCall
from nova.communications.mock import MockTelephony

#: 10:00 in Chicago, inside default calling hours.
NOON_UTC = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

SPEC = {
    "communications": {
        "allow_test_providers": True,
        "voice_providers": ["mock-voice"],
        "telephony_providers": ["mock-telephony"],
        "caller_ids": ["+15555550100"],
        "purposes": [
            {"id": "support-follow-up"},
            {"id": "renewal-offer", "marketing": True},
        ],
        "agents": [
            {
                "agent": "caller",
                "telephony": "mock-telephony",
                "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"},
                "purposes": ["support-follow-up", "renewal-offer"],
                "contact_groups": ["customers"],
                "approval": "never",
                "max_concurrent_calls": 2,
                "max_calls_per_day": 3,
            },
            {
                "agent": "supervised",
                "telephony": "mock-telephony",
                "caller_id": "+15555550100",
                "voice": {"primary": "mock-voice"},
                "purposes": ["support-follow-up"],
                "contact_groups": ["customers"],
                "approval": "always",
                "max_calls_per_day": 1,
            },
        ],
    }
}


@pytest.fixture
def carrier():
    return MockTelephony()


@pytest.fixture
def store(tmp_path):
    store = CommsStore(tmp_path / "comms.sqlite", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",)))
    store.grant_consent("c1", "ai_voice", evidence="form-17", at="2026-10-01T00:00:00Z")
    yield store
    store.close()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl", tenant_id="acme", actor="test")


@pytest.fixture
def gateway(store, audit, carrier):
    spec = parse_communications(SPEC)
    return CallGateway(spec, store, audit, {"mock-telephony": carrier}, clock=lambda: NOON_UTC)


def call(gateway, key="k1", agent="caller", contact="c1", purpose="support-follow-up"):
    return gateway.request_call(agent=agent, contact_id=contact, purpose=purpose, idempotency_key=key)


# -- one request, one dial ---------------------------------------------------------------


def test_a_retried_request_dials_once(gateway, carrier):
    first = call(gateway)
    again = call(gateway)
    assert first.record.call_id == again.record.call_id
    assert again.created is False
    assert len(carrier.placed) == 1
    assert carrier.placed[0].to_number == "+13125550101"
    assert carrier.placed[0].from_number == "+15555550100"


def test_concurrent_approvals_dial_once(gateway, carrier):
    pending = call(gateway, agent="supervised").record
    assert pending.status is CallStatus.APPROVAL_REQUIRED and not carrier.placed

    barrier = threading.Barrier(8)

    def approve():
        barrier.wait()
        gateway.approve(pending.call_id, approver="alice")

    threads = [threading.Thread(target=approve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(carrier.placed) == 1
    assert gateway.store.call(pending.call_id).approved_by == "alice"


def test_a_carrier_failure_is_recorded_with_its_reason_and_not_retried(gateway, carrier):
    carrier.fail_next = FailureReason.INVALID_NUMBER
    record = call(gateway).record
    assert record.status is CallStatus.FAILED
    assert record.failure == FailureReason.INVALID_NUMBER.value
    assert call(gateway).created is False and not carrier.placed


# -- approval is bound to the action ------------------------------------------------------


def test_an_approval_does_not_survive_a_changed_destination(gateway, store, carrier):
    pending = call(gateway, agent="supervised").record
    store.put_contact(Contact("c1", "+13125559999", "America/Chicago", ("customers",)))
    result = gateway.approve(pending.call_id, approver="alice")
    assert result.record.status is CallStatus.CANCELLED
    assert not carrier.placed


def test_withdrawn_consent_blocks_a_call_already_awaiting_approval(gateway, store, carrier):
    pending = call(gateway, agent="supervised").record
    store.revoke_consent("c1", "ai_voice")
    result = gateway.approve(pending.call_id, approver="alice")
    assert result.record.status is CallStatus.POLICY_BLOCKED
    assert not carrier.placed


def test_an_opt_out_blocks_every_later_request(gateway, carrier):
    gateway.opt_out("c1", reason="asked not to be called", source="call:abc")
    record = call(gateway).record
    assert record.status is CallStatus.POLICY_BLOCKED
    assert "opted out" in record.reason
    assert not carrier.placed


def test_refusals_and_dials_are_both_audited(gateway, audit):
    call(gateway, key="blocked", purpose="renewal-offer")  # no marketing consent
    call(gateway, key="ok")
    kinds = [(e.kind, e.detail.get("decision", {}).get("effect")) for e in audit.read()]
    assert ("communications.call.requested", "deny") in kinds
    assert ("communications.call.requested", "allow") in kinds
    assert any(kind == "communications.call.placed" for kind, _ in kinds)


# -- caps hold for calls through the gateway ----------------------------------------------


def test_daily_calls_stop_at_the_profile_cap(gateway, store, carrier):
    for index in range(3):
        record = call(gateway, key=f"d{index}").record
        gateway.ingest("mock-telephony", **_cb(carrier, record, CallStatus.COMPLETED, f"e{index}"))
    blocked = call(gateway, key="d3").record
    assert blocked.status is CallStatus.POLICY_BLOCKED
    assert len(carrier.placed) == 3


def test_a_rejected_call_does_not_use_up_the_daily_cap(gateway, carrier):
    rejected = call(gateway, key="r1", agent="supervised").record
    gateway.reject(rejected.call_id, approver="alice", reason="not today")
    nxt = call(gateway, key="r2", agent="supervised").record
    assert nxt.status is CallStatus.APPROVAL_REQUIRED
    gateway.approve(nxt.call_id, approver="alice")
    assert len(carrier.placed) == 1


def test_concurrency_counts_calls_still_holding_a_line(gateway, carrier):
    call(gateway, key="a")
    call(gateway, key="b")
    third = call(gateway, key="c").record
    assert third.status is CallStatus.POLICY_BLOCKED
    assert "concurrent" in third.reason


# -- carrier callbacks --------------------------------------------------------------------


def _cb(carrier, record, status, event_id, failure=None):
    headers, body = carrier.callback(record.provider_call_ref, status, event_id=event_id, failure=failure)
    return {"headers": headers, "body": body}


def test_a_callback_delivered_twice_is_applied_once(gateway, carrier):
    record = call(gateway).record
    cb = _cb(carrier, record, CallStatus.ANSWERED, "e1")
    assert gateway.ingest("mock-telephony", **cb).outcome == "applied"
    assert gateway.ingest("mock-telephony", **cb).outcome == "ignored"


def test_a_late_callback_never_moves_a_call_backwards(gateway, carrier):
    record = call(gateway).record
    gateway.ingest("mock-telephony", **_cb(carrier, record, CallStatus.COMPLETED, "e2"))
    late = gateway.ingest("mock-telephony", **_cb(carrier, record, CallStatus.RINGING, "e1"))
    assert late.outcome == "ignored"
    assert gateway.store.call(record.call_id).status is CallStatus.COMPLETED


def test_an_unsigned_callback_is_rejected(gateway, carrier):
    record = call(gateway).record
    cb = _cb(carrier, record, CallStatus.COMPLETED, "e1")
    cb["body"] = cb["body"].replace(b"completed", b"cancelled")
    assert gateway.ingest("mock-telephony", **cb).outcome == "rejected"
    assert gateway.store.call(record.call_id).status is CallStatus.INITIATED


def test_one_tenant_cannot_move_another_tenants_call(tmp_path, gateway, carrier):
    record = call(gateway).record
    other_store = CommsStore(tmp_path / "comms.sqlite", tenant_id="globex")
    other = CallGateway(
        gateway.spec, other_store, AuditLog(tmp_path / "g.jsonl", tenant_id="globex"),
        {"mock-telephony": carrier}, clock=lambda: NOON_UTC,
    )
    result = other.ingest("mock-telephony", **_cb(carrier, record, CallStatus.COMPLETED, "e1"))
    assert result.outcome == "rejected"
    assert other_store.call(record.call_id) is None
    assert gateway.store.call(record.call_id).status is CallStatus.INITIATED
    other_store.close()


# -- every carrier adapter honours the contract -------------------------------------------

CARRIERS = [MockTelephony]


@pytest.mark.parametrize("make", CARRIERS)
def test_carrier_contract(make):
    carrier = make()
    request = PlaceCall("call-1", "acme", "+13125550101", "+15555550100", 60)
    ref = carrier.place_call(request)
    assert ref

    headers, body = carrier.callback(ref, CallStatus.ANSWERED, event_id="e1")
    assert carrier.verify(headers, body, "")
    assert not carrier.verify(headers, body + b" ", "")
    event = carrier.normalize(json.loads(body))
    assert (event.provider, event.provider_call_ref, event.status) == (
        carrier.id, ref, CallStatus.ANSWERED,
    )
    with pytest.raises(ValueError):
        carrier.normalize({"nonsense": True})

    carrier.end_call(ref)
    carrier.end_call(ref)  # hanging up twice is not an error

    carrier.fail_next = FailureReason.BUSY
    with pytest.raises(ProviderError) as refused:
        carrier.place_call(replace(request, call_id="call-2"))
    assert refused.value.reason is FailureReason.BUSY
