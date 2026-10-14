"""WebSockets, borrowed from the runtime's own ``websockets`` dependency.

The only module in this package allowed to import it, and only lazily: the platform layer
must install and import with the standard library and PyYAML alone. A deployment that
runs calls has the runtime, and the runtime pins ``websockets``; one that does not run
calls never reaches this file. ``tests/platform/test_boundaries.py`` holds the exemption
and the test proving that, without the package, everything still imports and the refusal
names what to install.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Union

from nova.communications.contracts import MediaChannel

INSTALL_HINT = "install the runtime's 'websockets' package (pip install 'websockets>=15,<16')"


class WebSocketUnavailable(RuntimeError):
    """The ``websockets`` package is not installed, so a live connection cannot open."""


class _Channel(MediaChannel):
    """A ``websockets`` sync connection behind the :class:`MediaChannel` contract."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def send(self, message: str | bytes) -> None:
        self._connection.send(message)

    def recv(self, timeout: Optional[float] = None) -> str | bytes:
        from websockets.exceptions import ConnectionClosed

        try:
            return self._connection.recv(timeout=timeout)
        except ConnectionClosed as exc:
            raise EOFError(str(exc)) from exc

    def close(self) -> None:
        self._connection.close()

    @property
    def path(self) -> str:
        request = getattr(self._connection, "request", None)
        return getattr(request, "path", "") if request is not None else ""


def connect(url: str, headers: Mapping[str, str]) -> MediaChannel:
    """Open a client connection. Raises :class:`WebSocketUnavailable` without the package."""
    try:
        from websockets.sync.client import connect as ws_connect
    except ImportError as exc:
        raise WebSocketUnavailable(f"live connections need WebSockets: {INSTALL_HINT}") from exc
    return _Channel(ws_connect(url, additional_headers=dict(headers), open_timeout=10))


def serve(handler: Callable[[MediaChannel, str], None], *, host: str, port: int):
    """A threaded WebSocket server calling ``handler(channel, path)`` per connection."""
    try:
        from websockets.sync.server import serve as ws_serve
    except ImportError as exc:
        raise WebSocketUnavailable(f"the media bridge needs WebSockets: {INSTALL_HINT}") from exc

    def on_connection(connection) -> None:
        channel = _Channel(connection)
        handler(channel, channel.path)

    return ws_serve(on_connection, host, port, max_size=256 * 1024)
