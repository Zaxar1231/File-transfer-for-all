"""Tailscale: your devices anywhere, as if they were on the same network.

mDNS does not cross a tailnet, so the app asks the ``tailscale`` program for the
devices instead. Those that run File Transfer for All get the usual direct
transfer (Tailscale's WireGuard already proves who is at each 100.x address, so
the first certificate seen there is trusted). The others receive files through
Taildrop, Tailscale's own file sending.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fta.core.errors import TransferError

CANDIDATES = {
    "darwin": ["/Applications/Tailscale.app/Contents/MacOS/Tailscale"],
    "win32": [r"C:\Program Files\Tailscale\tailscale.exe"],
}


@dataclass(frozen=True)
class TailscalePeer:
    name: str
    address: str
    """The device's 100.x (IPv4) Tailscale address."""
    online: bool
    os: str = ""
    dns_name: str = ""

    def describe(self) -> str:
        return f"{self.name} ({self.address}, {self.os})" if self.os else f"{self.name} ({self.address})"


def find() -> str | None:
    if explicit := os.environ.get("FTA_TAILSCALE"):
        return explicit
    if found := shutil.which("tailscale"):
        return found
    return next((path for path in CANDIDATES.get(sys.platform, []) if Path(path).exists()), None)


def _run(args: list[str], timeout: float = 15, **kwargs) -> subprocess.CompletedProcess[bytes]:
    binary = find()
    if binary is None:
        raise TransferError("Tailscale is not installed (https://tailscale.com/download)")
    result = subprocess.run([binary, *args], capture_output=True, timeout=timeout, **kwargs)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
        raise TransferError(f"tailscale {args[0]}: {message[-1] if message else 'failed'}")
    return result


def peers() -> list[TailscalePeer]:
    """The other devices in your tailnet, online ones first."""
    status = json.loads(_run(["status", "--json"]).stdout or b"{}")
    found = []
    for peer in (status.get("Peer") or {}).values():
        addresses = [a for a in peer.get("TailscaleIPs") or [] if "." in a]
        if not addresses:
            continue
        found.append(
            TailscalePeer(
                name=peer.get("HostName") or peer.get("DNSName", "?").split(".")[0],
                address=addresses[0],
                online=bool(peer.get("Online")),
                os=peer.get("OS", ""),
                dns_name=(peer.get("DNSName") or "").rstrip("."),
            )
        )
    return sorted(found, key=lambda p: (not p.online, p.name.casefold()))


def taildrop(paths: list[Path], peer: TailscalePeer, timeout: float = 3600) -> None:
    """Send files with Taildrop; they land in the other device's Taildrop inbox."""
    files = [str(p) for p in paths if p.is_file()]
    if not files:
        raise TransferError("Taildrop sends files, not folders; zip the folder or use a device running FTA")
    _run(["file", "cp", *files, f"{peer.address}:"], timeout=timeout)
