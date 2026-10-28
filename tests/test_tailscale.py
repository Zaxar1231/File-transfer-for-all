"""Tailscale peers (through a stand-in tailscale program), the FTA probe and Taildrop."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from fta.core.errors import TransferError
from fta.lan.direct import fetch_info
from fta.tools import tailscale
from test_lan import receiver  # noqa: F401 - fixture

STATUS = {
    "Self": {"HostName": "me", "TailscaleIPs": ["100.64.0.1"]},
    "Peer": {
        "a": {"HostName": "nas", "DNSName": "nas.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.7", "fd7a::7"], "Online": True, "OS": "linux"},
        "b": {"HostName": "old-phone", "TailscaleIPs": ["100.64.0.9"], "Online": False, "OS": "android"},
        "c": {"HostName": "laptop", "TailscaleIPs": ["127.0.0.1"], "Online": True, "OS": "windows"},
    },
}


@pytest.fixture
def fake_tailscale(tmp_path, monkeypatch):
    log = tmp_path / "calls.json"
    script = tmp_path / "tailscale"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"log = {str(log)!r}\n"
        "calls = json.load(open(log)) if __import__('os').path.exists(log) else []\n"
        "calls.append(sys.argv[1:]); json.dump(calls, open(log, 'w'))\n"
        "if sys.argv[1:3] == ['status', '--json']:\n"
        f"    print(json.dumps({STATUS!r}))\n"
        "elif sys.argv[1:3] == ['file', 'cp'] and sys.argv[-1].startswith('100.64.0.9'):\n"
        "    print('peer is offline', file=sys.stderr); sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FTA_TAILSCALE", str(script))
    return lambda: json.loads(log.read_text()) if log.exists() else []


@pytest.mark.skipif(os.name != "posix", reason="the stand-in is a script with a shebang")
def test_peers_and_taildrop(tmp_path, fake_tailscale):
    found = tailscale.peers()
    assert [p.name for p in found] == ["laptop", "nas", "old-phone"]  # online first
    nas = found[1]
    assert nas.address == "100.64.0.7" and nas.dns_name == "nas.tail1234.ts.net" and nas.online

    photo = tmp_path / "фото.jpg"
    photo.write_bytes(b"jpeg")
    tailscale.taildrop([photo], nas)
    assert fake_tailscale()[-1] == ["file", "cp", str(photo), "100.64.0.7:"]
    with pytest.raises(TransferError, match="offline"):
        tailscale.taildrop([photo], found[2])
    with pytest.raises(TransferError, match="folders"):
        tailscale.taildrop([tmp_path], nas)


@pytest.mark.skipif(os.name != "posix", reason="the stand-in is a script with a shebang")
def test_a_tailnet_device_running_fta_is_found_by_probing(fake_tailscale, receiver):  # noqa: F811
    laptop = next(p for p in tailscale.peers() if p.name == "laptop")
    info, fingerprint = fetch_info(laptop.address, receiver.port, timeout=3)
    assert info["name"] == "Receiver" and fingerprint == receiver.identity.fingerprint


def test_missing_tailscale_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("FTA_TAILSCALE", "")
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: None)
    monkeypatch.setattr(tailscale, "CANDIDATES", {})
    assert tailscale.find() is None
    with pytest.raises(TransferError, match="not installed"):
        tailscale.peers()
