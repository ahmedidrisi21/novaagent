"""Meetings agents book themselves: the gate, approval, booking once, and what the calendar says."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from nova.apply import apply_bundle
from nova.audit import AuditLog
from nova.communications import CallGateway, CommsStore, Contact, parse_communications
from nova.communications.booking import (
    BookingChange,
    BookingRequest,
    BookingUnconfirmed,
    SlotUnavailable,
)
from nova.communications.calcom import CalcomCalendar
from nova.communications.contracts import ProviderError
from nova.communications.history import timeline
from nova.communications.inbox import master_key, process, refresh
from nova.communications.meetings import (
    approve,
    cancel,
    normalize_start,
    reject,
    request_meeting,
)
from nova.communications.mock import MockCalendar, MockTelephony
from nova.communications.sweep import sweep
from nova.communications.webhooks import handle
from nova.control import ControlAPI
from nova.control.auth import Principal
from nova.errors import SpecError
from nova.runtime.hermes.paths import HermesPaths

from tests.platform.test_communications_actions import Board
from tests.platform.test_communications_agent_email import SPEC as BASE_SPEC
from tests.platform.test_communications_agent_tool import COMMS, bundle_with, load_tool

START = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
PUBLIC = "https://calls.example.test"
SLOTS = ["2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z", "2026-10-15T15:00:00Z",
         "2026-10-16T15:00:00Z"]
SLOT = SLOTS[0]

MEETINGS = {
    "provider": "mock-calendar",
    "event_types": [
        {"id": "intro", "title": "Intro call", "provider_ref": "123", "duration_minutes": 30,
         "purposes": ["support", "promo"]},
        {"id": "sales", "title": "Sales call", "provider_ref": "456", "duration_minutes": 45,
         "purposes": ["promo"]},
        {"id": "private", "title": "Private call", "provider_ref": "789", "duration_minutes": 30,
         "purposes": ["support"]}],
    "agents": [{"agent": "writer", "purposes": ["support", "promo"],
                "contact_groups": ["customers"], "event_types": ["intro", "sales"],
                "approval": "never", "max_per_day": 2}],
}


def spec_with(approval="never", **changes):
    data = copy.deepcopy(BASE_SPEC)
    data["communications"]["email"] = {"provider": "mock-email",
                                       "from": "Acme <support@acme.example>"}
    meetings = copy.deepcopy(MEETINGS)
    meetings["agents"][0]["approval"] = approval
    meetings.update(changes)
    data["communications"]["meetings"] = meetings
    return parse_communications(data)


@pytest.fixture
def world(tmp_path):
    store = CommsStore(tmp_path / "calls.db", tenant_id="acme")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              name="Dana", email="dana@example.com"))
    store.put_contact(Contact("c2", "+13125550102", "America/Chicago", ("staff",),
                              email="sam@example.com"))
    store.put_contact(Contact("c3", "+13125550103", "America/Chicago", ("customers",)))
    store.put_contact(Contact("c4", "+13125550104", "America/Chicago", ("customers",),
                              email="lee@example.com"))
    clock = {"now": START}
    rig = {"cal": MockCalendar(open_times=SLOTS), "board": Board()}

    def make(spec=None):
        return CallGateway(spec or spec_with(), store,
                           AuditLog(tmp_path / "a.jsonl", tenant_id="acme"),
                           {"mock-telephony": MockTelephony()}, calendar=rig["cal"],
                           tasks=rig["board"], link_secret=b"k" * 32,
                           clock=lambda: clock["now"])

    yield make, store, clock, rig
    store.close()


def meet(gateway, request_id="mtg00001", contact_id="c1", purpose="support",
         event_type="intro", start=SLOT, agent="writer"):
    return request_meeting(gateway, agent=agent, request_id=request_id, contact_id=contact_id,
                           purpose=purpose, event_type=event_type, start=start)


# -- the gate ----------------------------------------------------------------------------------


def test_the_gate_refuses_each_meeting_the_tenant_did_not_allow(world):
    make, store, _, rig = world
    gateway = make()
    refusals = [
        (dict(agent="caller"), "no meeting grant"),
        (dict(purpose="billing"), "may not book for purpose"),
        (dict(event_type="deep"), "may not book event type"),
        (dict(event_type="private"), "may not book event type"),  # declared, but not this agent's
        (dict(event_type="sales"), "is not for purpose"),
        (dict(contact_id="nobody"), "does not exist"),
        (dict(contact_id="c2"), "none of the groups"),
        (dict(contact_id="c3"), "no email address"),
        (dict(purpose="promo"), "email_marketing consent"),
        (dict(start="2026-10-12T16:00:00Z"), "hours away"),
        (dict(start="2026-12-01T15:00:00Z"), "days away"),
    ]
    for index, (change, reason) in enumerate(refusals):
        result = meet(gateway, request_id=f"refused{index}", **change)
        assert (result["status"], reason in result["reason"]) == ("blocked", True), change
    assert rig["cal"].bookings == {}

    store.grant_consent("c1", "email_marketing", evidence="form-2", at="2026-10-01T00:00:00Z")
    assert meet(gateway, request_id="promo001", purpose="promo")["status"] == "booked"

    store.suppress_channel("c4", "email", reason="asked", at="2026-10-12T15:00:00Z")
    assert "opted out" in meet(gateway, request_id="after001", contact_id="c4")["reason"]
    gateway.opt_out("c1", reason="asked", source="test")
    assert "opted out" in meet(gateway, request_id="after002")["reason"]
    assert len(rig["cal"].bookings) == 1


def test_a_time_with_no_offset_is_read_in_the_contacts_zone_and_garbage_is_refused():
    assert normalize_start("2026-10-14T10:00:00", "America/Chicago") == "2026-10-14T15:00:00Z"
    assert normalize_start("2026-10-14T10:00:00-05:00", "") == "2026-10-14T15:00:00Z"
    for bad in ("tomorrow at 3", "", "2026-10-14T10:00:00"):
        with pytest.raises(ValueError):
            normalize_start(bad, "")


def test_an_agent_is_held_to_its_daily_cap(world):
    make, _, clock, rig = world
    gateway = make()
    assert [meet(gateway, request_id=f"cap0000{i}", start=SLOTS[i])["status"]
            for i in range(3)] == ["booked", "booked", "blocked"]
    clock["now"] += timedelta(days=1, minutes=1)
    assert meet(gateway, request_id="cap00004", start=SLOTS[3])["status"] == "booked"
    assert len(rig["cal"].bookings) == 3


# -- approval ------------------------------------------------------------------------------------


def test_approval_modes_decide_who_must_look_first(world):
    make, store, clock, rig = world
    always = make(spec_with("always"))
    assert meet(always, request_id="always01")["status"] == "awaiting_approval"
    assert rig["cal"].bookings == {}
    assert approve(always, "always01", actor="alice")["status"] == "booked"
    assert store.meeting_request("always01")["decided_by"] == "alice"

    clock["now"] += timedelta(days=1, minutes=1)  # a fresh daily allowance
    first = make(spec_with("first_contact"))
    assert meet(first, request_id="first001", contact_id="c4",
                start=SLOTS[1])["status"] == "awaiting_approval"
    approve(first, "first001", actor="alice")
    assert meet(first, request_id="first002", contact_id="c4",
                start=SLOTS[2])["status"] == "booked"


def test_an_approval_holds_only_for_the_address_and_consent_it_was_given_for(world):
    make, store, _, rig = world
    gateway = make(spec_with("always"))
    meet(gateway, request_id="moved001")
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              email="someone-else@example.com"))
    with pytest.raises(ValueError, match="address changed"):
        approve(gateway, "moved001", actor="alice")
    assert store.meeting_request("moved001")["status"] == "blocked"

    meet(gateway, request_id="optout01", contact_id="c4", start=SLOTS[1])
    gateway.opt_out("c4", reason="asked", source="test")
    with pytest.raises(ValueError, match="opted out"):
        approve(gateway, "optout01", actor="alice")
    with pytest.raises(ValueError):
        approve(gateway, "optout01", actor="alice")  # decided once
    assert rig["cal"].bookings == {}


def test_a_meeting_waiting_for_approval_is_refused_once_its_time_is_too_close(world):
    make, store, clock, rig = world
    gateway = make(spec_with("always"))
    meet(gateway, request_id="late0001")
    clock["now"] = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)  # an hour before it
    with pytest.raises(ValueError, match="hours away"):
        approve(gateway, "late0001", actor="alice")
    assert store.meeting_request("late0001")["status"] == "blocked" and rig["cal"].bookings == {}


# -- booking once --------------------------------------------------------------------------------


def test_a_meeting_is_booked_once_however_often_it_is_asked_for_or_approved(world):
    make, store, _, rig = world
    gateway = make(spec_with("always"))
    meet(gateway, request_id="once0001")
    meet(gateway, request_id="once0001")
    approve(gateway, "once0001", actor="alice")
    with pytest.raises(ValueError):
        approve(gateway, "once0001", actor="bob")
    with pytest.raises(ValueError):
        reject(gateway, "once0001", actor="bob")
    # Two approvers who both saw it waiting: only one claim to book it can succeed.
    assert not store.claim_meeting_request("once0001", at="2026-10-12T15:00:00Z", actor="bob")
    assert rig["cal"].book_calls == 1


def test_a_booking_the_provider_never_confirmed_is_found_not_made_again(world):
    make, store, clock, rig = world
    gateway = make()
    rig["cal"].fail_next = "unconfirmed"  # booked at the calendar, but the answer was lost
    assert meet(gateway, request_id="lost0001")["status"] == "sending"
    sweep(gateway)
    assert store.meeting_request("lost0001")["status"] == "sending"  # not stuck long enough yet

    clock["now"] += timedelta(minutes=11)
    rig["cal"].find_fails = True  # the search itself fails: booking again could double-book
    sweep(gateway)
    assert store.meeting_request("lost0001")["status"] == "sending"
    assert rig["cal"].book_calls == 1

    rig["cal"].find_fails = False
    sweep(gateway)
    sweep(gateway)
    record = store.meeting_request("lost0001")
    assert record["status"] == "booked" and record["booking_ref"] == "mock-booking-1"
    assert rig["cal"].book_calls == 1 and len(rig["cal"].bookings) == 1


def test_a_booking_never_confirmed_for_a_day_is_failed_not_retried_forever(world):
    make, store, clock, rig = world
    gateway = make()
    rig["cal"].find_fails = True
    meet(gateway, request_id="stuck001")
    clock["now"] += timedelta(hours=25)
    sweep(gateway)
    record = store.meeting_request("stuck001")
    assert record["status"] == "failed" and "never confirmed" in record["reason"]


def test_a_taken_time_fails_with_open_times_to_try_and_is_not_retried(world):
    make, store, clock, rig = world
    gateway = make()
    assert meet(gateway, request_id="first001")["status"] == "booked"
    result = meet(gateway, request_id="clash001", contact_id="c4")
    assert result["status"] == "failed" and "taken" in result["reason"]
    assert SLOTS[1] in result["reason"] and SLOT not in result["reason"].split("open times:")[1]
    clock["now"] += timedelta(minutes=30)
    sweep(gateway)
    assert rig["cal"].book_calls == 2 and store.meeting_request("clash001")["status"] == "failed"

    rig["cal"].fail_next = "refuse"
    assert meet(gateway, request_id="refuse01", start=SLOTS[2])["status"] == "failed"


# -- what the calendar reports --------------------------------------------------------------------


def post(gateway, rig, change, ref, *, event_id, start="", previous_ref=""):
    headers, body = rig["cal"].event(ref, change, event_id=event_id, start=start,
                                     previous_ref=previous_ref)
    return handle(gateway, PUBLIC, "/calendar/mock-calendar/events", headers, body)


def test_a_contact_cancelling_or_moving_a_meeting_reaches_the_agent_once(world):
    make, store, _, rig = world
    gateway = make()
    meet(gateway, request_id="chg00001")
    meet(gateway, request_id="chg00002", contact_id="c4", start=SLOTS[1])

    assert post(gateway, rig, BookingChange.RESCHEDULED, "new-ref", event_id="e1",
                start=SLOTS[2], previous_ref="mock-booking-1")[0] == 200
    moved = store.meeting_request("chg00001")
    assert (moved["booking_ref"], moved["start"], moved["status"]) == ("new-ref", SLOTS[2], "booked")

    replay = post(gateway, rig, BookingChange.RESCHEDULED, "new-ref", event_id="e1",
                  start=SLOTS[2], previous_ref="mock-booking-1")
    assert replay[0] == 200 and replay[2] == b"applied 0"  # the same delivery, applied once
    assert post(gateway, rig, BookingChange.CANCELLED, "new-ref", event_id="e2")[0] == 200
    assert post(gateway, rig, BookingChange.CANCELLED, "new-ref", event_id="e2")[0] == 200
    assert store.meeting_request("chg00001")["status"] == "cancelled"
    assert post(gateway, rig, BookingChange.CANCELLED, "someone-elses", event_id="e3")[0] == 200
    assert store.meeting_request("chg00002")["status"] == "booked"
    assert [i.assignee for i in rig["board"].items.values()] == ["writer", "writer"]
    assert all(k.startswith("meeting-change:chg00001") for k in rig["board"].items)

    headers, body = rig["cal"].event("x", BookingChange.CANCELLED, event_id="e4")
    assert handle(gateway, PUBLIC, "/calendar/mock-calendar/events", {}, body)[0] == 403
    assert handle(gateway, PUBLIC, "/calendar/calcom/events", headers, body)[0] == 404


def test_a_contact_who_opts_out_has_their_upcoming_meetings_cancelled(world):
    make, store, _, rig = world
    gateway = make()
    meet(gateway, request_id="out00001")
    meet(gateway, request_id="out00002", contact_id="c4", start=SLOTS[1])
    gateway.opt_out("c1", reason="asked", source="test")
    sweep(gateway)
    assert rig["cal"].cancelled == ["mock-booking-1"]
    assert store.meeting_request("out00001")["status"] == "cancelled"
    assert store.meeting_request("out00002")["status"] == "booked"


def test_an_admin_cancel_reaches_the_provider_first_and_only_booked_meetings_cancel(world):
    make, store, _, rig = world
    gateway = make(spec_with("always"))
    meet(gateway, request_id="adm00001")
    with pytest.raises(ValueError, match="not booked"):
        cancel(gateway, "adm00001", actor="alice")
    approve(gateway, "adm00001", actor="alice")

    def refuse(ref, reason):
        raise ProviderError("calendar down")

    rig["cal"].cancel = refuse
    with pytest.raises(ProviderError):
        cancel(gateway, "adm00001", actor="alice")
    assert store.meeting_request("adm00001")["status"] == "booked"  # still on the calendar
    del rig["cal"].cancel
    assert cancel(gateway, "adm00001", actor="alice", reason="rescheduled")["status"] == "cancelled"
    assert rig["cal"].cancelled == ["mock-booking-1"]


def test_the_history_shows_meetings_without_the_address(world):
    make, _, _, _ = world
    gateway = make()
    meet(gateway, request_id="his00001")
    (row,) = [r for r in timeline(gateway.store, contact_id="c1") if r["channel"] == "meeting"]
    assert row["status"] == "booked" and "intro meeting" in row["what"]
    assert "dana@example.com" not in json.dumps(row)


# -- the agent's tool ------------------------------------------------------------------------------


def test_an_agent_books_through_its_signed_tool_and_never_names_an_address(tmp_path, runtime, home):
    comms = copy.deepcopy(COMMS)
    comms["communications"].update(public_url=PUBLIC, allow_test_providers=True)
    comms["communications"]["meetings"] = {
        "provider": "mock-calendar",
        "event_types": [{"id": "intro", "title": "Intro call", "provider_ref": "123",
                         "duration_minutes": 30, "purposes": ["support"]}],
        "agents": [{"agent": "customer-support", "purposes": ["support"],
                    "contact_groups": ["customers"], "event_types": ["intro"],
                    "approval": "never"}]}
    bundle = bundle_with(tmp_path, comms)
    apply_bundle(bundle, runtime, audit=AuditLog(tmp_path / "a.jsonl", tenant_id=bundle.tenant_id))
    paths = HermesPaths(home)
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              email="dana@example.com"))
    cal = MockCalendar(open_times=SLOTS)
    gateway = CallGateway(bundle.communications, store,
                          AuditLog(tmp_path / "gw.jsonl", tenant_id=bundle.tenant_id),
                          {"mock-telephony": MockTelephony()}, calendar=cal, clock=lambda: START)
    tool = load_tool(paths.calling_plugin_dir("customer-support") / "__init__.py")
    master = master_key(runtime.state_location)
    try:
        refused = json.loads(tool.handle_book_meeting({
            "contact_id": "c1", "purpose": "renewal", "event_type": "intro", "start": SLOT}))
        assert refused["status"] == "refused"
        asked = json.loads(tool.handle_meeting_slots({
            "contact_id": "c1", "event_type": "intro", "day": "2026-10-14"}))
        queued = json.loads(tool.handle_book_meeting({
            "contact_id": "c1", "purpose": "support", "event_type": "intro",
            "start": "2026-10-14T10:00:00", "to": "attacker@example.net"}))
        process(gateway, runtime.calls_inbox_path, master)
        refresh(gateway, runtime.calls_inbox_path)
        slots = json.loads(tool.handle_meeting_status({"request_id": asked["request_id"]}))
        # Asked on the same pass as the booking, which is processed first: its time is taken.
        assert slots["status"] == "ready" and slots["slots"] == SLOTS[1:2]
        status = json.loads(tool.handle_meeting_status({"request_id": queued["request_id"]}))
        assert status["status"] == "booked" and "dana@example.com" not in json.dumps(status)
        ((_, booking),) = cal.bookings.values()
        assert booking.email == "dana@example.com" and booking.start == SLOT
    finally:
        store.close()


# -- Cal.com --------------------------------------------------------------------------------------


class Script:
    """A transport that answers as Cal.com would, and keeps what it was asked."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, dict(headers), json.loads(body) if body else None))
        status, data = self.answers.pop(0)
        if isinstance(data, OSError):
            raise data
        return status, json.dumps(data).encode()


def calcom(*answers):
    script = Script(*answers)
    return CalcomCalendar(api_key="cal_x", webhook_secret="whsec", transport=script), script


REQUEST = BookingRequest(request_id="mtg00001", event_type_ref="123", start=SLOT,
                         duration_minutes=30, name="Dana", email="dana@example.com",
                         time_zone="America/Chicago")


def test_calcom_books_with_the_versioned_contract_and_tags_the_booking():
    provider, script = calcom((201, {"status": "success", "data": {"uid": "u1", "status": "accepted"}}))
    booked = provider.book(REQUEST)
    ((method, url, headers, body),) = script.requests
    assert (method, url, booked.ref) == ("POST", "https://api.cal.com/v2/bookings", "u1")
    assert headers["Authorization"] == "Bearer cal_x" and headers["cal-api-version"] == "2026-02-25"
    assert body == {"start": SLOT, "eventTypeId": 123,
                    "attendee": {"name": "Dana", "email": "dana@example.com",
                                 "timeZone": "America/Chicago", "language": "en"},
                    "metadata": {"nova_request_id": "mtg00001"}}


@pytest.mark.parametrize("answer, error", [
    ((409, {"error": {"message": "slot taken"}}), SlotUnavailable),
    ((400, {"message": "bad"}), SlotUnavailable),
    ((401, {}), ProviderError),
    ((500, {}), BookingUnconfirmed),
    ((429, {}), BookingUnconfirmed),
    ((201, {"status": "success", "data": []}), BookingUnconfirmed),
    ((0, OSError("down")), BookingUnconfirmed),
])
def test_calcom_tells_a_refusal_from_a_booking_it_cannot_vouch_for(answer, error):
    provider, _ = calcom(answer)
    with pytest.raises(error) as caught:
        provider.book(REQUEST)
    assert type(caught.value) is error  # a lost answer is never reported as a refusal


def test_calcom_finds_only_the_live_booking_made_for_this_request():
    provider, script = calcom(
        (200, {"data": [
            {"uid": "other", "status": "accepted", "metadata": {"nova_request_id": "mtg9"}},
            {"uid": "dead", "status": "cancelled", "metadata": {"nova_request_id": "mtg00001"}}],
            "pagination": {"hasMore": True, "nextCursor": "c2"}}),
        (200, {"data": [{"uid": "mine", "status": "pending",
                         "metadata": {"nova_request_id": "mtg00001"}}],
               "pagination": {"hasMore": False}}))
    found = provider.find(REQUEST)
    assert (found.ref, found.status) == ("mine", "pending")
    first, second = script.requests
    assert "attendeeEmail=dana%40example.com" in first[1] and "cursor" not in first[1]
    assert "cursor=c2" in second[1] and first[2]["cal-api-version"] == "2026-05-01"
    none, _ = calcom((200, {"data": [], "pagination": {"hasMore": False}}))
    assert none.find(REQUEST) is None
    broken, _ = calcom((500, {}))
    with pytest.raises(ProviderError):
        broken.find(REQUEST)


def test_calcom_reads_open_times_in_either_documented_shape_and_cancels():
    provider, script = calcom(
        (200, {"data": {"2026-10-14": [{"start": "2026-10-14T10:00:00.000-05:00"},
                                       "2026-10-14T16:00:00Z"]}}),
        (200, {"status": "success"}), (404, {}), (500, {}))
    assert provider.slots("123", start="2026-10-14", end="2026-10-15", time_zone="America/Chicago",
                          duration_minutes=30) == ["2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z"]
    query = script.requests[0][1]
    assert query.startswith("https://api.cal.com/v2/slots?") and "eventTypeId=123" in query
    assert script.requests[0][2]["cal-api-version"] == "2024-09-04"
    provider.cancel("u 1", "opted out")
    assert script.requests[1][1] == "https://api.cal.com/v2/bookings/u%201/cancel"
    assert script.requests[1][3] == {"cancellationReason": "opted out"}
    provider.cancel("gone", "x")  # already gone is fine
    with pytest.raises(ProviderError):
        provider.cancel("u2", "x")


def test_calcom_verifies_the_signature_and_reads_each_kind_of_change():
    provider, _ = calcom()
    body = json.dumps({"triggerEvent": "BOOKING_RESCHEDULED", "payload": {
        "uid": "new", "rescheduleUid": "old", "startTime": "2026-10-15T10:00:00-05:00",
        "attendees": [{"email": "dana@example.com"}]}}).encode()
    signature = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert provider.verify({"X-Cal-Signature-256": signature}, body)
    assert provider.verify({"x-cal-signature-256": signature.upper()}, body)
    assert not provider.verify({"x-cal-signature-256": signature}, body + b" ")
    assert not provider.verify({}, body)
    (event,) = provider.parse({}, body)
    assert (event.booking_ref, event.previous_ref, event.change, event.start) == (
        "new", "old", BookingChange.RESCHEDULED, "2026-10-15T15:00:00Z")
    assert event.provider_event_id == hashlib.sha256(body).hexdigest()  # a replay is recognised
    ignored = json.dumps({"triggerEvent": "MEETING_STARTED", "uid": "x"}).encode()
    assert provider.parse({}, ignored) == []
    with pytest.raises(ValueError):
        provider.parse({}, b"not json")


# -- the spec ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("change, message", [
    ({"provider": "nope"}, "not a known calendar provider"),
    ({"event_types": []}, "at least one event type"),
    ({"event_types": [{"id": "intro", "title": "x", "provider_ref": "no spaces",
                       "purposes": ["support"]}]}, "provider_ref"),
    ({"event_types": [{"id": "intro", "title": "x", "provider_ref": "1", "purposes": ["nope"]},
                      {"id": "sales", "title": "y", "provider_ref": "2", "purposes": ["promo"]}]},
     "nope"),
    ({"agents": [{"agent": "writer", "purposes": ["support"], "contact_groups": [],
                  "event_types": ["intro"]}]}, "not a grant"),
    ({"agents": [{"agent": "writer", "purposes": ["support"], "contact_groups": ["customers"],
                  "event_types": ["missing"]}]}, "not a declared event type"),
    ({"agents": [{"agent": "writer", "purposes": ["support"], "contact_groups": ["customers"],
                  "event_types": ["intro"], "approval": "sometimes"}]}, "approval"),
])
def test_the_spec_refuses_a_meeting_grant_it_cannot_govern(change, message):
    data = copy.deepcopy(BASE_SPEC)
    data["communications"]["meetings"] = {**copy.deepcopy(MEETINGS), **change}
    with pytest.raises(SpecError, match=message):
        parse_communications(data)


def test_meetings_need_a_public_url_and_a_test_calendar_needs_the_test_flag():
    data = {"communications": {
        "purposes": [{"id": "support"}],
        "meetings": {"provider": "mock-calendar", "event_types": [
            {"id": "intro", "title": "Intro", "provider_ref": "1", "purposes": ["support"]}]}}}
    with pytest.raises(SpecError, match="books nothing"):
        parse_communications(data)
    data["communications"]["allow_test_providers"] = True
    with pytest.raises(SpecError, match="public_url"):
        parse_communications(data)
    data["communications"]["public_url"] = PUBLIC
    assert parse_communications(data).meetings.provider == "mock-calendar"


# -- people ---------------------------------------------------------------------------------------


def test_admins_decide_meetings_over_the_api_and_the_cli(tmp_path, runtime, home, capsys):
    from nova.cli import main

    comms = copy.deepcopy(COMMS)
    comms["communications"].update(public_url=PUBLIC)
    comms["communications"]["meetings"] = {
        "provider": "mock-calendar",
        "event_types": [{"id": "intro", "title": "Intro call", "provider_ref": "123",
                         "duration_minutes": 30, "purposes": ["support"]}],
        "agents": [{"agent": "customer-support", "purposes": ["support"],
                    "contact_groups": ["customers"], "event_types": ["intro"]}]}
    bundle = bundle_with(tmp_path, comms)
    store = CommsStore(runtime.calls_store_path, tenant_id=bundle.tenant_id)
    store.put_contact(Contact("c1", "+13125550101", "America/Chicago", ("customers",),
                              email="dana@example.com"))
    gateway = CallGateway(bundle.communications, store,
                          AuditLog(tmp_path / "gw.jsonl", tenant_id=bundle.tenant_id),
                          {"mock-telephony": MockTelephony()}, calendar=MockCalendar(open_times=SLOTS),
                          clock=lambda: START)
    for request_id in ("apireq01", "clireq01"):
        request_meeting(gateway, agent="customer-support", request_id=request_id,
                        contact_id="c1", purpose="support", event_type="intro", start=SLOT)
    store.close()

    control = ControlAPI(bundle, runtime, audit=AuditLog(tmp_path / "c.jsonl",
                                                         tenant_id=bundle.tenant_id))
    listed = control.handle("/platform/v1/communications/meetings").body["meetings"]
    assert {m["request_id"] for m in listed} == {"apireq01", "clireq01"}
    path = "/platform/v1/communications/meetings/apireq01/decide"
    viewer, admin = Principal(name="v", role="viewer"), Principal(name="alice", role="admin")
    assert control.write(path, viewer, {"action": "reject"}).status == 403
    assert control.write(path, admin, {"action": "explode"}).status == 400
    assert control.write(path.replace("apireq01", "missing1"), admin,
                         {"action": "reject"}).status == 404
    assert control.write(path, admin, {"action": "reject", "reason": "tone"}).body["status"] == "rejected"
    assert control.write(path, admin, {"action": "approve"}).status == 409

    calls = ["--home", str(home), "calls"]
    root = str(tmp_path / "bundle")
    assert main([*calls, "meetings", root]) == 0
    assert "clireq01" in capsys.readouterr().out
    assert main([*calls, "decide-meeting", root, "clireq01", "reject", "--actor", "alice"]) == 0
    assert "rejected" in capsys.readouterr().out
