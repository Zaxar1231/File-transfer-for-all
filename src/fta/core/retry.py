"""When a failed transfer is worth another attempt, and how long to wait first."""

from __future__ import annotations

from fta.core.errors import AccessDenied, NotFound, UnsupportedOperation, UnsupportedScheme

NOT_RETRYABLE: tuple[type[BaseException], ...] = (
    AccessDenied,
    NotFound,
    UnsupportedOperation,
    UnsupportedScheme,
    FileNotFoundError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
)
"""Errors a second attempt cannot fix: they need the user, not time."""


def is_retryable(exc: BaseException) -> bool:
    return not isinstance(exc, NOT_RETRYABLE)


def retry_delay(attempt: int) -> float:
    """2, 4, 8, ... seconds, at most a minute."""
    return float(min(2**attempt, 60))
