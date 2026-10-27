"""``fta://`` links that a QR code carries: where a device listens and its certificate fingerprint."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

DEFAULT_PORT = 53318


@dataclass(frozen=True)
class PeerLink:
    host: str
    port: int = DEFAULT_PORT
    fingerprint: str = ""
    name: str = ""

    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        query = f"fp={self.fingerprint}" + (f"&name={quote(self.name)}" if self.name else "")
        return f"fta://{host}:{self.port}/?{query}"


def local_address() -> str:
    """The address other devices on the network reach this one at; loopback when offline."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def looks_like_address(text: str) -> bool:
    """``fta://…``, an IP address or ``host:port``, as opposed to a device name."""
    if "://" in text:
        return True
    host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
    if port and not port.isdigit():
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(port) or host.endswith(".local")


def parse_link(text: str) -> PeerLink:
    """``fta://host:port/?fp=…&name=…``, or a bare ``host[:port]`` typed by hand."""
    text = text.strip()
    if "://" not in text:
        text = "fta://" + text
    parts = urlsplit(text)
    if parts.scheme != "fta" or not parts.hostname:
        raise ValueError(f"not a device link: {text}")
    query = parse_qs(parts.query)
    fingerprint = query.get("fp", [""])[0].lower()
    if fingerprint and (len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise ValueError("the fingerprint in the link is damaged")
    return PeerLink(parts.hostname, parts.port or DEFAULT_PORT, fingerprint, query.get("name", [""])[0])


def qr_png(text: str, scale: int = 8) -> bytes:
    """A QR code of ``text`` as PNG bytes, ready for ``ft.Image(src=...)``."""
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="png", scale=scale, border=2)
    return buffer.getvalue()


def qr_text(text: str) -> str:
    """The same QR code drawn with block characters, for a terminal."""
    import io

    import segno

    buffer = io.StringIO()
    segno.make(text, error="m").terminal(out=buffer, compact=True)
    return buffer.getvalue()
