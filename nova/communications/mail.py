"""What every email provider adapter must do, in this layer's own vocabulary.

Email here is outbound and narrow: a follow-up a call earned, sent to the address the
tenant has on file for the contact, from the tenant's own sender. The adapter sends one
message idempotently and turns the provider's delivery webhooks into :class:`EmailEvent`.
Inbound mail is not read (a reply goes to the tenant's ``reply_to`` mailbox, where a
person reads it).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


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
