"""End-to-end tests against real servers started in-process on localhost.

Each protocol test is skipped when its server or client library is missing;
`pip install -e ".[dev]"` installs all of them.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from fta import Endpoint, NotFound, copy, open_transport


def roundtrip_tree(base_url: str, tmp_path, options: dict[str, str]) -> None:
    """Upload a tree, inspect it remotely, download it back, delete a file."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"alpha")
    payload = os.urandom(300_000)  # several protocol chunks
    (src / "sub" / "b.bin").write_bytes(payload)

    copy(str(src), base_url + "/", recursive=True, destination_options=options)

    remote_dir = Endpoint.parse(base_url + "/src").path
    with open_transport(base_url, **options) as transport:
        entries = {entry.name: entry for entry in transport.listdir(remote_dir)}
        assert entries["sub"].is_dir
        assert entries["a.txt"].size == 5
        assert transport.stat(remote_dir).is_dir
        assert transport.stat(remote_dir + "/sub/b.bin").size == len(payload)

    out = tmp_path / "out"
    out.mkdir()
    copy(base_url + "/src", str(out), recursive=True, source_options=options)
    assert (out / "src" / "a.txt").read_bytes() == b"alpha"
    assert (out / "src" / "sub" / "b.bin").read_bytes() == payload

    with open_transport(base_url, **options) as transport:
        transport.delete(remote_dir + "/a.txt")
        with pytest.raises(NotFound):
            transport.stat(remote_dir + "/a.txt")


@pytest.fixture
def ftp_url(tmp_path):
    pytest.importorskip("pyftpdlib")
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "ftp-root"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "secret", str(root), perm="elradfmwMT")
    handler = type("Handler", (FTPHandler,), {"authorizer": authorizer})
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1, "handle_exit": False}, daemon=True)
    thread.start()
    yield f"ftp://user@127.0.0.1:{server.address[1]}"
    server.close_all()
    thread.join(5)


@pytest.fixture
def dav_port(tmp_path):
    pytest.importorskip("wsgidav")
    pytest.importorskip("cheroot")
    from cheroot import wsgi
    from wsgidav.wsgidav_app import WsgiDAVApp

    root = tmp_path / "dav-root"
    root.mkdir()
    app = WsgiDAVApp(
        {
            "provider_mapping": {"/": str(root)},
            "simple_dc": {"user_mapping": {"*": True}},
            "verbose": 0,
            "logging": {"enable": False},
        }
    )
    # wsgiref's server stalls on WsgiDAV PUT bodies; cheroot is the server WsgiDAV ships with
    server = wsgi.Server(("127.0.0.1", 0), app)
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    yield server.bind_addr[1]
    server.stop()
    thread.join(5)


@pytest.fixture
def sftp_url(tmp_path):
    asyncssh = pytest.importorskip("asyncssh")
    pytest.importorskip("paramiko")

    root = tmp_path / "sftp-root"
    root.mkdir()

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "user" and password == "secret"

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            server_factory=Server,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)),
        )

    server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield f"sftp://user@127.0.0.1:{server.get_port()}"
    server.close()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


@pytest.fixture
def s3_url(monkeypatch):
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="fta-test")
        yield "s3://fta-test"


def test_ftp(ftp_url, tmp_path):
    roundtrip_tree(ftp_url, tmp_path, {"password": "secret"})


def test_webdav(dav_port, tmp_path):
    roundtrip_tree(f"dav://127.0.0.1:{dav_port}", tmp_path, {})


def test_sftp(sftp_url, tmp_path):
    roundtrip_tree(sftp_url, tmp_path, {"password": "secret", "accept_unknown_host": "true"})


def test_s3(s3_url, tmp_path):
    roundtrip_tree(s3_url, tmp_path, {})


def test_http_put_head_get_delete(dav_port, tmp_path):
    url = f"http://127.0.0.1:{dav_port}/hello.txt"
    src = tmp_path / "hello.txt"
    src.write_bytes(b"hello http")
    events = []

    copy(str(src), url, progress=lambda done, total: events.append((done, total)))
    assert events[-1] == (10, 10)

    with open_transport(url) as transport:
        assert transport.stat("/hello.txt").size == 10

    out = tmp_path / "out.txt"
    copy(url, str(out))
    assert out.read_bytes() == b"hello http"

    with open_transport(url) as transport:
        transport.delete("/hello.txt")
        with pytest.raises(NotFound):
            transport.stat("/hello.txt")


def test_ftp_to_s3_is_staged_through_local_disk(ftp_url, s3_url, tmp_path):
    src = tmp_path / "bridge.txt"
    src.write_bytes(b"across protocols")

    copy(str(src), ftp_url + "/", destination_options={"password": "secret"})
    copy(ftp_url + "/bridge.txt", s3_url + "/in/", source_options={"password": "secret"})

    with open_transport(s3_url) as transport:
        assert transport.stat("/in/bridge.txt").size == len(b"across protocols")
