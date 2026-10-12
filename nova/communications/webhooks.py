"""The public HTTPS endpoint carriers and voice engines call back on.

Deliberately not the Control API. That server holds authenticated operator sessions and
must stay private; this one is reached by vendors over the internet and authenticates
every request by its signature instead. Putting both behind one listener would put the
operator surface on the internet.

Eight routes, POST, bodies capped at 64 KiB:

    /calls/<carrier>/events        every event, for a carrier that sends all to one webhook
    /calls/<carrier>/answer        the carrier asks what to do with an answered call
    /calls/<carrier>/consent       the person's answer to "may we record?"
    /calls/<carrier>/recording     a recording started, finished, or captured nothing
    /calls/<carrier>/status        call progress
    /voice/<engine>/events         a voice engine announces a bridged session
    /email/<provider>/events       delivery, bounce and complaint webhooks
    /email/<provider>/unsubscribe  the link in every email (one-click POST)

The unsubscribe link alone also answers GET, with a page asking to confirm: a person
clicks it in a browser, and a mail scanner fetching it must not unsubscribe anyone.

``public_url`` is the URL vendors are configured with. Signatures cover the exact URL the
vendor called, and behind a TLS-terminating proxy the request line alone does not say what
that was, so the configured value is the one signatures are checked against.
"""

from __future__ import annotations

import http.server
import threading

from nova.audit import new_correlation_id
from nova.communications.gateway import CallGateway

MAX_BODY_BYTES = 64 * 1024


def _answer(gateway, name, headers, body, url):
    answer = gateway.answer(name, headers=headers, body=body, url=url)
    return answer.http_status, answer.content_type, answer.body


def _consent(gateway, name, headers, body, url):
    from nova.communications.recording import consent_reply

    answer = consent_reply(gateway, name, headers=headers, body=body, url=url)
    return answer.http_status, answer.content_type, answer.body


def _plain(result):
    return result.http_status, "text/plain", result.outcome.encode("utf-8")


def _recording(gateway, name, headers, body, url):
    from nova.communications.recording import recording_event

    return _plain(recording_event(gateway, name, headers=headers, body=body, url=url))


def _status(gateway, name, headers, body, url):
    return _plain(gateway.ingest(name, headers=headers, body=body, url=url))


def _voice(gateway, name, headers, body, url):
    return _plain(gateway.voice_event(name, headers=headers, body=body))


def _carrier_events(gateway, name, headers, body, url):
    """One webhook for every event of a call (Telnyx). Verified here first — a ``continue``
    runs the carrier's own next step, so nothing unsigned may reach it — then handed to the
    handler for what the carrier says the event is."""
    carrier = gateway.carriers.get(name)
    if carrier is None:
        return 404, "text/plain", b"no such carrier"
    if not carrier.verify(headers, body, url):
        return _plain(gateway.ingest(name, headers=headers, body=body, url=url))  # audited 403
    try:
        kind = carrier.event_kind(headers, body)
    except ValueError:
        return 400, "text/plain", b"rejected"
    if kind == "continue":
        from nova.communications.contracts import ProviderError

        try:
            carrier.continue_plan(headers, body)
        except ProviderError as exc:
            gateway.audit.record("communications.call.command_failed",
                                 correlation_id=new_correlation_id(), subject=name,
                                 detail={"carrier": name}, error=str(exc))
        return 200, "text/plain", b"ok"
    handler = _EVENT_HANDLERS.get(kind)
    if handler is None:
        return 200, "text/plain", b"ignored"
    return handler(gateway, name, headers, body, url)


def _email_events(gateway, name, headers, body, url):
    from nova.communications.followup_email import ingest

    status, outcome = ingest(gateway, name, headers, body)
    return status, "text/plain", outcome.encode("utf-8")


def _unsubscribe(method: str):
    def handler(gateway, name, headers, body, url):
        from nova.communications.followup_email import unsubscribe

        return unsubscribe(gateway, name, url, method=method)

    return handler


#: ``/<area>/<provider>/<what>`` -> handler. Every route is enumerated here; nothing is
#: matched by prefix.
_EVENT_HANDLERS = {
    "answer": _answer,
    "consent": _consent,
    "recording": _recording,
    "status": _status,
}

ROUTES = {
    ("calls", "events"): _carrier_events,
    ("calls", "answer"): _answer,
    ("calls", "consent"): _consent,
    ("calls", "recording"): _recording,
    ("calls", "status"): _status,
    ("voice", "events"): _voice,
    ("email", "events"): _email_events,
    ("email", "unsubscribe"): _unsubscribe("POST"),
}
GET_ROUTES = {
    ("email", "unsubscribe"): _unsubscribe("GET"),
}


def handle(
    gateway: CallGateway, public_url: str, path: str, headers: dict[str, str], body: bytes,
    *, method: str = "POST",
) -> tuple[int, str, bytes]:
    """Route one request. Pure enough to test without a socket: ``(status, type, body)``."""
    route = path.split("?", 1)[0].strip("/").split("/")
    table = GET_ROUTES if method == "GET" else ROUTES
    handler = table.get((route[0], route[2])) if len(route) == 3 else None
    if handler is None:
        return 404, "text/plain", b"no such route"
    return handler(gateway, route[1], headers, body, public_url.rstrip("/") + path)


def build_server(
    gateway: CallGateway, *, public_url: str, host: str = "127.0.0.1", port: int = 8090
) -> http.server.ThreadingHTTPServer:
    """A threaded listener. Bind it behind a TLS proxy; vendors require HTTPS."""
    if not public_url.startswith("https://"):
        raise ValueError("public_url must be the HTTPS address vendors call")
    # One request at a time against the store: SQLite serialises writes anyway, and the
    # dial claim and session claim rely on it.
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "callhooks"

        def _reply(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._reply(400, "text/plain", b"bad length")
                return
            if length > MAX_BODY_BYTES:
                self._reply(413, "text/plain", b"too large")
                return
            body = self.rfile.read(length)
            with lock:
                self._reply(*handle(gateway, public_url, self.path, dict(self.headers), body))

        def do_GET(self) -> None:  # noqa: N802
            with lock:
                self._reply(*handle(gateway, public_url, self.path, dict(self.headers), b"",
                                    method="GET"))

        def log_message(self, format: str, *args: object) -> None:
            # Query strings carry call ids; the gateway's audit log is the record.
            pass

    return http.server.ThreadingHTTPServer((host, port), Handler)

