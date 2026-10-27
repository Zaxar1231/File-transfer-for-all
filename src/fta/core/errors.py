"""Exceptions raised by the core and by transports."""


class TransferError(Exception):
    """Base class for all fta errors."""


class UnsupportedScheme(TransferError):
    """No transport is registered for the URL scheme."""


class UnsupportedOperation(TransferError):
    """The transport does not implement the requested operation."""


class NotFound(TransferError):
    """The remote file or directory does not exist."""


class IntegrityError(TransferError):
    """The copy's SHA-256 does not match the source."""


class AccessDenied(TransferError):
    """The other side refused: wrong password or PIN, a declined offer, an unexpected certificate."""
