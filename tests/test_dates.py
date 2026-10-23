"""The Asia/Kolkata 12:30 cutoff, independent of the server's timezone."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.dates import IST, choose_report, select_report_date, utc_now
from app.exceptions import ConfigurationError, ReportNotAvailableError

ROOT = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")
T = "11111111-1111-1111-1111-111111111111"
Y = "22222222-2222-2222-2222-222222222222"


def ist(h: int, m: int, s: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 10, day, h, m, s, tzinfo=IST)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (ist(12, 29, 59), date(2026, 10, 21)),  # one second before cutoff -> yesterday
        (ist(12, 30, 0), date(2026, 10, 22)),  # exactly at cutoff -> today
        (ist(12, 30, 1), date(2026, 10, 22)),  # after cutoff -> today
        (ist(0, 0, 0), date(2026, 10, 21)),  # midnight -> yesterday
        (ist(23, 59, 59), date(2026, 10, 22)),
    ],
)
def test_cutoff_boundaries(moment: datetime, expected: date) -> None:
    assert select_report_date(moment) == expected


def test_injectable_clock() -> None:
    assert select_report_date(clock=lambda: ist(12, 30)) == date(2026, 10, 22)
    assert select_report_date(clock=lambda: ist(12, 29, 59)) == date(2026, 10, 21)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # 07:00 UTC == 12:30 IST
        (datetime(2026, 10, 22, 6, 59, 59, tzinfo=UTC), date(2026, 10, 21)),
        (datetime(2026, 10, 22, 7, 0, 0, tzinfo=UTC), date(2026, 10, 22)),
        # 19:00 Oct 21 in New York (EDT, -4) == 04:30 Oct 22 IST -> yesterday == Oct 21
        (datetime(2026, 10, 21, 19, 0, tzinfo=ZoneInfo("America/New_York")), date(2026, 10, 21)),
        # 18:30 UTC Oct 21 == 00:00 Oct 22 IST (midnight)
        (datetime(2026, 10, 21, 18, 30, tzinfo=UTC), date(2026, 10, 21)),
        # A fixed offset that is neither UTC nor IST
        (datetime(2026, 10, 22, 16, 0, tzinfo=timezone(timedelta(hours=10))), date(2026, 10, 21)),
    ],
)
def test_datetimes_in_other_timezones(moment: datetime, expected: date) -> None:
    assert select_report_date(moment) == expected


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        select_report_date(datetime(2026, 10, 22, 13, 0))


def test_utc_now_is_aware() -> None:
    assert utc_now().tzinfo is not None and utc_now().utcoffset() == timedelta(0)


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "UTC", "Pacific/Kiritimati", "EST5EDT"])
def test_machine_timezone_does_not_matter(tz: str) -> None:
    """Run the selection in a child process whose local timezone is something else."""
    code = (
        "from datetime import datetime, timezone; from app.dates import select_report_date, utc_now;"
        "import time;"
        "moments=[datetime(2026,10,22,6,59,59,tzinfo=timezone.utc), datetime(2026,10,22,7,0,0,tzinfo=timezone.utc)];"
        "print(','.join(select_report_date(m).isoformat() for m in moments));"
        "print(int(utc_now().timestamp()))"
    )
    env = {**os.environ, "TZ": tz}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    dates, stamp = out.stdout.strip().splitlines()
    assert dates == "2026-10-21,2026-10-22"
    # The child's "now" is the true instant regardless of its local zone.
    assert abs(int(stamp) - int(datetime.now(tz=UTC).timestamp())) < 60


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset is POSIX-only")
def test_in_process_tz_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert select_report_date(datetime(2026, 10, 22, 7, 0, tzinfo=UTC)) == date(2026, 10, 22)
    finally:
        monkeypatch.undo()
        time.tzset()


# -- choosing the saved report (Today / Yesterday) in the ACCOUNT's calendar ---


def test_same_timezone_account_uses_today_and_yesterday() -> None:
    after = choose_report(date(2026, 10, 22), account_tz=IST, report_id_today=T, report_id_yesterday=Y, now=ist(13, 0))
    before = choose_report(date(2026, 10, 21), account_tz=IST, report_id_today=T, report_id_yesterday=Y, now=ist(9, 0))
    assert (after.period, after.report_id) == ("today", T)
    assert (before.period, before.report_id) == ("yesterday", Y)


def test_us_account_before_cutoff_uses_today_definition() -> None:
    # 10:00 IST Oct 22 = 21:30 PDT Oct 21: IST "yesterday" is the account's "today".
    choice = choose_report(date(2026, 10, 21), account_tz=LA, report_id_today=T, report_id_yesterday=Y, now=ist(10, 0))
    assert choice.period == "today"


def test_date_not_started_for_account_is_skipped() -> None:
    # 12:45 IST Oct 22 = 00:15 PDT Oct 22 ... but in winter (PST, -8) it is still Oct 21.
    winter = datetime(2026, 12, 22, 12, 45, tzinfo=IST)
    with pytest.raises(ReportNotAvailableError):
        choose_report(date(2026, 12, 22), account_tz=LA, report_id_today=T, report_id_yesterday=Y, now=winter)


def test_missing_report_id_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        choose_report(date(2026, 10, 22), account_tz=IST, report_id_today=None, report_id_yesterday=Y, now=ist(13, 0))
