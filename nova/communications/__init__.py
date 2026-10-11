"""Phone calls placed by agents: the tenant's grant, the call gate, and the carrier seam.

This package owns what a carrier and a voice engine do not: which agents may call, whom,
why, from which number, and how often; consent and opt-outs; approval bound to the exact
call; and a call state that survives duplicate and out-of-order carrier callbacks. Carriers
and voice engines plug in behind :mod:`nova.communications.contracts`.

``docs/platform/COMMUNICATIONS.md`` is the design note, including what is not built yet.
"""

from nova.communications.contracts import (
    CallStatus,
    FailureReason,
    PlaceCall,
    ProviderError,
    ProviderEvent,
    SipVoiceEngine,
    TelephonyProvider,
    VoiceSessionRequest,
)
from nova.communications.gate import CallDecision, CallFacts, Contact, decide_call
from nova.communications.gateway import CallGateway, CallResult, IngestResult
from nova.communications.providers import (
    PROVIDERS,
    CommsProvider,
    Kind,
    catalogue,
    get_provider,
)
from nova.communications.spec import (
    COMMUNICATIONS_FILE,
    CallingProfile,
    CommunicationsSpec,
    Purpose,
    VoiceChoice,
    load_communications,
    parse_communications,
)
from nova.communications.store import CallRecord, CommsStore

__all__ = [
    "COMMUNICATIONS_FILE",
    "PROVIDERS",
    "CallDecision",
    "CallFacts",
    "CallGateway",
    "CallRecord",
    "CallResult",
    "CallStatus",
    "CallingProfile",
    "CommsProvider",
    "CommsStore",
    "CommunicationsSpec",
    "Contact",
    "FailureReason",
    "IngestResult",
    "Kind",
    "PlaceCall",
    "ProviderError",
    "ProviderEvent",
    "Purpose",
    "TelephonyProvider",
    "VoiceChoice",
    "SipVoiceEngine",
    "VoiceSessionRequest",
    "catalogue",
    "decide_call",
    "get_provider",
    "load_communications",
    "parse_communications",
]
