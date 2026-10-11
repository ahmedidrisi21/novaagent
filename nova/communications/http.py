"""The one place adapters touch the network, so a test can stand in for every vendor.

An adapter takes a ``Transport`` — ``(method, url, headers, body) -> (status, body)`` — and
defaults to :func:`urllib_transport`. Contract tests pass a scripted transport that answers
as the vendor would; nothing in the suite opens a socket to a vendor.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Callable, Mapping, Optional

Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], tuple[int, bytes]]

#: Seconds. A carrier that has not answered a dial request by then is treated as down.
TIMEOUT = 15


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
