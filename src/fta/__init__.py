"""fta: one API for copying files over many protocols."""

from fta.core.endpoint import Endpoint
from fta.core.errors import NotFound, TransferError, UnsupportedOperation, UnsupportedScheme
from fta.core.manager import copy
from fta.core.registry import known_schemes, open_transport, register
from fta.core.transport import ProgressCallback, RemoteEntry, Transport

__all__ = [
    "Endpoint",
    "NotFound",
    "ProgressCallback",
    "RemoteEntry",
    "Transport",
    "TransferError",
    "UnsupportedOperation",
    "UnsupportedScheme",
    "copy",
    "known_schemes",
    "open_transport",
    "register",
]
