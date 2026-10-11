"""What every carrier and voice engine adapter must do, in this layer's own vocabulary.

The workflow above this module asks for ``place_call`` and never learns whether Twilio or
Telnyx answered. Provider-specific shapes stop at an adapter's ``normalize``: past that
point there is one event vocabulary, one status set and one list of failure reasons.

**A call's status only moves forward.** Carriers deliver webhooks late, twice and out of
order; a ``ringing`` that arrives after ``answered`` is recorded and ignored, and a terminal
status absorbs everything after it. That rule lives here, as data, so the store and every
adapter apply the same one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class CallStatus(str, Enum):
    REQUESTED = "requested"
    APPROVAL_REQUIRED = "approval_required"
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    SESSION_CONNECTED = "session_connected"
    TRANSFERRED = "transferred"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    POLICY_BLOCKED = "policy_blocked"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL

    @property
    def active(self) -> bool:
        """Holding a line, or about to. Counted against concurrency."""
        return self in ACTIVE


TERMINAL = frozenset(
    {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.CANCELLED, CallStatus.POLICY_BLOCKED}
)
ACTIVE = frozenset(
    {
        CallStatus.INITIATED,
        CallStatus.RINGING,
        CallStatus.ANSWERED,
        CallStatus.SESSION_CONNECTED,
        CallStatus.TRANSFERRED,
    }
)

#: Forward order. A transition is legal only to a higher rank; terminal states share the
#: top rank so nothing leaves one.
_RANK: Mapping[CallStatus, int] = {
    CallStatus.REQUESTED: 0,
    CallStatus.APPROVAL_REQUIRED: 1,
    CallStatus.INITIATED: 2,
    CallStatus.RINGING: 3,
    CallStatus.ANSWERED: 4,
    CallStatus.SESSION_CONNECTED: 5,
    CallStatus.TRANSFERRED: 6,
    CallStatus.COMPLETED: 9,
    CallStatus.FAILED: 9,
    CallStatus.CANCELLED: 9,
    CallStatus.POLICY_BLOCKED: 9,
}


def advances(current: CallStatus, incoming: CallStatus) -> bool:
    """Whether ``incoming`` moves a call at ``current`` forward."""
    if current.terminal:
        return False
    return _RANK[incoming] > _RANK[current]


class FailureReason(str, Enum):
    """Why a call ended without completing. Retry rules key on this, so it is never free text."""

    BUSY = "busy"
    NO_ANSWER = "no_answer"
    INVALID_NUMBER = "invalid_number"
    CARRIER_REJECTED = "carrier_rejected"
    PROVIDER_FAILURE = "provider_failure"
    MACHINE_DETECTED = "machine_detected"
    POLICY_BLOCKED = "policy_blocked"


#: Event names on the wire, one per status.
def event_kind(status: CallStatus) -> str:
    return f"call.{status.value}"


@dataclass(frozen=True)
class PlaceCall:
    """Everything a carrier needs to dial, resolved by the gateway — never by an agent.

    The destination comes from a contact record and the caller id from the tenant's own
    numbers. An agent names a contact; it never supplies a phone number.
    """

    call_id: str
    tenant_id: str
    to_number: str
    from_number: str
    max_seconds: int
    #: Ask the carrier to detect a voicemail greeting before anything is said.
    detect_machine: bool = True


@dataclass(frozen=True)
class ProviderEvent:
    """A carrier callback after normalization."""

    provider: str
    #: The carrier's own id for this callback. Deduplication keys on it.
    provider_event_id: str
    #: The carrier's id for the call, returned by :meth:`TelephonyProvider.place_call`.
    provider_call_ref: str
    status: CallStatus
    occurred_at: str = ""
    failure: Optional[FailureReason] = None
    #: Our own call id, when the carrier echoes it back on a signed URL. Finds the call
    #: for a callback that beats the carrier reference into the store.
    call_id_hint: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SipTarget:
    """Where a carrier bridges an answered call, and the headers it must carry."""

    uri: str
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AnswerPlan:
    """What happens the moment a call is answered, in carrier-neutral terms.

    ``say`` is the tenant's disclosure, spoken by the carrier before any model is on the
    line — so the words are the tenant's, every time, and never paraphrased.
    """

    hang_up: bool = False
    say: str = ""
    language: str = ""
    connect: Optional[SipTarget] = None


class ProviderError(Exception):
    """A provider refused or could not be reached. Carries a normalized reason."""

    def __init__(self, message: str, *, reason: FailureReason = FailureReason.PROVIDER_FAILURE):
        super().__init__(message)
        self.reason = reason


class TelephonyProvider(ABC):
    """A carrier adapter. Credentials are the adapter's own business, held server side."""

    #: Matches an id in :mod:`nova.communications.providers`.
    id: str = ""

    @abstractmethod
    def place_call(self, request: PlaceCall) -> str:
        """Dial. Returns the carrier's call reference, or raises :class:`ProviderError`."""

    @abstractmethod
    def end_call(self, provider_call_ref: str) -> None:
        """Hang up. Must be safe to call on a call that already ended."""

    @abstractmethod
    def verify(self, headers: Mapping[str, str], body: bytes, url: str) -> bool:
        """Whether a callback really came from this carrier. False means drop it.

        ``url`` is the exact URL the carrier called, query string included: carriers sign
        it, and a URL rebuilt from parts will not match.
        """

    @abstractmethod
    def parse(self, headers: Mapping[str, str], body: bytes, url: str) -> ProviderEvent:
        """Translate a verified callback — a status update or the answer request — into
        the shared vocabulary. A machine-answered call is ``FAILED`` with
        ``MACHINE_DETECTED``. Raises ``ValueError`` for a body it does not understand;
        the gateway drops it rather than guessing a status.
        """

    @abstractmethod
    def render_answer(self, plan: AnswerPlan) -> tuple[str, bytes]:
        """The carrier's own reply to its answer request: ``(content_type, body)``."""


@dataclass(frozen=True)
class VoiceSessionRequest:
    """What a voice engine is given for one call. No credential and no phone number."""

    call_id: str
    tenant_id: str
    agent: str
    instructions: str
    voice: str = ""
    language: str = ""


@dataclass(frozen=True)
class IncomingSession:
    """A voice engine announcing a bridged call, after verification and parsing.

    ``headers`` are the SIP headers the carrier forwarded. They are **claims**: the
    gateway checks the call token in them before believing which call this is.
    """

    session_id: str
    event_id: str
    headers: Mapping[str, str] = field(default_factory=dict)


class VoiceSessionProvider(ABC):
    """A voice engine reached over SIP: the carrier bridges the call, the engine talks."""

    id: str = ""

    @abstractmethod
    def sip_target(self, call_id: str, token: str) -> SipTarget:
        """Where the carrier should bridge this call, carrying the id and its token."""

    @abstractmethod
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Whether a webhook really came from this engine."""

    @abstractmethod
    def parse_incoming(self, headers: Mapping[str, str], body: bytes) -> Optional[IncomingSession]:
        """A verified webhook as an incoming session, or None for an event that is not one.

        Raises ``ValueError`` for a body it cannot read.
        """

    @abstractmethod
    def accept(self, session_id: str, request: VoiceSessionRequest) -> None:
        """Take the call with these instructions. Raises :class:`ProviderError`."""

    @abstractmethod
    def reject(self, session_id: str) -> None:
        """Refuse a session the gateway does not recognise."""

    @abstractmethod
    def hang_up(self, session_id: str) -> None:
        """End a session. Must be safe to call twice."""
