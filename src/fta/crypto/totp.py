"""Time-based one-time codes (RFC 6238), as Google Authenticator, Aegis and 1Password make them."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
ISSUER = "File Transfer for All"


def new_secret() -> str:
    """160 random bits in base32, the form authenticator apps take."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def code(secret: str, at: float | None = None) -> str:
    counter = int((time.time() if at is None else at) // PERIOD)
    return _hotp(secret, counter)


def provisioning_uri(secret: str, account: str) -> str:
    """``otpauth://`` link for a QR code the authenticator app scans."""
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={PERIOD}"


class Verifier:
    """Accepts the current code and one step either side (clock drift), each code once only."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self._used: set[int] = set()
        self._lock = threading.Lock()

    def check(self, candidate: str, at: float | None = None) -> bool:
        candidate = candidate.strip().replace(" ", "")
        if len(candidate) != DIGITS or not candidate.isdigit():
            return False
        now = int((time.time() if at is None else at) // PERIOD)
        for counter in (now - 1, now, now + 1):
            if hmac.compare_digest(_hotp(self.secret, counter), candidate):
                with self._lock:
                    if counter in self._used:
                        return False  # a code seen over someone's shoulder cannot be replayed
                    self._used = {c for c in self._used if c >= now - 2} | {counter}
                return True
        return False


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)
