"""OpenAI GPT-Live as a voice engine, reached over SIP.

The carrier bridges an answered call to ``sip:<project>@sip.api.openai.com``. OpenAI then
announces the session with a signed ``live.transport.incoming`` webhook, and the gateway
accepts it with the purpose's instructions and the agent's voice — or rejects it when the
call token in the SIP headers does not check out. Only HTTPS requests leave this process;
the audio never passes through it.

**Mid-call questions go over a sideband.** When the gateway has an answerer, the session
is accepted with client delegation and :class:`LiveSideband` attaches to it over a
WebSocket. Each ``session.delegation.created`` is answered from what the caller just said,
by a read-only lookup, and the answer is appended as commentary for the voice to
paraphrase. The delegation event carries no question text by design, so the question is
the caller's speech since the last delegation.

Vendor contract: https://developers.openai.com/api/docs/guides/realtime-sip (Live sessions)
and the Standard Webhooks specification for signatures.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import logging
import threading
import time
from typing import Callable, Mapping, Optional

from nova.communications.contracts import (
    Answerer,
    CallHooks,
    IncomingSession,
    MediaChannel,
    ProviderError,
    SipTarget,
    SipVoiceEngine,
    VoiceSessionRequest,
)
from nova.communications.http import Transport, urllib_transport

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-live-1"
DEFAULT_VOICE = "marin"
SIP_HOSTS = {"us": "sip.api.openai.com", "eu": "sip-eu.api.openai.com"}

#: Seconds a webhook's timestamp may differ from now before it is treated as a replay.
TOLERANCE = 300

#: The deprecated name is accepted during the vendor's migration window.
INCOMING_EVENTS = frozenset({"live.transport.incoming", "live.call.incoming"})

#: SIP headers the gateway puts on the bridge, so the incoming session can be matched to
#: a call. Lower-case: SIP header names are case-insensitive and arrive in either form.
CALL_HEADER = "x-call-id"
TOKEN_HEADER = "x-call-token"

#: Characters per commentary append. The vendor's limit is 500 tokens; this stays well
#: inside it for any language.
APPEND_CHARS = 1200

#: The most caller speech one delegation carries. A question is a sentence or two; the
#: tail is what was just asked.
QUESTION_CHARS = 600

#: Characters of each side's recent speech the payment guard sees at once.
GUARD_CHARS = 80

#: Opens a sideband. Injectable so tests never touch the network.
Connector = Callable[[str, Mapping[str, str]], MediaChannel]


def _ws_connect(url: str, headers: Mapping[str, str]) -> MediaChannel:
    from nova.communications.ws import connect

    return connect(url, headers)


class LiveSideband:
    """Answers one session's delegations until the session closes. Testable without a socket."""

    def __init__(self, channel: MediaChannel, answer: Optional[Answerer],
                 heard: Optional[Callable[[str, str], None]] = None) -> None:
        self._channel = channel
        self._answer = answer
        self._listener = heard
        self._heard = ""
        #: The last stretch said by each side, for the payment guard: transcript deltas
        #: are fragments, and a card number arrives across several of them.
        self._recent = {"caller": "", "agent": ""}
        self._ids = itertools.count(1)

    def _send(self, kind: str, delegation_id: Optional[str], content: str) -> None:
        self._channel.send(json.dumps({
            "type": kind,
            "event_id": f"evt_gw_{next(self._ids)}",
            "delegation_id": delegation_id,
            "content": content,
        }))

    def run(self) -> None:
        while True:
            try:
                raw = self._channel.recv()
            except EOFError:
                return
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = event.get("type") if isinstance(event, dict) else None
            if kind in ("session.input_transcript.delta", "session.output_transcript.delta"):
                delta = str(event.get("delta") or "")
                speaker = "caller" if kind.startswith("session.input") else "agent"
                if speaker == "caller":
                    self._heard = (self._heard + delta)[-QUESTION_CHARS:]
                self._recent[speaker] = (self._recent[speaker] + delta)[-GUARD_CHARS:]
                if self._listener is not None:
                    self._listener(speaker, self._recent[speaker])
            elif kind == "session.delegation.created":
                delegation_id = (event.get("delegation") or {}).get("id")
                if delegation_id:
                    self._delegate(str(delegation_id))
            elif kind == "session.closed":
                return
            elif kind == "error":
                log.warning("live sideband error: %s", (event.get("error") or {}).get("message"))

    def _delegate(self, delegation_id: str) -> None:
        question, self._heard = self._heard.strip(), ""
        self._send("session.thinking.append", delegation_id, "Looking that up.")
        try:
            reply = self._answer(question) if question and self._answer else ""
        except Exception as exc:  # noqa: BLE001 — a failed lookup must not drop the call
            log.warning("delegation lookup failed: %s", exc)
            reply = ""
        reply = reply or ("I could not find that. Offer to follow up in writing rather than guess.")
        for start in range(0, len(reply), APPEND_CHARS):
            self._send("session.commentary.append", delegation_id, reply[start:start + APPEND_CHARS])


def sign(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    """A Standard Webhooks ``v1`` signature: HMAC-SHA256 over ``id.timestamp.body``."""
    key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


class OpenAILiveVoice(SipVoiceEngine):
    id = "openai-live"

    def __init__(
        self,
        *,
        api_key: str,
        project_id: str,
        webhook_secret: str,
        model: str = DEFAULT_MODEL,
        region: str = "us",
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport = urllib_transport,
        connector: Connector = _ws_connect,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not (api_key and project_id.startswith("proj_") and webhook_secret):
            raise ValueError(
                "GPT-Live needs an API key, a project id starting proj_, and a webhook secret"
            )
        if region not in SIP_HOSTS:
            raise ValueError(f"region must be one of {', '.join(SIP_HOSTS)}")
        self._key = api_key
        self._project = project_id
        self._secret = webhook_secret
        self._model = model
        self._host = SIP_HOSTS[region]
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._connector = connector
        self._clock = clock

    @classmethod
    def from_env(cls, env: Mapping[str, str], **options) -> "OpenAILiveVoice":
        return cls(
            api_key=env.get("OPENAI_API_KEY", ""),
            project_id=env.get("OPENAI_PROJECT_ID", ""),
            webhook_secret=env.get("OPENAI_WEBHOOK_SECRET", ""),
            **options,
        )

    def bridge_target(self, call_id: str, token: str) -> SipTarget:
        return SipTarget(
            uri=f"sip:{self._project}@{self._host};transport=tls",
            headers={"X-Call-Id": call_id, "X-Call-Token": token},
        )

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        message_id = lowered.get("webhook-id", "")
        timestamp = lowered.get("webhook-timestamp", "")
        given = lowered.get("webhook-signature", "")
        if not (message_id and timestamp.isdigit() and given):
            return False
        if abs(self._clock() - int(timestamp)) > TOLERANCE:
            return False
        expected = sign(self._secret, message_id, timestamp, body)
        # The header may list several space-separated signatures during secret rotation.
        return any(
            part.startswith("v1,") and hmac.compare_digest(part[3:], expected)
            for part in given.split()
        )

    def parse_incoming(self, headers: Mapping[str, str], body: bytes) -> Optional[IncomingSession]:
        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"unreadable webhook: {exc}") from exc
        if not isinstance(event, dict) or event.get("type") not in INCOMING_EVENTS:
            return None
        data = event.get("data") or {}
        if event["type"] == "live.transport.incoming" and data.get("type") != "sip":
            return None
        session_id = str(data.get("session_id") or "")
        if not session_id:
            raise ValueError("incoming session has no session_id")
        sip_headers = {
            str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in data.get("sip_headers") or []
            if isinstance(h, dict)
        }
        event_id = {k.lower(): v for k, v in headers.items()}.get("webhook-id") or str(event.get("id", ""))
        return IncomingSession(session_id=session_id, event_id=event_id, headers=sip_headers)

    def _post(self, path: str, payload: Optional[dict]) -> tuple[int, bytes]:
        try:
            return self._transport(
                "POST",
                f"{self._base}/live/sessions/{path}",
                {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                json.dumps(payload).encode("utf-8") if payload is not None else None,
            )
        except OSError as exc:
            raise ProviderError(f"OpenAI could not be reached: {exc}") from exc

    def accept(self, session_id: str, request: VoiceSessionRequest) -> None:
        instructions = request.instructions
        if request.language:
            instructions = f"{instructions}\n\nSpeak in this language: {request.language}."
        session: dict = {
            "type": "live",
            "model": self._model,
            "instructions": instructions,
            "audio": {"output": {"voice": request.voice or DEFAULT_VOICE}},
        }
        if request.delegation:
            session["delegation"] = {"type": "client"}
        status, body = self._post(f"{session_id}/accept", {"session": session})
        if not 200 <= status < 300:
            raise ProviderError(f"OpenAI refused the session ({status}): {body[:200]!r}")

    def attach(self, session_id: str, hooks: CallHooks) -> None:
        """Follow an accepted session on a background thread: answer its delegations and
        report what is said to ``hooks.heard``.

        The connection opens on that thread, not here: this runs inside a webhook handler,
        and a slow vendor handshake must not hold up other calls' webhooks. If it cannot
        open, the call continues on its instructions alone and the failure is logged.
        """
        base = self._base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        url = f"{base}/live/sessions/{session_id}/attach"
        headers = {"Authorization": f"Bearer {self._key}"}

        def serve() -> None:
            try:
                channel = self._connector(url, headers)
            except Exception as exc:  # noqa: BLE001 — no sideband means no lookups, not no call
                log.warning("live sideband for %s did not open: %s", session_id, exc)
                return
            try:
                LiveSideband(channel, hooks.answer, hooks.heard).run()
            finally:
                channel.close()

        threading.Thread(target=serve, name=f"live-sideband-{session_id}", daemon=True).start()

    def reject(self, session_id: str) -> None:
        # 603 Decline. A competing decision already made is fine: either way, not connected.
        # Unreachable is fine too: a session nobody accepts is never connected, so a failed
        # reject only means OpenAI times it out instead of hearing no.
        try:
            self._post(f"{session_id}/reject", {"status_code": 603})
        except ProviderError:
            pass

    def hang_up(self, session_id: str) -> None:
        status, body = self._post(f"{session_id}/hangup", None)
        if not (200 <= status < 300 or status == 404):
            raise ProviderError(f"OpenAI could not end the session ({status}): {body[:200]!r}")
