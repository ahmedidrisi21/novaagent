"""What every calendar provider adapter must do, in this layer's own vocabulary.

A meeting here is narrow: a time on a tenant's event type, for a contact at the address the
tenant has on file. The adapter lists open times, books one, finds a booking it made
earlier, cancels one, and turns the provider's signed webhooks into :class:`BookingEvent`.

**Booking can be ambiguous.** A request that times out may or may not have booked. That is
:class:`BookingUnconfirmed`, and the caller must :meth:`CalendarProvider.find` before it
books again. A refusal — the time is taken, the event type is wrong — is definite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

from nova.communications.contracts import ProviderError


class SlotUnavailable(ProviderError):
    """The provider refused this time: taken, outside availability, or not bookable."""


class BookingUnconfirmed(ProviderError):
    """The provider could not be reached or failed mid-request. The booking may exist."""


class BookingChange(str, Enum):
    CREATED = "created"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class BookingRequest:
    #: Our id. Carried to the provider as metadata so a booking can be found again.
    request_id: str
    event_type_ref: str
    #: UTC, ``2026-10-13T15:00:00Z``.
    start: str
    duration_minutes: int
    name: str
    email: str
    time_zone: str


@dataclass(frozen=True)
class Booked:
    ref: str
    #: ``accepted`` or ``pending`` (the host has to confirm).
    status: str = "accepted"


@dataclass(frozen=True)
class BookingEvent:
    #: Stable for one delivery's content, so a redelivery is recognised.
    provider_event_id: str
    booking_ref: str
    change: BookingChange
    start: str = ""
    #: The booking this one replaced, when the contact rescheduled.
    previous_ref: str = ""
    attendee_email: str = ""


class CalendarProvider(ABC):
    id: str = ""

    @abstractmethod
    def slots(self, event_type_ref: str, *, start: str, end: str, time_zone: str,
              duration_minutes: int) -> list[str]:
        """Open start times (UTC ``Z``) in ``[start, end]``. Raises ``ProviderError``."""

    @abstractmethod
    def find(self, request: BookingRequest) -> Optional[Booked]:
        """The live booking made for ``request.request_id``, or None. Raises
        ``ProviderError`` when it cannot tell."""

    @abstractmethod
    def book(self, request: BookingRequest) -> Booked:
        """Book once. Raises :class:`SlotUnavailable` or :class:`BookingUnconfirmed`."""

    @abstractmethod
    def cancel(self, ref: str, reason: str) -> None:
        """Cancel a booking. Already gone is not an error."""

    @abstractmethod
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only for a webhook the provider signed."""

    @abstractmethod
    def parse(self, headers: Mapping[str, str], body: bytes) -> list[BookingEvent]:
        """Normalised events from a verified webhook; events this layer does not use are dropped."""
