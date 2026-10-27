"""Update checks against a local stand-in for the GitHub Releases API."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import fta
from fta.app import updates

ROOT = Path(__file__).resolve().parent.parent


def test_package_and_pyproject_versions_match():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == fta.__version__


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("v0.3.1", "0.3.0", True),
        ("0.10.0", "0.9.9", True),  # numeric, not string, comparison
        ("v0.3.0", "0.3.0", False),
        ("v0.2.9", "0.3.0", False),
        ("v1.0.0-beta", "0.9.0", True),
    ],
)
def test_is_newer(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


@pytest.mark.parametrize(
    ("platform", "machine", "key"),
    [
        ("windows", "AMD64", "windows-x64"),
        ("macos", "arm64", "macos-arm64"),
        ("macos", "x86_64", "macos-x64"),
        ("linux", "x86_64", "linux-x64"),
        ("linux", "aarch64", "linux-arm64"),
        ("android", "aarch64", "android"),
        ("ios", "arm64", None),
        (None, "x86_64", None),
    ],
)
def test_platform_key(platform, machine, key):
    assert updates.platform_key(platform, machine) == key


@pytest.fixture
def fake_github():
    """Serves /releases/latest, /missing (404) and one downloadable asset."""
    payload = b"new build" * 1000
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/releases/latest":
                body = json.dumps(
                    {
                        "tag_name": "v9.9.9",
                        "html_url": "https://example.invalid/release",
                        "body": "* Faster transfers",
                        "assets": [
                            {
                                "name": updates.ASSETS["linux-x64"],
                                "browser_download_url": f"{state['base']}/download/{updates.ASSETS['linux-x64']}",
                            }
                        ],
                    }
                ).encode()
            elif self.path.startswith("/download/"):
                body = payload
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state["base"], payload
    server.shutdown()
    server.server_close()


def test_fetch_latest_and_download(fake_github, tmp_path):
    base, payload = fake_github
    release = updates.fetch_latest(f"{base}/releases/latest")

    assert release.version == "9.9.9"
    assert updates.is_newer(release.version)
    assert release.notes == "* Faster transfers"
    assert release.download_url("windows-x64") is None  # no Windows asset in this release
    url = release.download_url("linux-x64")

    events = []
    path = updates.download(url, tmp_path, lambda done, total: events.append((done, total)))

    assert path.name == updates.ASSETS["linux-x64"]
    assert path.read_bytes() == payload
    assert events[-1] == (len(payload), len(payload))
    assert not list(tmp_path.glob("*.part"))


def test_no_release_yet_is_not_an_error(fake_github):
    base, _ = fake_github
    assert updates.fetch_latest(f"{base}/missing") is None
