"""Exception taxonomy.

Errors are split by whether the caller should retry, because that decision must not be made
by string-matching a message. ``TransientError`` subclasses are retried with backoff by the
order router and the venue clients; everything else fails fast and is journalled.
"""

from __future__ import annotations


class AtlasError(Exception):
    """Base for all ATLAS errors."""


class ConfigError(AtlasError):
    """Invalid or missing configuration. Never retryable."""


class TransientError(AtlasError):
    """A failure that may succeed on retry (timeout, requote, temporary disconnect)."""


class VenueError(AtlasError):
    """Venue rejected a request for a definite reason.

    ``retcode`` carries the broker's raw code so incident analysis is not guesswork.
    """

    def __init__(self, message: str, retcode: int = 0, retcode_text: str = "") -> None:
        super().__init__(message)
        self.retcode = retcode
        self.retcode_text = retcode_text


class VenueUnavailable(TransientError):
    """Venue is unreachable or not ready. Retryable."""


class OrderRejected(VenueError):
    """Order was definitively rejected. Not retryable without changing the request."""


class RiskViolation(AtlasError):
    """The risk engine refused an action. Never bypassable."""

    def __init__(self, message: str, code: str = "RISK_VIOLATION") -> None:
        super().__init__(message)
        self.code = code


class DataError(AtlasError):
    """Market data is missing, malformed, or stale beyond tolerance."""


class ReconciliationError(AtlasError):
    """Local state and broker truth diverged in a way we will not auto-repair."""
