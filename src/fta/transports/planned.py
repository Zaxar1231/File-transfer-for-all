"""Protocols whose scheme is reserved but whose backend is not written yet.

Each class fails clearly on connect(). When a protocol is implemented, move its
class into its own module, delete the stub here, and add the module to
BUILTIN_MODULES in fta.core.registry.
"""

from __future__ import annotations

from typing import ClassVar

from fta.core.errors import UnsupportedOperation
from fta.core.registry import register
from fta.core.transport import Transport


class _Planned(Transport):
    backend_hint: ClassVar[str] = ""

    def connect(self) -> None:
        hint = f" (planned backend: {self.backend_hint})" if self.backend_hint else ""
        raise UnsupportedOperation(f"{self.endpoint.scheme}:// is not implemented yet{hint}")

    def close(self) -> None:
        """Never connected, so nothing to release."""


@register
class SmbTransport(_Planned):
    schemes = ("smb",)
    backend_hint = "smbprotocol"
