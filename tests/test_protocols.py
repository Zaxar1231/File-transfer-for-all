"""End-to-end tests against real servers started in-process on localhost.

Each protocol test is skipped when its server or client library is missing;
`pip install -e ".[dev]"` installs all of them.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

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

    async def stop():  # asyncio objects must be closed from their own loop's thread
        server.close()
        await server.wait_closed()

    asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


SAMBA_CONF = """[global]
server role = standalone server
smb ports = {port}
interfaces = 127.0.0.1
bind interfaces only = yes
lock directory = {base}/lock
state directory = {base}/state
cache directory = {base}/cache
private dir = {base}/private
pid directory = {base}/pid
ncalrpc dir = {base}/ncalrpc
log file = {base}/log/log.%m
passdb backend = tdbsam:{base}/private/passdb.tdb
load printers = no
printcap name = /dev/null
disable spoolss = yes

[public]
path = {share}
read only = no
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def smb_url():
    """A real Samba server on a random port, run as root (directly or via passwordless sudo)."""
    pytest.importorskip("smbclient")
    smbd = shutil.which("smbd") or shutil.which("smbd", path="/usr/sbin")
    smbpasswd = shutil.which("smbpasswd") or shutil.which("smbpasswd", path="/usr/bin:/usr/sbin")
    if not smbd or not smbpasswd:
        pytest.skip("Samba is not installed (apt-get install samba)")
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    if sudo and subprocess.run([*sudo, "true"], capture_output=True).returncode != 0:
        pytest.skip("smbd needs root or passwordless sudo")

    # Samba keeps Unix sockets in these directories, so the path must stay short.
    base = tempfile.mkdtemp(prefix="fta-smb-")
    share = os.path.join(base, "share")
    os.makedirs(share)
    for name in ("lock", "state", "cache", "private", "pid", "log"):
        os.makedirs(os.path.join(base, name))
    port = _free_port()
    conf = os.path.join(base, "smb.conf")
    with open(conf, "w") as fp:
        fp.write(SAMBA_CONF.format(port=port, base=base, share=share))
    user = getpass.getuser()
    subprocess.run(
        [*sudo, smbpasswd, "-c", conf, "-a", "-s", user], input="secret\nsecret\n", text=True, check=True, capture_output=True
    )
    server = subprocess.Popen(
        [*sudo, smbd, "--foreground", "--no-process-group", f"--configfile={conf}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # smbd signals its whole process group on shutdown; keep pytest out of it
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"smbd did not start (exit code {server.poll()})")
            time.sleep(0.1)

    yield f"smb://{user}@127.0.0.1:{port}/public"

    server.terminate()
    try:
        server.wait(10)
    except subprocess.TimeoutExpired:
        server.kill()
    subprocess.run([*sudo, "rm", "-rf", base], capture_output=True)


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


def test_ftp_download_knows_the_total_size(ftp_url, tmp_path):
    src = tmp_path / "sized.bin"
    src.write_bytes(os.urandom(200_000))
    copy(str(src), ftp_url + "/", destination_options={"password": "secret"})
    totals = set()

    copy(
        ftp_url + "/sized.bin",
        str(tmp_path / "back.bin"),
        progress=lambda done, total: totals.add(total),
        source_options={"password": "secret"},
    )

    assert totals == {200_000}


def test_webdav(dav_port, tmp_path):
    roundtrip_tree(f"dav://127.0.0.1:{dav_port}", tmp_path, {})


def test_sftp(sftp_url, tmp_path):
    roundtrip_tree(sftp_url, tmp_path, {"password": "secret", "accept_unknown_host": "true"})


def test_s3(s3_url, tmp_path):
    roundtrip_tree(s3_url, tmp_path, {})


def test_smb(smb_url, tmp_path):
    roundtrip_tree(smb_url, tmp_path, {"password": "secret"})


def test_smb_reports_wrong_password_and_missing_share(smb_url):
    from fta import TransferError

    with pytest.raises(TransferError, match="cannot connect"):
        with open_transport(smb_url, password="wrong"):
            pass
    with open_transport(smb_url, password="secret") as transport:
        with pytest.raises(TransferError, match="need a share"):
            transport.listdir("/")


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
