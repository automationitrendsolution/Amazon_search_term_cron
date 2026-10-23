"""Exception hierarchy.

Every failure the pipeline can hit maps to one class, and every class maps to
one process exit code (see :data:`EXIT_CODES` and ``run.py``), so cron logs and
monitoring can tell "session expired" from "data may already be committed".
"""

from __future__ import annotations


class AutomationError(Exception):
    """Base class for expected, reported failures."""

    exit_code = 1


class ConfigurationError(AutomationError):
    """Missing or invalid settings, selectors or arguments."""

    exit_code = 2


class AuthenticationError(AutomationError):
    """The saved Amazon session is missing/expired or a challenge is shown."""

    exit_code = 3


class BrowserError(AutomationError):
    """Navigation, selector, browser-crash or polling failure."""

    exit_code = 4


class NavigationError(BrowserError):
    """A page did not load or an expected control was missing."""


class ReportNotReadyError(BrowserError):
    """The run we started never offered a download within the timeout."""


class StaleReportError(BrowserError):
    """The run we started could not be told apart from older runs.

    Raised instead of guessing: downloading the wrong run would load another
    day's (or another hour's) data.
    """


class DownloadError(BrowserError):
    """The download did not arrive, failed, or is empty/unreadable."""


class ReportNotAvailableError(AutomationError):
    """The selected date has not started yet in the Amazon account timezone."""

    exit_code = 0  # an expected skip, not a failure


class ValidationError(AutomationError):
    """The CSV failed validation.  Nothing was written to MySQL."""

    exit_code = 5

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = list(problems or [])
        detail = "".join(f"\n  - {p}" for p in self.problems[:25])
        more = f"\n  ... and {len(self.problems) - 25} more" if len(self.problems) > 25 else ""
        super().__init__(f"{message}{detail}{more}")


class SchemaError(AutomationError):
    """Destination table missing, incompatible, or not transactional."""

    exit_code = 6


class DatabaseError(AutomationError):
    """Connection/query failure.  ``committed`` says what state MySQL is in."""

    exit_code = 6

    def __init__(self, message: str, *, committed: bool | None = False) -> None:
        #: False = rolled back / never written; True = committed;
        #: None = unknown (failure during COMMIT itself).
        self.committed = committed
        super().__init__(message)


class VerificationError(AutomationError):
    """Post-commit verification failed.  The data IS committed."""

    exit_code = 7


class ArchiveError(AutomationError):
    """Import and verification succeeded, but moving the file failed."""

    exit_code = 8


class LockError(AutomationError):
    """Another run holds the lock."""

    exit_code = 9
