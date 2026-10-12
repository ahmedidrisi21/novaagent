"""Ed25519 signature checks, borrowed from the runtime's own ``cryptography`` dependency.

Telnyx signs its webhooks with Ed25519 under a per-account public key; the standard
library has no Ed25519. This is the only module in the package allowed to import
``cryptography``, and only lazily, like :mod:`nova.communications.ws` borrows
``websockets``: the platform layer still installs and imports with the standard library
and PyYAML alone. Without the package, verification answers False — a webhook nobody
can verify is a webhook nobody accepts — and :func:`available` says why.
"""

from __future__ import annotations

import base64
import binascii

INSTALL_HINT = "install the runtime's 'cryptography' package (pip install 'cryptography>=50,<51')"


def available() -> str:
    """'' when signatures can be checked here; otherwise what to install."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return f"Ed25519 webhook signatures need cryptography: {INSTALL_HINT}"
    return ""


def verify(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    """True only for a valid signature over ``message`` by the base64 raw 32-byte key."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), message)
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True
