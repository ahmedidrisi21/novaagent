"""Per-vendor settings that are configuration, not secrets: which Telnyx Call Control app
dials, which ElevenLabs agent talks. The keys for both live with the gateway."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from nova._fields import Doc
from nova.errors import SpecError

TELNYX = "telnyx"
ELEVENLABS = "elevenlabs-agents"
_AGENT_ID = re.compile(r"^agent_[A-Za-z0-9]{8,64}$")


@dataclass(frozen=True)
class VendorSpec:
    telnyx_connection_id: str = ""
    elevenlabs_agent_id: str = ""
    #: ElevenLabs' regional API host, e.g. ``api.eu.residency.elevenlabs.io``. Empty: global.
    elevenlabs_host: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.telnyx_connection_id:
            out["telnyx"] = {"connection_id": self.telnyx_connection_id}
        if self.elevenlabs_agent_id:
            out["elevenlabs"] = {"agent_id": self.elevenlabs_agent_id,
                                 **({"host": self.elevenlabs_host} if self.elevenlabs_host else {})}
        return out


def parse_vendors(telnyx: Optional[Doc], elevenlabs: Optional[Doc], *, telephony: tuple,
                  voice: tuple, source) -> VendorSpec:
    connection = telnyx.str_("connection_id") if telnyx is not None else ""
    agent = elevenlabs.str_("agent_id") if elevenlabs is not None else ""
    host = elevenlabs.str_("host") if elevenlabs is not None else ""
    for doc in (telnyx, elevenlabs):
        if doc is not None:
            doc.reject_unknown()
    if TELNYX in telephony and not connection:
        raise SpecError("telnyx.connection_id is required to dial through Telnyx: the Call "
                        "Control application's id", field="communications.telnyx", source=source)
    if ELEVENLABS in voice and not _AGENT_ID.match(agent):
        raise SpecError("elevenlabs.agent_id (agent_...) is required to talk through "
                        "ElevenLabs Agents", field="communications.elevenlabs", source=source)
    if host and not re.match(r"^api(\.[a-z]{2})?(\.residency)?\.elevenlabs\.io$", host):
        raise SpecError("must be an ElevenLabs API host, e.g. api.eu.residency.elevenlabs.io",
                        field="communications.elevenlabs.host", source=source)
    return VendorSpec(telnyx_connection_id=connection, elevenlabs_agent_id=agent,
                      elevenlabs_host=host)
