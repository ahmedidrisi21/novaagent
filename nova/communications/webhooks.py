"""The public HTTPS endpoint carriers and voice engines call back on.

Deliberately not the Control API. That server holds authenticated operator sessions and
must stay private; this one is reached by vendors over the internet and authenticates
every request by its signature instead. Putting both behind one listener would put the
operator surface on the internet.

Three routes, POST only, bodies capped at 64 KiB:

    /calls/<carrier>/answer   the carrier asks what to do with an answered call
    /calls/<carrier>/status   call progress
    /voice/<engine>/events    a voice engine announces a bridged session

``public_url`` is the URL vendors are configured with. Signatures cover the exact URL the
vendor called, and behind a TLS-terminating proxy the request line alone does not say what
that was, so the configured value is the one signatures are checked against.
"""

from __future__ import annotations

import http.server
import threading

from nova.communications.gateway import CallGateway

MAX_BODY_BYTES = 64 * 1024


def handle(
    gateway: CallGateway, public_url: str, path: str, headers: dict[str, str], body: bytes
) -> tuple[int, str, bytes]:
    """Route one request. Pure enough to test without a socket: ``(status, type, body)``."""
    route = path.split("?", 1)[0].strip("/").split("/")
    url = public_url.rstrip("/") + path
    if len(route) == 3 and route[0] == "calls" and route[2] == "answer":
        answer = gateway.answer(route[1], headers=headers, body=body, url=url)
        return answer.http_status, answer.content_type, answer.body
    if len(route) == 3 and route[0] == "calls" and route[2] == "status":
        result = gateway.ingest(route[1], headers=headers, body=body, url=url)
        return result.http_status, "text/plain", result.outcome.encode("utf-8")
    if len(route) == 3 and route[0] == "voice" and route[2] == "events":
        result = gateway.voice_event(route[1], headers=headers, body=body)
        return result.http_status, "text/plain", result.outcome.encode("utf-8")
    return 404, "text/plain", b"no such route"


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
            self._reply(405, "text/plain", b"POST only")

        def log_message(self, format: str, *args: object) -> None:
            # Query strings carry call ids; the gateway's audit log is the record.
            pass

    return http.server.ThreadingHTTPServer((host, port), Handler)

