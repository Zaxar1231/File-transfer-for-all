"""SOCKS proxies (Tor first of all) for the protocols that can use them: SFTP, HTTP(S), WebDAV.

``proxy=socks5h://127.0.0.1:9050`` sends the connection through Tor; the ``h``
means the proxy resolves host names, which is what makes ``.onion`` addresses
work and keeps DNS lookups from leaking past Tor.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from fta.core.errors import TransferError

TOR = "socks5h://127.0.0.1:9050"
"""Tor's SOCKS port on a computer running Tor (Tor Browser uses 9150)."""


@dataclass(frozen=True)
class Proxy:
    kind: int
    host: str
    port: int
    remote_dns: bool
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, url: str) -> Proxy:
        import socks

        parts = urlsplit(url)
        kinds = {"socks5": socks.SOCKS5, "socks5h": socks.SOCKS5, "socks4": socks.SOCKS4, "socks4a": socks.SOCKS4}
        if parts.scheme not in kinds or not parts.hostname:
            raise TransferError(f"unsupported proxy {url!r}; use socks5h://host:port (Tor: {TOR})")
        return cls(
            kinds[parts.scheme],
            parts.hostname,
            parts.port or 1080,
            parts.scheme in ("socks5h", "socks4a"),
            unquote(parts.username) if parts.username else None,
            unquote(parts.password) if parts.password else None,
        )

    def connect(self, host: str, port: int, timeout: float | None = 30) -> socket.socket:
        import socks

        sock = socks.socksocket()
        sock.set_proxy(self.kind, self.host, self.port, rdns=self.remote_dns, username=self.username, password=self.password)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except (socks.ProxyError, OSError) as exc:
            sock.close()
            raise TransferError(f"via proxy {self.host}:{self.port}: cannot reach {host}:{port}: {exc}") from exc
        return sock


def opener(proxy_url: str) -> urllib.request.OpenerDirector:
    """A urllib opener whose HTTP and HTTPS connections go through the proxy."""
    proxy = Proxy.parse(proxy_url)

    class Http(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = proxy.connect(self.host, self.port, self.timeout)

    class Https(http.client.HTTPSConnection):
        def connect(self) -> None:
            raw = proxy.connect(self.host, self.port, self.timeout)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)

    class HttpHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001 - urllib's signature
            return self.do_open(Http, req)

    class HttpsHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001
            return self.do_open(Https, req, context=ssl.create_default_context())

    return urllib.request.build_opener(HttpHandler, HttpsHandler)
