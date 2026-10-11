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
    #: Ask the carrier to detect a voicemail greeting before connecting the voice engine.
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
    detail: Mapping[str, Any] = field(default_factory=dict)


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
        """Whether a callback really came from this carrier. False means drop it."""

    @abstractmethod
    def normalize(self, payload: Mapping[str, Any]) -> ProviderEvent:
        """Translate a verified callback into the shared vocabulary.

        Raises ``ValueError`` for a payload it does not understand; the gateway records
        and drops it rather than guessing a status.
        """


@dataclass(frozen=True)
class VoiceSessionRequest:
    """What a voice engine is given for one call. No credential and no raw phone number."""

    call_id: str
    tenant_id: str
    agent: str
    voice: str = ""
    language: str = ""
    #: Opaque reference to the call's purpose and context, resolved server side.
    context_ref: str = ""


class VoiceSessionProvider(ABC):
    """A voice engine adapter."""

    id: str = ""

    @abstractmethod
    def open_session(self, request: VoiceSessionRequest) -> str:
        """Start a session for an answered call. Returns the engine's session id."""

    @abstractmethod
    def close_session(self, session_id: str) -> None:
        """End a session. Must be safe to call twice."""
