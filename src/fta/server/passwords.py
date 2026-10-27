"""Password hashes for the built-in server: PBKDF2-HMAC-SHA256, never the password itself."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
