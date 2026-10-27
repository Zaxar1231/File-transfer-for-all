"""Saved connections and settings, kept as one JSON file in the app's data directory."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROTOCOLS: dict[str, str] = {
    "sftp": "SFTP",
    "smb": "SMB (Windows, Samba, NAS)",
    "ftp": "FTP",
    "ftps": "FTPS",
    "davs": "WebDAV (HTTPS)",
    "dav": "WebDAV",
    "s3": "Amazon S3",
    "https": "HTTPS",
    "http": "HTTP",
}

DEFAULT_PORTS = {"sftp": 22, "smb": 445, "ftp": 21, "ftps": 21, "dav": 80, "davs": 443, "http": 80, "https": 443}

LOCAL_ID = "local"
"""Connection id that stands for this device's own filesystem."""


@dataclass
class Connection:
    name: str
    scheme: str
    host: str
    port: int | None = None
    username: str = ""
    password: str = ""
    save_password: bool = False
    path: str = "/"
    options: dict[str, str] = field(default_factory=dict)
    """Protocol-specific options, e.g. key_file for SFTP or endpoint_url for S3."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def url(self, path: str | None = None) -> str:
        netloc = self.host
        if self.port and self.port != DEFAULT_PORTS.get(self.scheme):
            netloc = f"{netloc}:{self.port}"
        if self.username:
            netloc = f"{quote(self.username, safe='')}@{netloc}"
        target = path if path is not None else self.path
        return f"{self.scheme}://{netloc}{quote(target or '/')}"

    def transport_options(self) -> dict[str, str]:
        """Options for fta transports; the password goes here, never into the URL."""
        options = dict(self.options)
        if self.password:
            options["password"] = self.password
        return options

    @property
    def label(self) -> str:
        return PROTOCOLS.get(self.scheme, self.scheme.upper())

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.save_password:
            data["password"] = ""
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Connection:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Settings:
    theme_mode: str = "system"
    """One of "system", "light", "dark"."""
    seed_color: str = "#6750A4"
    parallel_transfers: int = 2
    show_hidden: bool = False
    """Show files whose names start with a dot."""
    check_updates: bool = True
    """Look for a newer release on GitHub at start-up."""


def default_data_dir() -> Path:
    """FLET_APP_STORAGE_DATA inside a packaged app, otherwise the usual per-user config dir."""
    if packaged := os.environ.get("FLET_APP_STORAGE_DATA"):
        return Path(packaged)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "fta"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "fta"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fta"


class Store:
    """Connections and settings with load/save. Saved passwords are stored in plain text,
    so the file is created readable by the current user only."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "config.json"
        self.connections: list[Connection] = []
        self.settings = Settings()
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.connections = [Connection.from_json(item) for item in data.get("connections", [])]
        known = {f.name for f in fields(Settings)}
        self.settings = Settings(**{k: v for k, v in data.get("settings", {}).items() if k in known})

    def save(self) -> None:
        data = {
            "connections": [connection.to_json() for connection in self.connections],
            "settings": asdict(self.settings),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def get(self, connection_id: str) -> Connection | None:
        return next((c for c in self.connections if c.id == connection_id), None)

    def upsert(self, connection: Connection) -> None:
        for index, existing in enumerate(self.connections):
            if existing.id == connection.id:
                self.connections[index] = connection
                break
        else:
            self.connections.append(connection)
        self.save()

    def remove(self, connection_id: str) -> None:
        self.connections = [c for c in self.connections if c.id != connection_id]
        self.save()
