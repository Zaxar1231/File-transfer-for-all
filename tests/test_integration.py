"""Against real servers from docker-compose.yml: OpenSSH, vsftpd, Apache WebDAV, Samba, S3.

The unit tests use in-process servers; these catch what only real ones do
(OpenSSH's SFTP quirks, vsftpd's passive mode, Apache's PROPFIND answers…).
Run with ``docker compose up -d --wait`` and ``FTA_IT=1``.
"""

from __future__ import annotations

import os
import time

import pytest

from fta.core.registry import open_transport
from test_protocols import resume_and_verify, roundtrip_tree

pytestmark = pytest.mark.skipif(not os.environ.get("FTA_IT"), reason="set FTA_IT=1 with docker compose up")

SECRET = {"password": "secret"}
SERVERS = {
    "sftp": ("sftp://fta@127.0.0.1:2222/upload", {**SECRET, "accept_unknown_host": "true"}),
    "ftp": ("ftp://fta@127.0.0.1:2121/home/fta", SECRET),
    "webdav": ("dav://fta@127.0.0.1:8080", SECRET),
    "smb": ("smb://fta@127.0.0.1:1445/share", SECRET),
    "s3": (
        "s3://fta-bucket",
        {"endpoint_url": "http://127.0.0.1:9090", "access_key": "x", "secret_key": "x", "region": "us-east-1"},
    ),
}


def _wait(url: str, options: dict) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            with open_transport(url, **options) as transport:
                transport.listdir(transport.endpoint.path or "/")
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest.mark.parametrize("name", list(SERVERS))
def test_real_server_roundtrip_and_resume(name, tmp_path):
    url, options = SERVERS[name]
    _wait(url, options)
    base = f"{url}/run-{os.getpid()}-{int(time.time())}"
    with open_transport(url, **options) as transport:
        transport.mkdir(transport.endpoint.path.rstrip("/") + base[len(url) :], exist_ok=True)
    work = tmp_path / "work"
    work.mkdir()
    roundtrip_tree(base, work, options)
    resume_and_verify(base, work, options, upload=name in ("sftp", "ftp", "smb"))  # S3 and WebDAV resume downloads only


def test_delta_sync_against_openssh(tmp_path):
    from fta.core.integrity import BLOCK_SIZE
    from fta.core.sync import sync

    url, options = SERVERS["sftp"]
    _wait(url, options)
    source = tmp_path / "src"
    source.mkdir()
    big = source / "disk.img"
    big.write_bytes(os.urandom(3 * BLOCK_SIZE))
    target = f"{url}/sync-{int(time.time())}"
    manifest = tmp_path / "m.json"
    sync(str(source), target, manifest=manifest, destination_options=options)
    data = bytearray(big.read_bytes())
    data[5] ^= 0xFF
    big.write_bytes(bytes(data))
    os.utime(big, (time.time() + 5, time.time() + 5))
    report = sync(str(source), target, manifest=manifest, destination_options=options)
    assert report.patched == ["disk.img"] and report.bytes_sent == BLOCK_SIZE
    back = tmp_path / "back.img"
    with open_transport(target, **options) as transport:
        transport.download(transport.endpoint.path + "/disk.img", back)
    assert back.read_bytes() == bytes(data)
