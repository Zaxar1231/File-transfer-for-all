"""Updates from GitHub Releases: find the newest version, pick this platform's build, download it.

Release assets have fixed names (no version inside), so
``https://github.com/<repo>/releases/latest/download/<name>`` always points at the newest
build; the README's download links rely on the same names.
"""

from __future__ import annotations

import json
import platform as host
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from fta import __version__
from fta.core.transport import Progress, ProgressCallback, copy_stream

REPOSITORY = "Zaxar1231/File-transfer-for-all"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"

ASSETS = {
    "windows-x64": "FileTransferForAll-windows-x64.zip",
    "macos": "FileTransferForAll-macos.zip",
    "linux-x64": "FileTransferForAll-linux-x64.tar.gz",
    "linux-arm64": "FileTransferForAll-linux-arm64.tar.gz",
    "android": "FileTransferForAll-android.apk",
}
"""Platform key → release asset name. The release workflow uploads exactly these files."""


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    notes: str = ""
    assets: dict[str, str] = field(default_factory=dict)
    """Asset name → download URL."""

    def download_url(self, platform_key: str | None) -> str | None:
        name = ASSETS.get(platform_key or "")
        return self.assets.get(name) if name else None


def parse_version(text: str) -> tuple[int, ...]:
    """``v1.2.10`` → ``(1, 2, 10)``; anything after the numeric part (``-beta``) is ignored."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def platform_key(page_platform: str | None, machine: str | None = None) -> str | None:
    """Which release asset fits this device. ``page_platform`` is Flet's ``page.platform`` value."""
    machine = (machine or host.machine()).lower()
    if page_platform == "android":
        return "android"
    if page_platform == "windows":
        return "windows-x64"  # Windows on ARM runs the x64 build under emulation
    if page_platform == "macos":
        return "macos"
    if page_platform == "linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    return None  # iOS and web have no downloadable build


def fetch_latest(url: str = LATEST_RELEASE_API, timeout: float = 10) -> Release | None:
    """The newest published release, or ``None`` when there is none yet."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": f"fta/{__version__}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return Release(
        version=str(data["tag_name"]).lstrip("v"),
        page_url=data.get("html_url") or RELEASES_PAGE,
        notes=data.get("body") or "",
        assets={asset["name"]: asset["browser_download_url"] for asset in data.get("assets", [])},
    )


def download(url: str, folder: Path, progress: ProgressCallback | None = None, timeout: float = 30) -> Path:
    """Download into ``folder`` and return the file; a partial download never takes the final name."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    target = folder / name
    partial = folder / f"{name}.part"
    request = urllib.request.Request(url, headers={"User-Agent": f"fta/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as out:
        length = response.headers.get("Content-Length")
        copy_stream(response, out, Progress(progress, int(length) if length and length.isdigit() else None))
    partial.replace(target)
    return target


def downloads_folder() -> Path:
    folder = Path.home() / "Downloads"
    return folder if folder.is_dir() else Path.home()


def reveal(path: Path) -> None:
    """Show a downloaded file in the system file manager."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])
