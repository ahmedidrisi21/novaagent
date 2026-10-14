"""Cal.com as the calendar provider.

Cal.com's API takes a version header per endpoint, and documents **no idempotency key** for
creating a booking. So ``find`` reads the contact's bookings around the requested time and
matches the ``nova_request_id`` metadata this adapter wrote, and the caller finds before it
books again after anything ambiguous. Webhooks carry ``x-cal-signature-256``: a hex
HMAC-SHA256 of the body under the webhook secret. There is no timestamp to bound replay, so
each delivery is identified by a hash of its body and applied once.

Vendor contract: https://cal.com/docs/api-reference/v2/bookings/create-a-booking,
.../bookings/get-all-bookings, .../bookings/cancel-a-booking,
.../slots/get-available-time-slots-for-an-event-type and
https://cal.com/docs/developing/guides/automation/webhooks. Not verified against a live
account: the signature encoding, the error for a taken slot, and whether a slot is an object
or a bare string (the docs show both, so both are read).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timedelta, timezone, UTC
from typing import Any, Mapping, Optional

from nova.communications.booking import (
    Booked,
    BookingChange,
    BookingEvent,
    BookingRequest,
    BookingUnconfirmed,
    CalendarProvider,
    SlotUnavailable,
)
from nova.communications.contracts import ProviderError
from nova.communications.http import Transport, urllib_transport

DEFAULT_BASE_URL = "https://api.cal.com"
SIGNATURE_HEADER = "x-cal-signature-256"
#: Written into every booking so it can be found again; the value is our request id.
METADATA_KEY = "nova_request_id"
V_BOOK, V_SLOTS, V_LIST = "2026-02-25", "2024-09-04", "2026-05-01"
MAX_PAGES = 5
TRIGGERS = {
    "BOOKING_CREATED": BookingChange.CREATED,
    "BOOKING_CANCELLED": BookingChange.CANCELLED,
    "BOOKING_RESCHEDULED": BookingChange.RESCHEDULED,
    "BOOKING_REJECTED": BookingChange.REJECTED,
}
#: Booking states that no longer hold the time.
_DEAD = ("cancelled", "rejected")


def _utc(value: str) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalcomCalendar(CalendarProvider):
    id = "calcom"

    def __init__(self, *, api_key: str, webhook_secret: str, base_url: str = DEFAULT_BASE_URL,
                 transport: Transport = urllib_transport) -> None:
        if not (api_key and webhook_secret):
            raise ValueError("Cal.com needs an API key and a webhook signing secret")
        self._key = api_key
        self._secret = webhook_secret.encode("utf-8")
        self._base = base_url.rstrip("/")
        self._transport = transport

    @classmethod
    def from_env(cls, env: Mapping[str, str], **options) -> CalcomCalendar:
        return cls(api_key=env.get("CALCOM_API_KEY", ""),
                   webhook_secret=env.get("CALCOM_WEBHOOK_SECRET", ""), **options)

    def _call(self, method: str, path: str, version: str, payload: Optional[dict] = None,
              query: Optional[Mapping[str, str]] = None) -> tuple[int, Any]:
        url = f"{self._base}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        headers = {"Authorization": f"Bearer {self._key}", "cal-api-version": version}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            status, raw = self._transport(method, url, headers, body)
        except OSError as exc:
            raise BookingUnconfirmed(f"Cal.com could not be reached: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        return status, data

    def slots(self, event_type_ref: str, *, start: str, end: str, time_zone: str,
              duration_minutes: int) -> list[str]:
        status, data = self._call("GET", "/v2/slots", V_SLOTS, query={
            "eventTypeId": event_type_ref, "start": start, "end": end, "timeZone": "UTC",
            "duration": str(duration_minutes)})
        if not 200 <= status < 300:
            raise ProviderError(f"Cal.com could not list open times ({status})")
        days = data.get("data") if isinstance(data, dict) else None
        out: list[str] = []
        for day in (days or {}).values() if isinstance(days, dict) else ():
            for slot in day if isinstance(day, list) else ():
                value = slot.get("start") if isinstance(slot, dict) else slot
                if isinstance(value, str):
                    try:
                        out.append(_utc(value))
                    except ValueError:
                        continue
        return sorted(set(out))

    def find(self, request: BookingRequest) -> Optional[Booked]:
        around = datetime.fromisoformat(request.start)
        query: dict[str, str] = {
            "attendeeEmail": request.email,
            "afterStart": _utc((around - timedelta(minutes=1)).isoformat()),
            "beforeEnd": _utc((around + timedelta(minutes=request.duration_minutes + 1)).isoformat()),
            "limit": "100"}
        for _ in range(MAX_PAGES):
            status, data = self._call("GET", "/v2/bookings", V_LIST, query=query)
            if not 200 <= status < 300:
                raise ProviderError(f"Cal.com could not list bookings ({status})")
            for booking in data.get("data") or []:
                meta = booking.get("metadata") if isinstance(booking, dict) else None
                if (isinstance(meta, dict) and meta.get(METADATA_KEY) == request.request_id
                        and str(booking.get("status")) not in _DEAD and booking.get("uid")):
                    return Booked(ref=str(booking["uid"]), status=str(booking.get("status")))
            page = data.get("pagination") or {}
            if not (page.get("hasMore") and page.get("nextCursor")):
                return None
            query["cursor"] = str(page["nextCursor"])
        raise ProviderError("Cal.com has more bookings than could be searched")

    def book(self, request: BookingRequest) -> Booked:
        status, data = self._call("POST", "/v2/bookings", V_BOOK, {
            "start": request.start,
            "eventTypeId": int(request.event_type_ref),
            "attendee": {"name": request.name, "email": request.email,
                         "timeZone": request.time_zone, "language": "en"},
            "metadata": {METADATA_KEY: request.request_id},
        })
        booking = data.get("data") if isinstance(data, dict) else None
        if 200 <= status < 300 and isinstance(booking, dict) and booking.get("uid"):
            return Booked(ref=str(booking["uid"]), status=str(booking.get("status") or "accepted"))
        message = str(data.get("error", {}).get("message") if isinstance(data.get("error"), dict)
                      else data.get("message") or "")[:200]
        if status in (400, 409, 422):
            raise SlotUnavailable(f"Cal.com would not book that time ({status}): {message}")
        if status in (401, 403, 404):
            raise ProviderError(f"Cal.com refused the request ({status}): {message}")
        # 2xx without a booking, 429, 5xx: the request may have been acted on.
        raise BookingUnconfirmed(f"Cal.com did not confirm the booking ({status}): {message}")

    def cancel(self, ref: str, reason: str) -> None:
        status, _ = self._call(
            "POST", f"/v2/bookings/{urllib.parse.quote(ref, safe='')}/cancel", V_BOOK,
            {"cancellationReason": reason[:200]})
        if status == 404 or 200 <= status < 300:
            return
        raise ProviderError(f"Cal.com could not cancel the booking ({status})")

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return bool(given) and hmac.compare_digest(given.strip().lower(), expected)

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[BookingEvent]:
        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"unreadable webhook: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError("the webhook is not an object")
        change = TRIGGERS.get(str(event.get("triggerEvent") or ""))
        payload = event.get("payload")
        if change is None or not isinstance(payload, dict) or not payload.get("uid"):
            return []
        attendees = payload.get("attendees")
        first = attendees[0] if isinstance(attendees, list) and attendees else {}
        try:
            start = _utc(str(payload["startTime"])) if payload.get("startTime") else ""
        except ValueError:
            start = ""
        return [BookingEvent(
            provider_event_id=hashlib.sha256(body).hexdigest(),
            booking_ref=str(payload["uid"]), change=change, start=start,
            previous_ref=str(payload.get("rescheduleUid") or ""),
            attendee_email=str(first.get("email") or "") if isinstance(first, dict) else "")]
