"""Browser workflow logic that can be tested without Amazon: run identification,
stale-download protection, polling, timeouts and download handling."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import browser
from app.browser import (
    HistoryRow,
    ReportHistoryPage,
    identify_new_run,
    load_selectors,
    make_row,
    parse_created_at,
    poll_until_ready,
    retrieve_with_retries,
    safe_url,
    validate_download,
)
from app.config import DEFAULT_SELECTOR_FILE
from app.dates import ReportChoice
from app.exceptions import (
    AuthenticationError,
    DownloadError,
    NavigationError,
    ReportNotReadyError,
    StaleReportError,
)

LA = ZoneInfo("America/Los_Angeles")
CATALOGUE = load_selectors(DEFAULT_SELECTOR_FILE)
#: Trigger at 10:05:30 PDT Oct 21 == 17:05:30 UTC.
TRIGGER = datetime(2026, 10, 21, 17, 5, 30, tzinfo=UTC)


def row(text: str, has_download: bool = False, index: int = 0) -> HistoryRow:
    return make_row(index, text, has_download, CATALOGUE, LA)


OLD_DONE = row("Completed Oct 20, 2026 2:00 AM PDT Yesterday – Download", True)
OLD_DONE_2 = row("Completed Jul 10, 2026 5:28 AM PDT Yesterday – Download", True, 1)


# -- parsing -------------------------------------------------------------------


def test_parse_live_row_format() -> None:
    text, moment = parse_created_at(OLD_DONE.text, CATALOGUE["pattern_created_at"], LA)
    assert text == "Oct 20, 2026 2:00 AM PDT"
    assert moment == datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def test_parse_without_abbreviation_uses_browser_zone() -> None:
    _, moment = parse_created_at("Processing Oct 21, 2026 10:05 AM Today -", CATALOGUE["pattern_created_at"], LA)
    assert moment == datetime(2026, 10, 21, 17, 5, tzinfo=UTC)


def test_unknown_abbreviation_is_not_guessed() -> None:
    text, moment = parse_created_at("Completed Oct 20, 2026 2:00 AM XYZT", CATALOGUE["pattern_created_at"], LA)
    assert text is not None and moment is None


def test_status_classification() -> None:
    assert row("Completed Oct 21, 2026 10:05 AM PDT Today Download", True).status == "ready"
    assert row("Processing Oct 21, 2026 10:05 AM PDT Today -").status == "pending"
    assert row("Failed Oct 21, 2026 10:05 AM PDT Today -").status == "failed"


# -- identifying this execution's run -------------------------------------------


def test_old_download_on_top_is_never_used() -> None:
    """The stale-download case: nothing new yet, but the top row offers Download."""
    match = identify_new_run([OLD_DONE, OLD_DONE_2], [OLD_DONE, OLD_DONE_2], TRIGGER)
    assert match.state == "not-started" and match.row is None


def test_new_pending_then_ready() -> None:
    pending = row("Processing Oct 21, 2026 10:05 AM PDT Yesterday -")
    ready = row("Completed Oct 21, 2026 10:05 AM PDT Yesterday – Download", True)
    assert identify_new_run([OLD_DONE], [pending, OLD_DONE], TRIGGER).state == "pending"
    match = identify_new_run([OLD_DONE], [ready, OLD_DONE], TRIGGER)
    assert match.state == "ready" and match.row.created_text == "Oct 21, 2026 10:05 AM PDT"


def test_old_run_finishing_is_not_mistaken_for_ours() -> None:
    old_pending = row("Processing Oct 21, 2026 9:00 AM PDT Yesterday -")
    old_finished = row("Completed Oct 21, 2026 9:00 AM PDT Yesterday Download", True)
    match = identify_new_run([old_pending], [old_finished], TRIGGER)
    assert match.state == "not-started"


def test_older_row_scrolling_into_view_is_ignored() -> None:
    older = row("Completed Oct 1, 2026 1:00 AM PDT Yesterday Download", True, 2)
    assert identify_new_run([OLD_DONE], [OLD_DONE, older], TRIGGER).state == "not-started"


def test_clock_skew_tolerance() -> None:
    # Amazon's clock one minute behind ours still counts (default skew 120s)...
    early = row("Processing Oct 21, 2026 10:04 AM PDT Yesterday -")
    assert identify_new_run([OLD_DONE], [early, OLD_DONE], TRIGGER).state == "pending"
    # ...but a run from 10 minutes earlier does not.
    stale = row("Completed Oct 21, 2026 9:55 AM PDT Yesterday Download", True)
    assert identify_new_run([OLD_DONE], [stale, OLD_DONE], TRIGGER, skew_seconds=120).state == "not-started"


def test_two_new_runs_fail_closed() -> None:
    a = row("Processing Oct 21, 2026 10:05 AM PDT Yesterday -")
    b = row("Processing Oct 21, 2026 10:06 AM PDT Yesterday -", index=1)
    with pytest.raises(StaleReportError, match="2 new runs"):
        identify_new_run([OLD_DONE], [b, a, OLD_DONE], TRIGGER)


def test_same_minute_collision_fails_closed() -> None:
    previous = row("Completed Oct 21, 2026 10:05 AM PDT Yesterday Download", True)
    new = row("Processing Oct 21, 2026 10:05 AM PDT Yesterday -")
    with pytest.raises(StaleReportError, match="cannot be told apart"):
        identify_new_run([previous], [new, previous], TRIGGER)


def test_unreadable_new_row_fails_closed() -> None:
    weird = row("Processing just now Yesterday -")
    with pytest.raises(StaleReportError, match="could not be read"):
        identify_new_run([OLD_DONE], [weird, OLD_DONE], TRIGGER)


# -- polling -------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_poll_returns_when_ready() -> None:
    clock = FakeClock()
    states = iter(["not-started", "pending", "ready"])
    reloads = []
    match, polls = poll_until_ready(
        lambda: browser.RunMatch(next(states), None), lambda: reloads.append(1),
        timeout_seconds=600, interval_seconds=30, clock=clock, sleep=clock.sleep,
    )
    assert (match.state, polls, len(reloads)) == ("ready", 3, 2)


def test_poll_times_out_while_pending() -> None:
    clock = FakeClock()
    with pytest.raises(ReportNotReadyError, match="never changed from '-' to Download"):
        poll_until_ready(lambda: browser.RunMatch("pending", None), lambda: None,
                         timeout_seconds=100, interval_seconds=30, clock=clock, sleep=clock.sleep)
    assert clock.now <= 100


def test_poll_stops_on_failed_run() -> None:
    clock = FakeClock()
    with pytest.raises(ReportNotReadyError, match="failed"):
        poll_until_ready(lambda: browser.RunMatch("failed", None), lambda: None,
                         timeout_seconds=100, interval_seconds=30, clock=clock, sleep=clock.sleep)


# -- download handling ---------------------------------------------------------


def test_validate_download(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="missing"):
        validate_download(tmp_path / "nope.csv")
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(DownloadError, match="empty"):
        validate_download(empty)
    html = tmp_path / "page.csv"
    html.write_bytes(b"<!DOCTYPE html><html></html>")
    with pytest.raises(DownloadError, match="HTML"):
        validate_download(html)
    ok = tmp_path / "ok.csv"
    ok.write_bytes(b"Date\nOct 21, 2026\n")
    assert validate_download(ok) == ok.stat().st_size


class _Nothing:
    """A locator/page stand-in that finds nothing and does nothing."""

    def __init__(self) -> None:
        self.listeners = []

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False

    def wait_for_timeout(self, ms):
        pass

    def on(self, event, fn):
        self.listeners.append((event, fn))

    def remove_listener(self, event, fn):
        self.listeners.remove((event, fn))

    def click(self):
        pass


class FakeSession:
    def __init__(self) -> None:
        self.selectors = CATALOGUE
        self.page = _Nothing()
        self.context = _Nothing()
        self.failures = []

    def capture_failure(self, label):
        self.failures.append(label)
        return {}


def choice() -> ReportChoice:
    return ReportChoice(date(2026, 10, 21), "22222222-2222-2222-2222-222222222222", "yesterday", None, date(2026, 10, 21))


def test_missing_download_control(settings) -> None:
    session = FakeSession()
    page = ReportHistoryPage(session, settings, choice())
    ours = row("Completed Oct 21, 2026 10:05 AM PDT Yesterday Download", True)
    with pytest.raises(DownloadError, match="no Download control"):
        page.download(browser.RunMatch("ready", ours), TRIGGER, 3)
    assert session.failures == ["download-control-missing"]


def test_download_timeout(settings) -> None:
    from dataclasses import replace

    session = FakeSession()
    page = ReportHistoryPage(session, replace(settings, download_timeout_ms=50), choice())
    with pytest.raises(DownloadError, match="no file arrived"):
        page._capture_download(_Nothing(), "x.csv")
    assert session.page.listeners == []  # listeners cleaned up
    assert session.failures == ["download-timeout"]


# -- retries -------------------------------------------------------------------


def test_transient_failures_are_retried(settings) -> None:
    attempts = []

    def flaky(s, c):
        attempts.append(1)
        if len(attempts) == 1:
            raise NavigationError("timeout loading page")
        return "ok"

    assert retrieve_with_retries(settings, choice(), retrieve=flaky, sleep=lambda s: None) == "ok"
    assert len(attempts) == 2


@pytest.mark.parametrize("error", [AuthenticationError("expired"), ReportNotReadyError("slow"), StaleReportError("x")])
def test_non_transient_failures_are_not_retried(settings, error) -> None:
    attempts = []

    def failing(s, c):
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        retrieve_with_retries(settings, choice(), retrieve=failing, sleep=lambda s: None)
    assert len(attempts) == 1


def test_safe_url_strips_tokens() -> None:
    assert safe_url("https://www.amazon.com/ap/signin?openid.return_to=x&ssoResponse=SECRET") == \
        "https://www.amazon.com/ap/signin?<redacted>"


def test_selector_file_is_complete() -> None:
    for key in browser.REQUIRED_SELECTOR_KEYS:
        assert CATALOGUE[key]
