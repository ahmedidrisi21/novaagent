"""ElevenLabs Agents as a voice engine, over the media bridge.

The carrier streams the call to our bridge; this engine relays it to the tenant's
ElevenLabs agent over the Agents WebSocket and relays the agent's voice back. Both sides
speak 8 kHz mu-law, so audio passes through untouched — the agent must be set to
``ulaw_8000`` for input and output, and a call refuses to start (and fails over) if not.

What stays NOVA's on every call, whatever the hosted agent is configured to do:

* **The purpose's instructions** are sent as the conversation's prompt override, and the
  agent's voice as the TTS override. The agent's security settings must allow both
  overrides; ElevenLabs refuses the conversation otherwise, and the call fails over.
* **Knowledge** is reached through one client tool, ``lookup_knowledge(question)``, which
  the gateway answers with the same read-only, audited lookup GPT-Live uses. Any other
  client tool is refused: nothing said on a call makes the gateway act.
* **Spoken opt-outs** are caught in the caller's transcript as it arrives: the contact is
  suppressed at once, the agent is told to say goodbye, and the call ends.
* **The transcript** — both sides, turn by turn — feeds the gateway's redacting transcriber
  and the payment guard.

Vendor contract: https://elevenlabs.io/docs/agents-platform/api-reference/agents-platform/websocket
and ``GET /v1/convai/conversation/get-signed-url``. Network access goes through an
injectable connector and transport, so the tests drive the loop with a scripted agent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Callable, Mapping, Optional

from nova.communications.cascade import OPT_OUT, Connector
from nova.communications.contracts import (
    CallHooks,
    MediaChannel,
    ProviderError,
    StreamTarget,
    StreamVoiceEngine,
    VoiceSessionRequest,
)
from nova.communications.http import Transport, urllib_transport

log = logging.getLogger(__name__)

DEFAULT_HOST = "api.elevenlabs.io"
AUDIO_FORMAT = "ulaw_8000"
LOOKUP_TOOL = "lookup_knowledge"
#: Told to the agent when the caller asks not to be called again.
OPT_OUT_UPDATE = ("The caller has asked not to be called again. Confirm in one short sentence "
                  "that they will not be called again, say goodbye, and say nothing more.")
#: Seconds the agent gets to say goodbye before the call is ended anyway.
GOODBYE_SECONDS = 8.0
START_TIMEOUT = 10.0

CALL_RULES = (
    "You are on a live phone call; the caller has already heard that you are an AI. Keep "
    "replies short and spoken. For any fact about the company, call lookup_knowledge and "
    "answer only from what it returns; if it does not cover the question, say you don't "
    "have that and offer to follow up in writing. Never invent a fact."
)


def _connect(url: str, headers: Mapping[str, str]) -> MediaChannel:
    from nova.communications.ws import connect

    return connect(url, headers)


class ElevenLabsVoice(StreamVoiceEngine):
    id = "elevenlabs-agents"

    def __init__(self, *, api_key: str, agent_id: str, media_url: str, host: str = DEFAULT_HOST,
                 transport: Transport = urllib_transport,
                 connector: Connector = _connect) -> None:
        if not (api_key and agent_id):
            raise ValueError("ElevenLabs Agents needs an API key and an agent id")
        if not media_url.startswith("wss://"):
            raise ValueError(f"the media bridge URL must be public wss://, got {media_url!r}")
        self._key = api_key
        self._agent = agent_id
        self._media_url = media_url
        self._host = host or DEFAULT_HOST
        self._transport = transport
        self._connector = connector

    def bridge_target(self, call_id: str, token: str) -> StreamTarget:
        return StreamTarget(self._media_url, {"call_id": call_id, "token": token})

    def _signed_url(self) -> str:
        """A one-conversation URL, so the API key never travels on the WebSocket."""
        query = urllib.parse.urlencode({"agent_id": self._agent})
        try:
            status, body = self._transport(
                "GET", f"https://{self._host}/v1/convai/conversation/get-signed-url?{query}",
                {"xi-api-key": self._key}, None)
        except OSError as exc:
            raise ProviderError(f"ElevenLabs could not be reached: {exc}") from exc
        try:
            url = json.loads(body.decode("utf-8")).get("signed_url") if 200 <= status < 300 else ""
        except (ValueError, AttributeError):
            url = ""
        if not url or not str(url).startswith("wss://"):
            raise ProviderError(f"ElevenLabs gave no conversation URL ({status})")
        return str(url)

    def converse(self, media: MediaChannel, stream_sid: str, request: VoiceSessionRequest,
                 hooks: CallHooks) -> None:
        agent = self._connector(self._signed_url(), {})
        try:
            AgentCall(media=media, agent=agent, stream_sid=stream_sid, request=request,
                      hooks=hooks).run()
        finally:
            try:
                agent.close()
            except Exception:
                pass


def _initiation(request: VoiceSessionRequest) -> dict:
    agent: dict = {"prompt": {"prompt": f"{request.instructions}\n\n{CALL_RULES}".strip()}}
    if request.language:
        agent["language"] = request.language.split("-")[0]
    override: dict = {"agent": agent}
    if request.voice:
        override["tts"] = {"voice_id": request.voice}
    return {"type": "conversation_initiation_client_data",
            "conversation_config_override": override,
            "dynamic_variables": {"nova_call_id": request.call_id, "nova_agent": request.agent}}


class AgentCall:
    """One call relayed between the carrier's stream and the agent's WebSocket."""

    def __init__(self, *, media: MediaChannel, agent: MediaChannel, stream_sid: str,
                 request: VoiceSessionRequest, hooks: CallHooks,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._media, self._agent = media, agent
        self._sid = stream_sid
        self._request = request
        self._hooks = hooks
        self._clock = clock
        self._send_lock = threading.Lock()
        self._done = threading.Event()
        self._goodbye_by: Optional[float] = None
        self.opted_out = False

    def run(self) -> None:
        self._agent.send(json.dumps(_initiation(self._request)))
        self._await_metadata()
        reader = threading.Thread(target=self._from_agent, name="elevenlabs-agent", daemon=True)
        reader.start()
        try:
            self._from_caller()
        finally:
            self._done.set()
            try:
                self._agent.close()  # wakes the reader
            except Exception:
                pass
            reader.join(timeout=5)

    def _await_metadata(self) -> None:
        """The agent's audio formats must be ours, or the caller would hear noise."""
        deadline = self._clock() + START_TIMEOUT
        while self._clock() < deadline:
            try:
                message = json.loads(self._agent.recv(timeout=START_TIMEOUT))
            except (EOFError, TimeoutError, TypeError, ValueError) as exc:
                raise ProviderError(f"the ElevenLabs agent did not start: {exc}") from exc
            if message.get("type") == "ping":
                self._pong(message)
                continue
            if message.get("type") != "conversation_initiation_metadata":
                continue
            meta = message.get("conversation_initiation_metadata_event") or {}
            formats = (meta.get("user_input_audio_format"), meta.get("agent_output_audio_format"))
            if formats != (AUDIO_FORMAT, AUDIO_FORMAT):
                raise ProviderError(f"the ElevenLabs agent must use {AUDIO_FORMAT} for input and "
                                    f"output, not {formats[0]} / {formats[1]}")
            return
        raise ProviderError("the ElevenLabs agent did not start in time")

    # -- caller -> agent ------------------------------------------------------------------

    def _from_caller(self) -> None:
        while not self._done.is_set():
            if self._goodbye_by is not None and self._clock() >= self._goodbye_by:
                self._end()
                return
            try:
                raw = self._media.recv(timeout=0.5)
            except TimeoutError:
                continue
            except EOFError:
                return
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            event = message.get("event")
            if event == "media" and (message.get("media") or {}).get("track", "inbound") \
                    in ("inbound", "inbound_track"):
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    try:
                        self._agent.send(json.dumps({"user_audio_chunk": payload}))
                    except EOFError:
                        return
            elif event == "stop":
                return

    # -- agent -> caller ------------------------------------------------------------------

    def _from_agent(self) -> None:
        handlers = {
            "audio": self._audio,
            "interruption": lambda m: self._to_caller({"event": "clear", "streamSid": self._sid}),
            "ping": self._pong,
            "user_transcript": self._caller_said,
            "agent_response": self._agent_said,
            "client_tool_call": self._tool_call,
        }
        while not self._done.is_set():
            try:
                message = json.loads(self._agent.recv())
            except EOFError:
                return
            except (TypeError, ValueError):
                continue
            handler = handlers.get(message.get("type"))
            if handler is not None:
                try:
                    handler(message)
                except EOFError:
                    return

    def _to_caller(self, message: dict) -> None:
        with self._send_lock:
            self._media.send(json.dumps(message))

    def _to_agent(self, message: dict) -> None:
        with self._send_lock:
            self._agent.send(json.dumps(message))

    def _audio(self, message: dict) -> None:
        chunk = (message.get("audio_event") or {}).get("audio_base_64")
        if chunk:
            self._to_caller({"event": "media", "streamSid": self._sid, "media": {"payload": chunk}})

    def _pong(self, message: dict) -> None:
        event_id = (message.get("ping_event") or {}).get("event_id")
        self._to_agent({"type": "pong", "event_id": event_id})

    def _caller_said(self, message: dict) -> None:
        text = str((message.get("user_transcription_event") or {}).get("user_transcript") or "")
        if not text.strip():
            return
        if self._hooks.heard is not None:
            self._hooks.heard("caller", text)
        if self._hooks.said is not None:
            self._hooks.said("caller", text)
        if not self.opted_out and OPT_OUT.search(text):
            self.opted_out = True
            if self._hooks.opt_out is not None:
                self._hooks.opt_out(text)
            self._to_agent({"type": "contextual_update", "text": OPT_OUT_UPDATE})
            self._goodbye_by = self._clock() + GOODBYE_SECONDS

    def _agent_said(self, message: dict) -> None:
        text = str((message.get("agent_response_event") or {}).get("agent_response") or "")
        if not text.strip():
            return
        if self._hooks.heard is not None:
            self._hooks.heard("agent", text)
        if self._hooks.said is not None:
            self._hooks.said("agent", text)

    def _tool_call(self, message: dict) -> None:
        call = message.get("client_tool_call") or {}
        tool_call_id = call.get("tool_call_id")
        if call.get("tool_name") != LOOKUP_TOOL or self._hooks.answer is None:
            self._to_agent({"type": "client_tool_result", "tool_call_id": tool_call_id,
                            "result": "That tool is not available on this call.",
                            "is_error": True})
            return
        question = str((call.get("parameters") or {}).get("question") or "")
        try:
            answer = self._hooks.answer(question) if question else ""
        except Exception as exc:
            log.warning("knowledge lookup failed on an ElevenLabs call: %s", exc)
            answer = ""
        self._to_agent({"type": "client_tool_result", "tool_call_id": tool_call_id,
                        "result": answer or "Not found. Offer to follow up in writing.",
                        "is_error": False})

    def _end(self) -> None:
        self._done.set()
        if self._hooks.end_call is not None:
            self._hooks.end_call()

