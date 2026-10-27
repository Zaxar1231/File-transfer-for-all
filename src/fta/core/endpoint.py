"""Parsing of local paths and remote URLs into one shape."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

LOCAL_SCHEMES = frozenset({"", "file"})


@dataclass(frozen=True)
class Endpoint:
    """A location: a local path or a remote URL such as ``sftp://user@host/dir/file``."""

    scheme: str
    host: str | None
    port: int | None
    username: str | None
    password: str | None
    path: str
    query: str = ""

    @classmethod
    def parse(cls, location: str) -> Endpoint:
        if "://" not in location:
            return cls(scheme="", host=None, port=None, username=None, password=None, path=location)
        parts = urlsplit(location)
        return cls(
            scheme=parts.scheme.lower(),
            host=parts.hostname,
            port=parts.port,
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
            path=unquote(parts.path),
            query=parts.query,
        )

    @property
    def is_local(self) -> bool:
        return self.scheme in LOCAL_SCHEMES

    @property
    def netloc(self) -> str:
        """``host[:port]`` without credentials."""
        host = self.host or ""
        return f"{host}:{self.port}" if self.port else host
