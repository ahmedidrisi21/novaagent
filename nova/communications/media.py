"""The media bridge: where a carrier streams an answered call's audio for a stream engine.

One WebSocket per call, at ``/media/<engine>``, from Twilio or Telnyx (both stream 8 kHz
mu-law in near-identical messages). The stream's ``start`` message carries the call id
and token the answer plan put there; the gateway checks them exactly as it checks
a SIP session, and binds the stream to the call. Anything else is closed without a word.

Internet-facing, like the webhook listener, and like it, never the Control API.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from nova.communications.contracts import MediaChannel
from nova.communications.gateway import CallGateway

log = logging.getLogger(__name__)

#: Seconds to wait for the stream's ``start`` before giving up on it.
START_TIMEOUT = 10.0


def _await_start(channel: MediaChannel) -> Optional[dict]:
    """The ``start`` message, skipping ``connected``. None on anything else."""
    for _ in range(3):
        try:
            message = json.loads(channel.recv(timeout=START_TIMEOUT))
        except (EOFError, TimeoutError, TypeError, ValueError):
            return None
        if message.get("event") == "start":
            return message
        if message.get("event") != "connected":
            return None
    return None


def _parameters(details: dict) -> dict:
    """The call id and token the answer plan attached: Twilio sends ``customParameters``
    as an object, Telnyx ``custom_parameters`` as a list of name/value pairs."""
    raw = details.get("customParameters") or details.get("custom_parameters") or {}
    if isinstance(raw, list):
        raw = {str(item.get("name")): item.get("value") for item in raw if isinstance(item, dict)}
    return raw if isinstance(raw, dict) else {}


def bridge(gateway: CallGateway, channel: MediaChannel, path: str) -> None:
    """Serve one media stream. Blocks until the call's stream ends."""
    route = path.split("?", 1)[0].strip("/").split("/")
    try:
        if len(route) != 2 or route[0] != "media":
            return
        start = _await_start(channel)
        if start is None:
            return
        details = start.get("start") or {}
        parameters = _parameters(details)
        stream_sid = str(start.get("streamSid") or details.get("streamSid") or
                         start.get("stream_id") or "")
        granted = gateway.stream_started(
            route[1],
            call_id=str(parameters.get("call_id", "")),
            token=str(parameters.get("token", "")),
            stream_sid=stream_sid,
        )
        if granted is None:
            return
        voice, request, hooks = granted
        try:
            voice.converse(channel, stream_sid, request, hooks)
        except Exception as exc:  # noqa: BLE001 — a broken engine must still hang up
            log.warning("stream engine %s failed on call %s: %s", route[1], request.call_id, exc)
            # The next engine in the agent's order takes the call, or the phone leg ends.
            gateway.engine_failed(request.call_id, route[1], f"{type(exc).__name__}: {exc}")
    finally:
        try:
            channel.close()
        except Exception:  # noqa: BLE001 — already closed by the other side
            pass


def build_media_server(gateway: CallGateway, *, host: str = "127.0.0.1", port: int = 8091):
    """A threaded WebSocket server for the bridge. Run it behind a TLS proxy (wss://)."""
    from nova.communications.ws import serve

    return serve(lambda channel, path: bridge(gateway, channel, path), host=host, port=port)
