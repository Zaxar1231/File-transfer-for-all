"""rclone: 70+ cloud storages (Google Drive, Dropbox, Яндекс.Диск, OneDrive, Mail.ru…) in one program.

The app finds rclone in its own ``bin`` folder, then in PATH, and can install the
official build there, checking the release's SHA256SUMS. Cloud accounts live in
the app's own ``rclone.conf``; remotes from the user's usual rclone config are
listed too, so an existing setup just works.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fta.core.errors import TransferError

logger = logging.getLogger(__name__)

VERSION = "v1.69.1"
DOWNLOADS = f"https://github.com/rclone/rclone/releases/download/{VERSION}"

DATA_DIR: Path | None = None
"""The app's data folder (bin/rclone and rclone.conf live there); set by the app and the CLI."""

PROVIDERS: dict[str, str] = {
    "drive": "Google Drive",
    "dropbox": "Dropbox",
    "yandex": "Яндекс.Диск",
    "onedrive": "OneDrive",
    "mailru": "Облако Mail.ru",
    "pcloud": "pCloud",
    "box": "Box",
    "mega": "MEGA",
    "webdav": "WebDAV (Nextcloud и др.)",
}
"""rclone backend type → name shown to people. Every other backend works too, by config."""

OAUTH = {"drive", "dropbox", "yandex", "onedrive", "pcloud", "box"}
"""Backends whose login is a browser sign-in; the others take a user name and password."""


@dataclass(frozen=True)
class Remote:
    name: str
    type: str

    @property
    def label(self) -> str:
        return PROVIDERS.get(self.type, self.type)


def binary_name() -> str:
    return "rclone.exe" if sys.platform == "win32" else "rclone"


def local_binary() -> Path | None:
    return DATA_DIR / "bin" / binary_name() if DATA_DIR else None


def find() -> str | None:
    """Path to rclone: FTA_RCLONE, the app's copy, then PATH."""
    if explicit := os.environ.get("FTA_RCLONE"):
        return explicit
    own = local_binary()
    if own is not None and own.exists():
        return str(own)
    return shutil.which("rclone")


def config_path() -> Path | None:
    return DATA_DIR / "rclone.conf" if DATA_DIR else None


def run(
    args: Sequence[str],
    *,
    remote: str | None = None,
    input: bytes | None = None,
    timeout: float | None = 120,
    check: bool = True,
    app_config: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run rclone with the config that defines ``remote`` (or the app's, with ``app_config``);
    a failure becomes a TransferError with rclone's message."""
    extra = ["--config", str(config_path())] if app_config and config_path() else config_args(remote)
    result = subprocess.run([_binary(), *extra, *args], input=input, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise TransferError(f"rclone {args[0]}: {_message(result.stderr)}")
    return result


def popen(args: Sequence[str], *, remote: str | None = None, **kwargs) -> subprocess.Popen[bytes]:
    return subprocess.Popen([_binary(), *config_args(remote), *args], **kwargs)


def _binary() -> str:
    binary = find()
    if binary is None:
        raise TransferError("rclone is not installed; install it in Settings or from https://rclone.org")
    return binary


def app_remotes() -> set[str]:
    """Section names in the app's rclone.conf."""
    import configparser

    path = config_path()
    if path is None or not path.exists():
        return set()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return set()
    return set(parser.sections())


def config_args(remote: str | None = None) -> list[str]:
    """The app's rclone.conf for remotes made in the app (and for config commands);
    the user's own rclone config for everything else."""
    path = config_path()
    if path is None:
        return []
    if remote is None or remote in app_remotes():
        return ["--config", str(path)]
    return []


def _message(stderr: bytes) -> str:
    lines = [line for line in stderr.decode(errors="replace").splitlines() if line.strip()]
    for line in reversed(lines):
        if "ERROR" in line or "Failed" in line or "error" in line:
            return line.split(" : ", 1)[-1].strip()
    return lines[-1].strip() if lines else "failed"


def remotes() -> list[Remote]:
    """Remotes made in the app first, then the ones from the user's usual rclone config."""
    binary = find()
    if binary is None:
        return []
    found: dict[str, Remote] = {}
    path = config_path()
    for extra in ([["--config", str(path)]] if path is not None and path.exists() else []) + [[]]:
        try:
            result = subprocess.run([binary, *extra, "config", "dump"], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            for name, section in json.loads(result.stdout or b"{}").items():
                found.setdefault(name, Remote(name, section.get("type", "?")))
    return sorted(found.values(), key=lambda r: r.name.casefold())


def create_remote(name: str, kind: str, params: dict[str, str] | None = None, *, timeout: float = 600) -> None:
    """Add a remote to the app's config. For OAuth providers rclone opens the browser to sign in
    and waits for it; for the rest ``params`` carries user/pass (rclone obscures the password)."""
    path = config_path()
    if path is None:
        raise TransferError("no data folder for rclone.conf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    args = ["config", "create", name, kind]
    for key, value in (params or {}).items():
        args += [key, value]
    if params and "pass" in params:
        args.append("--obscure")
    run(args, timeout=timeout, app_config=True)


def delete_remote(name: str) -> None:
    run(["config", "delete", name], remote=name)


# --- installing -----------------------------------------------------------------------


def asset_name() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm-v7"}.get(machine)
    system = {"linux": "linux", "darwin": "osx", "win32": "windows"}.get(sys.platform)
    if arch is None or system is None:
        raise TransferError(f"no rclone build for {sys.platform}/{machine}")
    return f"rclone-{VERSION}-{system}-{arch}.zip"


def install(progress: Callable[[int, int | None], None] | None = None, fetch: Callable[[str], bytes] | None = None) -> Path:
    """Download the official rclone into the app's bin folder, checking SHA256SUMS."""
    target = local_binary()
    if target is None:
        raise TransferError("no data folder to install rclone into")
    fetch = fetch or (lambda url: _download(url, progress))
    name = asset_name()
    sums = fetch(f"{DOWNLOADS}/SHA256SUMS").decode()
    expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(name)), None)
    if expected is None:
        raise TransferError(f"{name} is not listed in rclone's SHA256SUMS")
    archive = fetch(f"{DOWNLOADS}/{name}")
    if hashlib.sha256(archive).hexdigest() != expected:
        raise TransferError("the downloaded rclone does not match its published SHA-256; not installed")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        member = next(m for m in zipped.namelist() if m.endswith("/" + binary_name()))
        data = zipped.read(member)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, target)
    logger.info("installed rclone %s to %s", VERSION, target)
    return target


def _download(url: str, progress: Callable[[int, int | None], None] | None) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
        total = int(response.headers.get("Content-Length") or 0) or None
        chunks, done = [], 0
        while chunk := response.read(256 * 1024):
            chunks.append(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
        return b"".join(chunks)
