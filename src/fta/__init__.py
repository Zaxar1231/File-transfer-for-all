"""fta: one API for copying files over many protocols."""

__version__ = "0.6.0"
"""Keep in sync with [project].version in pyproject.toml; release tags are v<version>."""

from fta.core.endpoint import Endpoint  # noqa: E402
from fta.core.errors import (  # noqa: E402
    IntegrityError,
    NotFound,
    TransferError,
    UnsupportedOperation,
    UnsupportedScheme,
)
from fta.core.manager import FileResult, copy  # noqa: E402
from fta.core.registry import known_schemes, open_transport, register  # noqa: E402
from fta.core.transport import ProgressCallback, RemoteEntry, Transport  # noqa: E402

__all__ = [
    "__version__",
    "Endpoint",
    "FileResult",
    "IntegrityError",
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
