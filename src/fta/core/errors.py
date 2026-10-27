"""Exceptions raised by the core and by transports."""


class TransferError(Exception):
    """Base class for all fta errors."""


class UnsupportedScheme(TransferError):
    """No transport is registered for the URL scheme."""


class UnsupportedOperation(TransferError):
    """The transport does not implement the requested operation."""


class NotFound(TransferError):
    """The remote file or directory does not exist."""
