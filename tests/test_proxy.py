"""Connections through a SOCKS proxy (as through Tor): SFTP, WebDAV/HTTP and the SSH terminal."""

from __future__ import annotations

import pytest

pytest.importorskip("socks")

from fta.core.errors import TransferError  # noqa: E402
from fta.core.proxy import Proxy  # noqa: E402
from socks_server import SocksServer  # noqa: E402
from test_protocols import dav_port, roundtrip_tree, sftp_url  # noqa: E402,F401 - fixtures


@pytest.fixture
def socks_proxy():
    server = SocksServer()
    yield server
    server.close()


def test_sftp_through_the_proxy(tmp_path, sftp_url, socks_proxy):  # noqa: F811
    options = {"password": "secret", "accept_unknown_host": "true", "proxy": socks_proxy.url}
    roundtrip_tree(sftp_url, tmp_path, options)
    assert socks_proxy.connections and all(host == "127.0.0.1" for host, _ in socks_proxy.connections)


def test_webdav_through_the_proxy_with_names_resolved_by_it(tmp_path, dav_port, socks_proxy):  # noqa: F811
    # "localhost" reaches the proxy as a name, as an .onion address would reach Tor.
    roundtrip_tree(f"dav://localhost:{dav_port}", tmp_path, {"proxy": socks_proxy.url})
    assert ("localhost", dav_port) in socks_proxy.connections


def test_bad_proxies_are_clear_errors():
    with pytest.raises(TransferError, match="socks5h"):
        Proxy.parse("http://127.0.0.1:8080")
    dead = Proxy.parse("socks5h://127.0.0.1:1")
    with pytest.raises(TransferError, match="via proxy"):
        dead.connect("example.onion", 22, timeout=3)
