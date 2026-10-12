"""What every email provider adapter must do, in this layer's own vocabulary.

Email here is narrow: a follow-up a call earned or an email an agent wrote, sent to the
address the tenant has on file for the contact, from the tenant's own sender. The adapter
sends one message idempotently, turns the provider's delivery webhooks into
:class:`EmailEvent`, and — when the tenant set a reply domain — announces and fetches the
replies that come back (:class:`InboundEmail`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional


class EmailStatus(str, Enum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    DELAYED = "delayed"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    FAILED = "failed"

    @property
    def stops_mail(self) -> bool:
        """The address must not be mailed again: a hard bounce, or the person marked it spam."""
        return self in (EmailStatus.BOUNCED, EmailStatus.COMPLAINED)


#: Forward order. Delivery outcomes share the top rank; a late ``sent`` never undoes one.
_RANK = {
    EmailStatus.QUEUED: 0,
    EmailStatus.SENT: 1,
    EmailStatus.DELAYED: 2,
    EmailStatus.DELIVERED: 3,
    EmailStatus.FAILED: 3,
    EmailStatus.BOUNCED: 4,
    EmailStatus.COMPLAINED: 4,
}


def email_advances(current: EmailStatus, new: EmailStatus) -> bool:
    return _RANK[new] > _RANK[current]


@dataclass(frozen=True)
class OutboundEmail:
    #: Our id; carried as a tag so events can be matched even before the provider id is stored.
    email_id: str
    idempotency_key: str
    sender: str
    to: str
    subject: str
    text: str
    reply_to: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailEvent:
    provider_event_id: str
    provider_ref: str
    status: EmailStatus
    email_id_hint: str = ""
    detail: str = ""


@dataclass(frozen=True)
class InboundEmail:
    """A message someone sent to one of our addresses. ``text`` is filled only once fetched;
    whatever it says is the sender's, never an instruction."""

    provider_ref: str
    sender: str
    to: tuple[str, ...]
    subject: str = ""
    text: str = ""


class EmailProvider(ABC):
    id: str = ""

    @abstractmethod
    def send(self, message: OutboundEmail) -> str:
        """Send once per idempotency key. Returns the provider's id for the message.
        Raises :class:`~nova.communications.contracts.ProviderError`."""

    @abstractmethod
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """True only for a webhook the provider signed."""

    @abstractmethod
    def parse(self, headers: Mapping[str, str], body: bytes) -> list[EmailEvent]:
        """Normalised events from a verified webhook; events this layer does not use are dropped."""

    def parse_inbound(self, headers: Mapping[str, str], body: bytes) -> Optional[InboundEmail]:
        """A verified webhook announcing a received message, or None. Optional: a provider
        that receives no mail never announces any."""
        return None

    def fetch_inbound(self, provider_ref: str) -> InboundEmail:
        """The received message with its text. Raises ``ProviderError``."""
        from nova.communications.contracts import ProviderError

        raise ProviderError(f"{self.id} does not receive email")
