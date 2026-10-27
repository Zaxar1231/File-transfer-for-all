"""Maps URL schemes to transport classes.

Built-in transports are imported on first use. Third-party packages can add
transports through the ``fta.transports`` entry point group.
"""

from __future__ import annotations

from importlib import import_module, metadata
from typing import Any

from fta.core.errors import UnsupportedScheme
from fta.core.endpoint import Endpoint
from fta.core.transport import Transport

ENTRY_POINT_GROUP = "fta.transports"
BUILTIN_MODULES = (
    "fta.transports.http",
    "fta.transports.ftp",
    "fta.transports.planned",
)

_registry: dict[str, type[Transport]] = {}
_loaded = False


def register(cls: type[Transport]) -> type[Transport]:
    """Class decorator: make the transport available for each of its schemes."""
    for scheme in cls.schemes:
        _registry[scheme] = cls
    return cls


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module in BUILTIN_MODULES:
        import_module(module)
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        register(entry_point.load())


def transport_class(scheme: str) -> type[Transport]:
    _load_all()
    try:
        return _registry[scheme.lower()]
    except KeyError:
        known = ", ".join(known_schemes())
        raise UnsupportedScheme(f"no transport for scheme {scheme!r}; known: {known}") from None


def open_transport(location: str, **options: Any) -> Transport:
    """Build a transport for a remote URL. Use it as a context manager to connect."""
    endpoint = Endpoint.parse(location)
    return transport_class(endpoint.scheme)(endpoint, **options)


def known_schemes() -> list[str]:
    _load_all()
    return sorted(_registry)
