"""Maps URL schemes to transport classes.

Built-in transports are imported on first use. Third-party packages can add
transports through the ``fta.transports`` entry point group.
"""

from __future__ import annotations

import threading
from importlib import import_module, metadata
from typing import Any

from fta.core.endpoint import Endpoint
from fta.core.errors import UnsupportedScheme
from fta.core.transport import Transport

ENTRY_POINT_GROUP = "fta.transports"
BUILTIN_MODULES = (
    "fta.transports.local",
    "fta.transports.memory",
    "fta.transports.http",
    "fta.transports.webdav",
    "fta.transports.ftp",
    "fta.transports.sftp",
    "fta.transports.s3",
    "fta.transports.smb",
    "fta.transports.rclone",
)

_registry: dict[str, type[Transport]] = {}
_loaded = False
_load_lock = threading.Lock()


def register(cls: type[Transport]) -> type[Transport]:
    """Class decorator: make the transport available for each of its schemes."""
    for scheme in cls.schemes:
        _registry[scheme] = cls
    return cls


def _load_all() -> None:
    """Import built-in and plugin transports once, even when several threads ask at the same time."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        for module in BUILTIN_MODULES:
            import_module(module)
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
            register(entry_point.load())
        _loaded = True


def transport_class(scheme: str) -> type[Transport]:
    _load_all()
    try:
        return _registry[scheme.lower()]
    except KeyError:
        known = ", ".join(known_schemes())
        raise UnsupportedScheme(f"no transport for scheme {scheme!r}; known: {known}") from None


def open_transport(location: str, **options: Any) -> Transport:
    """Build a transport for a local path or URL. Use it as a context manager to connect."""
    endpoint = Endpoint.parse(location)
    return transport_class(endpoint.scheme)(endpoint, **options)


def known_schemes() -> list[str]:
    """Registered URL schemes; plain local paths (scheme "") are not listed."""
    _load_all()
    return sorted(scheme for scheme in _registry if scheme)
