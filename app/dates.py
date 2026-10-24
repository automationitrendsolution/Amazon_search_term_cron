"""Report-date selection: the Asia/Kolkata 12:30 rule.

The server's local timezone is never consulted.  Every function takes an
injectable ``now`` (an aware datetime) or ``clock`` (a callable returning one),
so the cutoff boundary is testable and a UTC cron host, a laptop in IST and a
container without ``/etc/localtime`` all pick the same date.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .exceptions import ConfigurationError, ReportNotAvailableError

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CUTOFF = time(12, 30)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The current instant as an aware UTC datetime (never local time)."""
    return datetime.now(tz=UTC)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ConfigurationError(
            "A naive datetime was supplied; report-date selection needs a timezone-aware "
            "datetime so the server's local timezone can never leak in."
        )
    return moment


def select_report_date(
    now: datetime | None = None,
    *,
    clock: Clock = utc_now,
    tz: ZoneInfo = IST,
    cutoff: time = DEFAULT_CUTOFF,
) -> date:
    """Return the report date for ``now`` (default: ``clock()``).

    In ``tz`` (Asia/Kolkata):

    * before 12:30:00            -> yesterday's date
    * at exactly 12:30:00, after -> today's date
    """
    local = _aware(now if now is not None else clock()).astimezone(tz)
    if local.time().replace(tzinfo=None) >= cutoff:
        return local.date()
    return local.date() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Which saved Amazon report covers that date
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportChoice:
    report_date: date
    report_id: str
    #: "today" or "yesterday" -- relative to the AMAZON ACCOUNT's calendar.
    period: str
    expected_name: str | None
    account_today: date


def choose_report(
    report_date: date,
    *,
    account_tz: ZoneInfo,
    period_tz: ZoneInfo | None = None,
    report_id_today: str | None,
    report_id_yesterday: str | None,
    name_today: str | None = None,
    name_yesterday: str | None = None,
    now: datetime | None = None,
    clock: Clock = utc_now,
) -> ReportChoice:
    """Pick the saved report (relative period Today / Yesterday) for ``report_date``.

    Two calendars are involved (observed live on 2026-10-23):

    * ``period_tz`` -- the calendar Amazon uses to decide what "Today" and
      "Yesterday" mean.  For this account it is the *user's* timezone (IST):
      at 00:31 IST the "Yesterday" report already returned the IST-yesterday date.
    * ``account_tz`` -- the marketplace calendar the CSV rows are dated in
      (US Pacific).  A date that has not started there yet has no data.

    ``period_tz`` defaults to ``account_tz``.  The date inside the downloaded
    CSV is validated again later, so a wrong choice can never be imported.

    Raises:
        ReportNotAvailableError: the date has not started in the account's calendar yet.
        ConfigurationError: no configured report covers the date.
    """
    moment = _aware(now if now is not None else clock())
    account_today = moment.astimezone(account_tz).date()
    period_today = moment.astimezone(period_tz or account_tz).date()
    if report_date > account_today:
        raise ReportNotAvailableError(
            f"Report date {report_date} has not started yet in the Amazon account timezone "
            f"{account_tz.key} (account date is {account_today}). Skipping; nothing changed."
        )
    lag = (period_today - report_date).days
    if lag < 0:
        raise ReportNotAvailableError(
            f"Report date {report_date} is after today ({period_today}) in "
            f"{(period_tz or account_tz).key}. Skipping; nothing changed."
        )
    if lag == 0:
        report_id, name, period, var = report_id_today, name_today, "today", "REPORT_ID_TODAY"
    elif lag == 1:
        report_id, name, period, var = (
            report_id_yesterday,
            name_yesterday,
            "yesterday",
            "REPORT_ID_YESTERDAY",
        )
    else:
        raise ConfigurationError(
            f"Report date {report_date} is {lag} days before today ({period_today}); "
            "neither the Today nor the Yesterday report covers it."
        )
    if not report_id:
        raise ConfigurationError(f"{report_date} needs the '{period}' report but {var} is not set")
    return ReportChoice(report_date, report_id, period, name, account_today)
