"""Saved connections and settings (config.toml in the app's data directory), logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

logger = logging.getLogger(__name__)

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
    """Every field can be overridden with an environment variable FTA_<NAME>, e.g. FTA_PARALLEL_TRANSFERS=4."""

    theme_mode: str = "system"
    """One of "system", "light", "dark"."""
    seed_color: str = "#6750A4"
    parallel_transfers: int = 2
    retries: int = 3
    """Automatic retries after a failed attempt; each one resumes the partial file."""
    verify: bool = False
    """Hash both sides with SHA-256 after each file and fail on a mismatch."""
    show_hidden: bool = False
    """Show files whose names start with a dot."""
    check_updates: bool = True
    """Look for a newer release on GitHub at start-up."""
    log_level: str = "INFO"


def default_data_dir() -> Path:
    """FTA_DATA_DIR, FLET_APP_STORAGE_DATA inside a packaged app, else the usual per-user config dir."""
    if explicit := os.environ.get("FTA_DATA_DIR"):
        return Path(explicit)
    if packaged := os.environ.get("FLET_APP_STORAGE_DATA"):
        return Path(packaged)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "fta"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "fta"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fta"


def env_overrides(environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """Settings given as FTA_<FIELD> environment variables, converted to the field's type."""
    overrides: dict[str, Any] = {}
    for spec in fields(Settings):
        raw = environ.get(f"FTA_{spec.name.upper()}")
        if raw is None:
            continue
        default = getattr(Settings(), spec.name)
        if isinstance(default, bool):
            overrides[spec.name] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            try:
                overrides[spec.name] = int(raw)
            except ValueError:
                continue  # a typo in the environment must not stop the app
        else:
            overrides[spec.name] = raw
    return overrides


class Store:
    """Connections and settings in ``config.toml``.

    Saved passwords are stored in plain text, so the file is created readable by
    the current user only. Settings set through FTA_* environment variables win
    over the file but are never written back to it.
    """

    def __init__(self, path: Path | None = None, environ: Mapping[str, str] = os.environ) -> None:
        self.path = path or Path(environ.get("FTA_CONFIG") or default_data_dir() / "config.toml")
        self.data_dir = self.path.parent
        self.connections: list[Connection] = []
        self.overrides = env_overrides(environ)
        self._file_settings = Settings()
        self.settings = Settings(**self.overrides)
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        data = self._read()
        if data is None:
            return
        self.connections = [Connection.from_json(item) for item in data.get("connections", [])]
        known = {f.name for f in fields(Settings)}
        self._file_settings = Settings(**{k: v for k, v in data.get("settings", {}).items() if k in known})
        self.settings = replace(self._file_settings, **self.overrides)

    def _read(self) -> dict[str, Any] | None:
        try:
            return tomllib.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, tomllib.TOMLDecodeError):
            logger.exception("cannot read %s; starting with defaults", self.path)
            return None
        legacy = self.path.with_name("config.json")  # versions before 0.4 kept JSON
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        logger.info("migrating %s to %s", legacy, self.path)
        self.connections = [Connection.from_json(item) for item in data.get("connections", [])]
        known = {f.name for f in fields(Settings)}
        self._file_settings = Settings(**{k: v for k, v in data.get("settings", {}).items() if k in known})
        self.settings = replace(self._file_settings, **self.overrides)
        self.save()
        legacy.rename(legacy.with_suffix(".json.migrated"))
        return None

    def save(self) -> None:
        settings = asdict(self.settings)
        for name, value in self.overrides.items():
            if settings[name] == value:  # still the environment's value: keep the file's own
                settings[name] = getattr(self._file_settings, name)
        self._file_settings = Settings(**settings)
        data = {
            "settings": settings,
            "connections": [_drop_none(connection.to_json()) for connection in self.connections],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fp:
                tomli_w.dump(data, fp)
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


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    """TOML has no null; a missing key reads back as the field's default."""
    return {key: value for key, value in data.items() if value is not None}


def setup_logging(data_dir: Path, level: str = "INFO") -> Path:
    """Log to a rotating file in the data directory (1 MB x 3) and return its path."""
    log_path = data_dir / "fta.log"
    data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(getattr(h, "baseFilename", None) == str(log_path) for h in root.handlers):
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    return log_path
