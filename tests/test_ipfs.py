"""IPFS through an HTTP gateway (with Range resume) and through a local node's API.

Both servers are small in-process stand-ins that answer the endpoints the
transport uses, so the test runs without the network or an IPFS daemon.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from fta.core.errors import NotFound, UnsupportedOperation
from fta.core.manager import copy
from fta.core.registry import open_transport
from fta.transports import ipfs

CID = "bafybeigdyrztktest"
FILES = {"/readme.txt": b"hello from ipfs", "/media/clip.bin": os.urandom(300_000)}


class FakeIpfs(BaseHTTPRequestHandler):
    """Gateway (GET/HEAD /ipfs/...) and Kubo API (POST /api/v0/...) in one."""

    added: dict[str, bytes] = {}

    def log_message(self, *args) -> None:
        pass

    def _path(self, value: str) -> str | None:
        prefix = f"/ipfs/{CID}"
        return unquote(value)[len(prefix) :] or "/" if unquote(value).startswith(prefix) else None

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self._path(urlsplit(self.path).path)
        data = FILES.get(path or "")
        if data is None:
            self._send(404)
            return
        start = 0
        if (ranged := self.headers.get("Range", "")).startswith("bytes="):
            start = int(ranged[6:].split("-")[0])
            self._send(206, data[start:], {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        else:
            self._send(200, data)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        command = url.path.removeprefix("/api/v0/")
        if command == "version":
            self._send(200, json.dumps({"Version": "0.32.1"}).encode())
        elif command == "add":
            body = self.rfile.read(int(self.headers["Content-Length"]))
            content = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            cid = "bafk" + hashlib.sha256(content).hexdigest()[:20]
            FakeIpfs.added[cid] = content
            self._send(200, json.dumps({"Name": "x", "Hash": cid, "Size": str(len(content))}).encode() + b"\n")
        elif command == "ls":
            path = self._path(query["arg"])
            names = sorted({p[len(path.rstrip("/")) + 1 :].split("/")[0] for p in FILES if p.startswith(path.rstrip("/") + "/")})
            links = [
                {"Name": n, "Type": 2 if f"{path.rstrip('/')}/{n}" in FILES else 1, "Size": len(FILES.get(f"{path.rstrip('/')}/{n}", b""))}
                for n in names
            ]
            self._send(200, json.dumps({"Objects": [{"Links": links}]}).encode())
        elif command == "files/stat":
            path = self._path(query["arg"])
            if path in FILES:
                self._send(200, json.dumps({"Type": "file", "Size": len(FILES[path])}).encode())
            elif any(p.startswith(path.rstrip("/") + "/") for p in FILES):
                self._send(200, json.dumps({"Type": "directory", "Size": 0}).encode())
            else:
                self._send(500, b'{"Message":"no link named"}')
        elif command == "cat":
            data = FILES[self._path(query["arg"])]
            self._send(200, data[int(query.get("offset", 0)) :])
        else:
            self._send(404)


@pytest.fixture
def fake_ipfs():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIpfs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_gateway_download_with_resume(tmp_path, fake_ipfs):
    options = {"gateway": fake_ipfs, "api": "http://127.0.0.1:1"}  # no local node
    copy(f"ipfs://{CID}/readme.txt", str(tmp_path / "readme.txt"), source_options=options)
    assert (tmp_path / "readme.txt").read_bytes() == FILES["/readme.txt"]

    clip = FILES["/media/clip.bin"]
    (tmp_path / "clip.bin").write_bytes(clip[:100_000])
    [result] = copy(f"ipfs://{CID}/media/clip.bin", str(tmp_path), resume=True, source_options=options)
    assert result.resumed_from == 100_000 and (tmp_path / "clip.bin").read_bytes() == clip
    with open_transport(f"ipfs://{CID}/", **options) as transport:
        with pytest.raises(UnsupportedOperation, match="local IPFS node"):
            transport.listdir("/")
        with pytest.raises(NotFound):
            transport.stat("/missing")


def test_local_node_lists_downloads_and_publishes(tmp_path, fake_ipfs):
    options = {"api": fake_ipfs, "gateway": "http://127.0.0.1:1"}  # everything through the node
    with open_transport(f"ipfs://{CID}/", **options) as transport:
        assert {e.name: e.is_dir for e in transport.listdir("/")} == {"media": True, "readme.txt": False}
        assert transport.stat("/media/clip.bin").size == 300_000
    copy(f"ipfs://{CID}/media", str(tmp_path), recursive=True, source_options=options)
    assert (tmp_path / "media" / "clip.bin").read_bytes() == FILES["/media/clip.bin"]

    mine = tmp_path / "отчёт.pdf"
    mine.write_bytes(os.urandom(70_000))
    cid = ipfs.add(mine, {"api": fake_ipfs})
    assert FakeIpfs.added[cid] == mine.read_bytes()
