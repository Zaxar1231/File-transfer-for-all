"""Cloud drives through rclone, against a real rclone with a local "cloud"."""

from __future__ import annotations

import hashlib
import io
import shutil
import zipfile

import pytest

from fta.core.errors import TransferError
from fta.tools import rclone
from test_protocols import resume_and_verify, roundtrip_tree

needs_rclone = pytest.mark.skipif(rclone.find() is None, reason="rclone is not installed")


@pytest.fixture
def app_rclone(tmp_path, monkeypatch):
    """The app's data folder with its own rclone.conf; the user's default config is a separate file."""
    monkeypatch.setattr(rclone, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "users-rclone.conf"))
    return tmp_path


@needs_rclone
def test_cloud_roundtrip_and_resume(app_rclone):
    cloud = app_rclone / "cloud"
    cloud.mkdir()
    # Mixed case on purpose: rclone remote names are case-sensitive, URL hosts are not.
    rclone.create_remote("MyCloud", "alias", {"remote": str(cloud)})
    assert "MyCloud" in rclone.app_remotes()

    work = app_rclone / "work"
    work.mkdir()
    roundtrip_tree("rclone://MyCloud", work, {})
    resume_and_verify("rclone://MyCloud", work, {}, upload=False)  # downloads continue with cat --offset
    assert (cloud / "src" / "sub" / "b.bin").exists()


@needs_rclone
def test_remotes_from_the_app_and_from_the_users_config(app_rclone):
    rclone.create_remote("mine", "alias", {"remote": str(app_rclone)})
    users = app_rclone / "users-rclone.conf"
    users.write_text(f"[old-drive]\ntype = alias\nremote = {app_rclone}\n")
    found = {remote.name: remote for remote in rclone.remotes()}
    assert set(found) == {"mine", "old-drive"}
    assert found["mine"].type == "alias"

    # A remote from the user's own config works without copying it anywhere.
    from fta.core.registry import open_transport

    (app_rclone / "hello.txt").write_text("hi")
    with open_transport("rclone://old-drive/") as transport:
        assert "hello.txt" in [entry.name for entry in transport.listdir("/")]

    rclone.delete_remote("mine")
    assert "mine" not in rclone.app_remotes()


@needs_rclone
def test_errors_are_readable(app_rclone):
    from fta.core.errors import NotFound
    from fta.core.registry import open_transport

    rclone.create_remote("c", "alias", {"remote": str(app_rclone / "nowhere")})
    with open_transport("rclone://c/") as transport:
        with pytest.raises(NotFound):
            transport.stat("/missing.txt")
    with pytest.raises(TransferError, match="rclone"):
        with open_transport("rclone://not-configured/") as transport:
            transport.listdir("/")


def test_install_checks_the_published_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(rclone, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rclone, "asset_name", lambda: "rclone-v1-linux-amd64.zip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(f"rclone-v1-linux-amd64/{rclone.binary_name()}", b"#!/bin/sh\necho rclone v1\n")
    archive = buffer.getvalue()
    sums = f"{hashlib.sha256(archive).hexdigest()}  rclone-v1-linux-amd64.zip\n".encode()

    def fetch(url):
        return sums if url.endswith("SHA256SUMS") else archive

    target = rclone.install(fetch=fetch)
    assert target == tmp_path / "bin" / rclone.binary_name() and target.read_bytes().startswith(b"#!/bin/sh")
    monkeypatch.delenv("FTA_RCLONE", raising=False)
    assert rclone.find() == str(target)

    target.unlink()
    with pytest.raises(TransferError, match="SHA-256"):
        rclone.install(fetch=lambda url: sums if url.endswith("SHA256SUMS") else archive + b"tampered")
    assert not target.exists()


def test_time_parsing():
    from fta.transports.rclone import parse_time

    assert parse_time("2024-01-02T03:04:05.123456789+01:00") == pytest.approx(1704161045.123456)
    assert parse_time("2024-01-02T03:04:05Z") == 1704164645.0
    assert parse_time("garbage") is None
    assert shutil  # keep the import for helpers below
