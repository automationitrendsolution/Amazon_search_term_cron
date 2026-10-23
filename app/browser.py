"""Playwright workflow for the Amazon Ads Search Term report.

Workflow (:func:`retrieve_report`):

1. launch Chromium on the persistent, already-signed-in profile;
2. open ``reporting/history?entityId=...&reportId=...`` for the saved Search
   Term report chosen for the selected date (Today / Yesterday definition);
3. confirm the session is authenticated and the history grid rendered;
4. snapshot every visible run row, record the trigger time, choose
   Actions -> Run;
5. reload every REPORT_POLL_INTERVAL_SECONDS and look for the run *this
   execution* created -- a row that was not in the snapshot and whose
   "Date created" is at or after the trigger time;
6. when that row (and only that row) offers Download, download it into
   ``data/downloads/`` under a unique name and check it is non-empty/readable.

Stale-download protection: a Download link is only ever taken from the row
matched in step 5.  Older rows -- even ones with a Download link on top of
the grid -- are never used.  If the new run cannot be told apart from older
ones (unreadable "Date created", two new rows, same-minute collision) the
run fails closed with :class:`StaleReportError`.

Every UI locator comes from ``config/selectors.json``.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .auth import assert_authenticated, is_sign_in_page
from .config import Settings
from .dates import ReportChoice
from .exceptions import (
    AuthenticationError,
    BrowserError,
    ConfigurationError,
    DownloadError,
    NavigationError,
    ReportNotReadyError,
    StaleReportError,
)
from .logging_setup import get_logger, set_context
from .storage import download_filename, safe_name, unique_path, utc_stamp, write_state

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Download, Locator, Page, Playwright

logger = get_logger("browser")


# ---------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _load_selector_file(path: str, mtime: float) -> dict[str, tuple[str, ...]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Selector file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Selector file {path} is not valid JSON: {exc}") from exc
    catalogue: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise ConfigurationError(f"Selector key {key!r} must be a non-empty list of strings")
        if key.startswith("pattern_"):
            for pattern in value:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigurationError(f"{key}: invalid regular expression {pattern!r}: {exc}") from exc
        catalogue[key] = tuple(value)
    return catalogue


REQUIRED_SELECTOR_KEYS = (
    "sign_in_marker",
    "security_challenge_marker",
    "report_error_marker",
    "report_history_loaded_marker",
    "report_not_found_marker",
    "report_actions_menu",
    "run_report_menu_item",
    "run_report_button",
    "run_report_confirm_button",
    "history_row",
    "history_row_download",
    "loading_indicator",
    "pattern_created_at",
    "pattern_status_ready",
    "pattern_status_failed",
)


def load_selectors(path: Path) -> dict[str, tuple[str, ...]]:
    """Load and validate ``config/selectors.json`` (comment keys ignored)."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Selector file not found: {path}") from exc
    catalogue = _load_selector_file(str(path), mtime)
    missing = [k for k in REQUIRED_SELECTOR_KEYS if k not in catalogue]
    if missing:
        raise ConfigurationError(f"{path} is missing selector key(s): {missing}")
    return catalogue


# ---------------------------------------------------------------------------
# pure logic: row parsing, new-run identification, polling (unit-tested)
# ---------------------------------------------------------------------------

#: Abbreviation -> fixed UTC offset.  The abbreviation already says whether
#: DST applies, so a fixed offset is exact.  Unknown abbreviations fail closed.
TZ_ABBREVIATIONS: dict[str, timezone] = {
    "UTC": timezone.utc,
    "GMT": timezone.utc,
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "IST": timezone(timedelta(hours=5, minutes=30)),
    "BST": timezone(timedelta(hours=1)),
    "CET": timezone(timedelta(hours=1)),
    "CEST": timezone(timedelta(hours=2)),
    "JST": timezone(timedelta(hours=9)),
    "AEST": timezone(timedelta(hours=10)),
    "AEDT": timezone(timedelta(hours=11)),
}

_TS_FORMATS = (
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M:%S %p",
    "%b %d, %Y, %I:%M:%S %p",
    "%B %d, %Y %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M:%S %p",
    "%B %d, %Y, %I:%M:%S %p",
)


def parse_created_at(
    row_text: str, patterns: Sequence[str], fallback_tz: ZoneInfo
) -> tuple[str | None, datetime | None]:
    """Find the "Date created" timestamp in a row's text.

    Returns ``(matched text, aware UTC datetime)``; the datetime is None when
    the text is missing or cannot be interpreted unambiguously.  Without a
    timezone abbreviation the browser's timezone (``fallback_tz``, which this
    module sets as Chromium's timezone) applies.
    """
    for pattern in patterns:
        match = re.search(pattern, row_text)
        if not match:
            continue
        stamp = " ".join(match.group("ts").replace(".", "").split())
        stamp = re.sub(r"(\d)([AaPp][Mm])$", r"\1 \2", stamp).replace("Sept ", "Sep ")
        abbreviation = match.groupdict().get("tz")
        matched = match.group(0).strip()
        for fmt in _TS_FORMATS:
            try:
                naive = datetime.strptime(stamp, fmt)  # %b and %p are case-insensitive
                break
            except ValueError:
                continue
        else:
            return matched, None
        if abbreviation:
            zone = TZ_ABBREVIATIONS.get(abbreviation)
            if zone is None:
                return matched, None
            return matched, naive.replace(tzinfo=zone).astimezone(UTC)
        return matched, naive.replace(tzinfo=fallback_tz).astimezone(UTC)
    return None, None


def classify_status(row_text: str, has_download: bool, catalogue: dict[str, tuple[str, ...]]) -> str:
    """'failed', 'ready' (needs a Download control) or 'pending'."""
    if any(re.search(p, row_text, re.IGNORECASE) for p in catalogue["pattern_status_failed"]):
        return "failed"
    if has_download:
        return "ready"
    return "pending"


@dataclass(frozen=True)
class HistoryRow:
    index: int
    text: str
    created_text: str | None
    created_at: datetime | None
    status: str
    has_download: bool

    @property
    def key(self) -> str:
        """Identity of a run that survives status changes (Processing -> Completed)."""
        return self.created_text or self.text


def make_row(
    index: int, text: str, has_download: bool, catalogue: dict[str, tuple[str, ...]], fallback_tz: ZoneInfo
) -> HistoryRow:
    clean = " ".join(text.split())
    created_text, created_at = parse_created_at(clean, catalogue["pattern_created_at"], fallback_tz)
    return HistoryRow(
        index=index,
        text=clean[:300],
        created_text=created_text,
        created_at=created_at,
        status=classify_status(clean, has_download, catalogue),
        has_download=has_download,
    )


@dataclass(frozen=True)
class RunMatch:
    #: not-started | pending | ready | failed
    state: str
    row: HistoryRow | None
    detail: str = ""


def identify_new_run(
    before: Sequence[HistoryRow],
    now: Sequence[HistoryRow],
    triggered_at: datetime,
    *,
    skew_seconds: int = 120,
) -> RunMatch:
    """Find the run this execution started among the rows visible ``now``.

    A row is ours only if (a) its identity (Date created text) was not in the
    pre-trigger snapshot -- compared as a multiset, so status changes on old
    rows do not count -- and (b) its Date created is no earlier than the
    trigger time, floored to the minute (the grid shows minutes), minus
    ``skew_seconds`` for clock differences.

    Raises:
        StaleReportError: the new run cannot be identified with confidence.
    """
    remaining = Counter(row.key for row in before)
    before_keys = set(remaining)
    fresh: list[HistoryRow] = []
    for row in now:
        if remaining[row.key] > 0:
            remaining[row.key] -= 1
        else:
            fresh.append(row)

    floor = triggered_at.astimezone(UTC).replace(second=0, microsecond=0) - timedelta(seconds=skew_seconds)
    recent: list[HistoryRow] = []
    for row in fresh:
        if row.created_at is None:
            raise StaleReportError(
                f"A new history row appeared but its 'Date created' could not be read ({row.text!r}). "
                "Cannot prove it is this execution's run, so nothing is downloaded. Check "
                "pattern_created_at in config/selectors.json."
            )
        if row.created_at >= floor:
            recent.append(row)
        else:
            logger.debug("Ignoring older row that scrolled into view: %s", row.text)

    if not recent:
        return RunMatch("not-started", None, f"{len(now)} row(s), none created since {floor.isoformat()}")
    if len(recent) > 1:
        raise StaleReportError(
            f"{len(recent)} new runs appeared since the trigger ({[r.text for r in recent]}); "
            "cannot tell which one this execution started. Failing closed."
        )
    row = recent[0]
    if row.key in before_keys:
        raise StaleReportError(
            f"The new run shares its 'Date created' ({row.key!r}) with an older run, so the two "
            "cannot be told apart. Failing closed; the next scheduled run will retry."
        )
    return RunMatch(row.status, row, row.text)


def poll_until_ready(
    probe: Callable[[], RunMatch],
    reload: Callable[[], None],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[RunMatch, int]:
    """Reload and probe until our run is READY.  Returns (match, polls).

    Raises:
        ReportNotReadyError: Amazon marked the run failed, or it never became
            downloadable within ``timeout_seconds``.
        StaleReportError: propagated from :func:`identify_new_run`.
    """
    deadline = clock() + timeout_seconds
    polls = 0
    last = RunMatch("not-started", None)
    while True:
        polls += 1
        last = probe()
        logger.info("Status poll %d: %s %s", polls, last.state, last.detail)
        if last.state == "ready":
            return last, polls
        if last.state == "failed":
            raise ReportNotReadyError(f"Amazon reported the run as failed: {last.detail}")
        if clock() + interval_seconds > deadline:
            raise ReportNotReadyError(
                f"The report run was still '{last.state}' after {timeout_seconds:.0f}s ({polls} polls); "
                "it never changed from '-' to Download. Nothing was downloaded. "
                "Raise REPORT_READY_TIMEOUT_SECONDS if Amazon is slow."
            )
        sleep(interval_seconds)
        reload()


# ---------------------------------------------------------------------------
# download validation
# ---------------------------------------------------------------------------


def validate_download(path: Path) -> int:
    """The file must exist, be non-empty and readable.  Returns its size."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DownloadError(f"Downloaded file {path} is missing: {exc}") from exc
    if size == 0:
        raise DownloadError(f"Downloaded file {path.name} is empty (0 bytes)")
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        raise DownloadError(f"Downloaded file {path.name} is not readable: {exc}") from exc
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").lower().startswith((b"<!doctype", b"<html")):
        raise DownloadError(f"Downloaded file {path.name} is an HTML page, not a report")
    return size


def safe_url(url: str) -> str:
    """Scheme/host/path only: Amazon puts SSO tokens and signatures in query strings."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    trimmed = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{trimmed}?<redacted>" if parts.query else trimmed


# ---------------------------------------------------------------------------
# browser session
# ---------------------------------------------------------------------------


class BrowserSession:
    """Persistent Chromium context.  Use as a context manager so Chromium always exits."""

    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        self.selectors = load_selectors(settings.selector_file)
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc is not None and not isinstance(exc, (AuthenticationError, ReportNotReadyError, StaleReportError)):
            self.capture_failure("unhandled-error")
        self.close()

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self.settings.ensure_dirs()
        timezone_id = self.settings.amazon_account_timezone.key
        logger.info("Launching Chromium (headless=%s, timezone=%s)", self.headless, timezone_id)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.session_dir),
                headless=self.headless,
                accept_downloads=True,
                downloads_path=str(self.settings.download_dir),
                args=list(self.settings.browser_args),
                user_agent=self.settings.user_agent,
                viewport={"width": 1600, "height": 1000},
                locale="en-US",
                # "Date created" is rendered in the browser's zone; pinning it
                # makes the run-identification arithmetic deterministic.
                timezone_id=timezone_id,
            )
        except Exception as exc:
            self.close()
            raise BrowserError(
                f"Could not launch Chromium: {exc}. Run 'uv run playwright install chromium' "
                "(and 'sudo uv run playwright install-deps chromium' on Ubuntu). If the profile is "
                "locked, make sure no other Chromium uses data/session."
            ) from exc
        self._context.set_default_timeout(self.settings.browser_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error closing the browser: %s", exc)
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
            self._playwright = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("Browser session is not started (or Chromium crashed)")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserError("Browser session is not started (or Chromium crashed)")
        return self._context

    def find_first(self, key: str, *, timeout_ms: int = 2_500, within: Locator | None = None) -> Locator | None:
        """First visible candidate for ``key`` within ``timeout_ms`` (whole search), else None."""
        deadline = time.monotonic() + timeout_ms / 1000
        root = within if within is not None else self.page
        while True:
            for selector in self.selectors[key]:
                try:
                    locator = root.locator(selector).first
                    if locator.is_visible():
                        return locator
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(250)

    def screenshot(self, label: str) -> Path | None:
        if self._page is None:
            return None
        path = unique_path(self.settings.screenshot_dir, f"{utc_stamp()}_{safe_name(label)}.png")
        try:
            self._page.screenshot(path=str(path), full_page=True)
            logger.info("Screenshot saved: %s", path)
            return path
        except Exception as exc:  # pragma: no cover - page may be dead
            logger.warning("Could not capture screenshot %s: %s", label, exc)
            return None

    def capture_failure(self, label: str) -> dict[str, str]:
        """Screenshot + redacted URL + title.  Page HTML and cookies are never saved."""
        details: dict[str, str] = {}
        if self._page is None:
            return details
        try:
            details["url"] = safe_url(self._page.url)
            details["title"] = self._page.title()
        except Exception:
            pass
        shot = self.screenshot(label)
        if shot:
            details["screenshot"] = str(shot)
        logger.error("Browser diagnostics (%s): %s", label, details)
        return details


# ---------------------------------------------------------------------------
# report history page
# ---------------------------------------------------------------------------


@dataclass
class DownloadedReport:
    path: Path
    size_bytes: int
    suggested_filename: str
    report_id: str
    period: str
    report_date: str
    triggered_at: str
    run_created: str | None
    run_row_text: str
    polls: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ReportHistoryPage:
    def __init__(self, session: BrowserSession, settings: Settings, choice: ReportChoice) -> None:
        self.session = session
        self.settings = settings
        self.choice = choice

    @property
    def page(self) -> Page:
        return self.session.page

    # -- navigation ----------------------------------------------------------

    def _navigate(self, action: Callable[[], object], what: str) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            action()
        except PlaywrightTimeoutError as exc:
            self.session.capture_failure("navigation-timeout")
            raise NavigationError(f"Timed out {what}") from exc
        except PlaywrightError as exc:
            # The history page rewrites its own URL while booting; Playwright
            # reports that as an interrupted navigation.  Follow it.
            if "interrupted by another navigation" not in str(exc):
                self.session.capture_failure("navigation-failed")
                raise NavigationError(f"Failed {what}: {str(exc).splitlines()[0]}") from exc
            try:
                self.page.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeoutError as wait_exc:
                raise NavigationError(f"Timed out after a redirect while {what}") from wait_exc

    def open(self) -> None:
        url = self.settings.history_url(self.choice.report_id)
        logger.info("Opening report history for report %s (%s)", self.choice.report_id, self.choice.period)
        self._navigate(lambda: self.page.goto(url, wait_until="domcontentloaded"), "loading report history")
        assert_authenticated(self.session)
        self.wait_until_loaded()
        self.assert_report_exists()

    def reload(self) -> None:
        self._navigate(lambda: self.page.reload(wait_until="domcontentloaded"), "reloading report history")
        assert_authenticated(self.session)
        self.wait_until_loaded()

    def wait_until_loaded(self) -> None:
        deadline = time.monotonic() + self.settings.browser_timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.session.find_first("report_error_marker", timeout_ms=0):
                self.session.capture_failure("amazon-error-panel")
                raise NavigationError(
                    "Amazon showed 'Sorry, something went wrong' instead of the report history. "
                    "Check AMAZON_ENTITY_ID and that this login can open the report."
                )
            if is_sign_in_page(self.page, self.session.selectors):
                assert_authenticated(self.session)
            if self.session.find_first("report_history_loaded_marker", timeout_ms=0):
                self._wait_for_idle()
                return
            self.page.wait_for_timeout(500)
        self.session.capture_failure("history-not-loaded")
        raise NavigationError(
            f"The report history did not render within {self.settings.browser_timeout_ms} ms "
            f"at {safe_url(self.page.url)} (selector key 'report_history_loaded_marker')."
        )

    def _wait_for_idle(self) -> None:
        for selector in self.session.selectors["loading_indicator"]:
            try:
                self.page.locator(selector).first.wait_for(state="hidden", timeout=15_000)
            except Exception:
                continue
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            logger.debug("networkidle not reached; continuing")
        self.page.wait_for_timeout(1_000)  # AG Grid renders rows after the data request

    def assert_report_exists(self) -> None:
        if self.session.find_first("report_not_found_marker", timeout_ms=1_000):
            self.session.capture_failure("report-not-found")
            raise ConfigurationError(f"Amazon does not recognise report {self.choice.report_id}")
        expected = self.choice.expected_name
        if expected:
            body = self.page.inner_text("body")
            if expected.lower() not in body.lower():
                self.session.capture_failure("report-name-mismatch")
                raise ConfigurationError(
                    f"Report page does not show the expected name {expected!r}; refusing to run a "
                    "report that may be the wrong one (check REPORT_ID_* / REPORT_NAME_*)."
                )

    # -- rows ----------------------------------------------------------------

    def _row_locator(self) -> Locator | None:
        for selector in self.session.selectors["history_row"]:
            try:
                locator = self.page.locator(selector)
                if locator.count():
                    return locator
            except Exception:
                continue
        return None

    def _download_control(self, row: Locator) -> Locator | None:
        for selector in self.session.selectors["history_row_download"]:
            try:
                control = row.locator(selector).first
                if control.count() and control.is_visible():
                    return control
            except Exception:
                continue
        return None

    def read_rows(self) -> list[HistoryRow]:
        locator = self._row_locator()
        if locator is None:
            return []
        rows: list[HistoryRow] = []
        for i in range(min(locator.count(), 100)):
            item = locator.nth(i)
            try:
                text = item.inner_text(timeout=2_000)
            except Exception:
                continue
            rows.append(
                make_row(
                    i,
                    text,
                    self._download_control(item) is not None,
                    self.session.selectors,
                    self.settings.amazon_account_timezone,
                )
            )
        return rows

    # -- run -----------------------------------------------------------------

    def trigger_run(self) -> tuple[list[HistoryRow], datetime]:
        """Snapshot the grid, then Actions -> Run.  Returns (snapshot, trigger time UTC)."""
        before = self.read_rows()
        logger.info("History before run: %d row(s); newest: %s", len(before), before[0].text if before else "-")
        control = self.session.find_first("run_report_button", timeout_ms=1_000)
        if control is None:
            menu = self.session.find_first("report_actions_menu", timeout_ms=5_000)
            if menu is None:
                self.session.capture_failure("actions-menu-missing")
                raise NavigationError("The 'Actions' menu was not found (selector key 'report_actions_menu')")
            menu.click()
            control = self.session.find_first("run_report_menu_item", timeout_ms=5_000)
            if control is None:
                self.page.keyboard.press("Escape")
                self.session.capture_failure("run-menu-item-missing")
                raise NavigationError("No 'Run' entry in the Actions menu (selector key 'run_report_menu_item')")
        disabled = (control.get_attribute("aria-disabled") or "").lower() == "true"
        if disabled or not control.is_enabled():
            self.page.keyboard.press("Escape")
            self.session.capture_failure("run-disabled")
            raise NavigationError("The Run action is disabled (a run may already be in progress); not guessing")

        triggered_at = datetime.now(tz=UTC)
        control.click()
        confirm = self.session.find_first("run_report_confirm_button", timeout_ms=2_000)
        if confirm is not None:
            confirm.click()
            logger.info("Confirmed the run dialog")
        logger.info("Run triggered at %s for report %s", triggered_at.isoformat(), self.choice.report_id)
        return before, triggered_at

    def wait_for_new_run(self, before: list[HistoryRow], triggered_at: datetime) -> tuple[RunMatch, int]:
        def probe() -> RunMatch:
            return identify_new_run(
                before, self.read_rows(), triggered_at, skew_seconds=self.settings.run_clock_skew_seconds
            )

        self.page.wait_for_timeout(3_000)
        self.reload()
        return poll_until_ready(
            probe,
            self.reload,
            timeout_seconds=self.settings.report_ready_timeout_seconds,
            interval_seconds=self.settings.poll_interval_seconds,
        )

    # -- download ------------------------------------------------------------

    def download(self, match: RunMatch, triggered_at: datetime, polls: int) -> DownloadedReport:
        assert match.row is not None
        locator = self._row_locator()
        # Re-locate OUR row by identity right before clicking: never by position alone.
        target: Locator | None = None
        if locator is not None:
            hits = []
            for i in range(min(locator.count(), 100)):
                try:
                    text = " ".join(locator.nth(i).inner_text(timeout=2_000).split())
                except Exception:
                    continue
                if match.row.created_text and match.row.created_text in text:
                    hits.append(locator.nth(i))
            if len(hits) == 1:
                target = hits[0]
            elif len(hits) > 1:
                raise StaleReportError("Matched run identity appears in more than one row; failing closed")
        control = self._download_control(target) if target is not None else None
        if control is None:
            self.session.capture_failure("download-control-missing")
            raise DownloadError("This execution's run shows no Download control (selector key 'history_row_download')")

        filename = download_filename(self.choice.report_date.isoformat(), self.choice.period, self.choice.report_id)
        path = self._capture_download(control, filename)
        size = validate_download(path)
        set_context(file=path.name)
        logger.info("Downloaded %s (%d bytes)", path.name, size)
        return DownloadedReport(
            path=path,
            size_bytes=size,
            suggested_filename=self._last_suggested,
            report_id=self.choice.report_id,
            period=self.choice.period,
            report_date=self.choice.report_date.isoformat(),
            triggered_at=triggered_at.isoformat(),
            run_created=match.row.created_text,
            run_row_text=match.row.text,
            polls=polls,
        )

    _last_suggested = ""

    def _capture_download(self, control: Locator, filename: str) -> Path:
        captured: list[Download] = []
        popups: list[Page] = []

        def on_download(download: Download) -> None:
            captured.append(download)

        def on_page(page: Page) -> None:
            popups.append(page)
            page.on("download", on_download)

        context = self.session.context
        self.page.on("download", on_download)
        context.on("page", on_page)
        try:
            control.click()
            deadline = time.monotonic() + self.settings.download_timeout_ms / 1000
            while not captured and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
        finally:
            self.page.remove_listener("download", on_download)
            context.remove_listener("page", on_page)
        for popup in popups:
            try:
                popup.close()
            except Exception:
                pass

        if not captured:
            self.session.capture_failure("download-timeout")
            raise DownloadError(f"Clicked Download but no file arrived within {self.settings.download_timeout_ms} ms")
        download = captured[0]
        failure = download.failure()
        if failure:
            raise DownloadError(f"Browser reported the download failed: {failure}")
        self._last_suggested = download.suggested_filename or ""
        if self._last_suggested and not self._last_suggested.lower().endswith(".csv"):
            logger.warning("Amazon suggested %r (not .csv); content is validated next", self._last_suggested)
        target = unique_path(self.settings.download_dir, filename)
        try:
            download.save_as(str(target))
        except Exception as exc:
            raise DownloadError(f"Could not save the download to {target}: {exc}") from exc
        return target


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def retrieve_report(settings: Settings, choice: ReportChoice) -> DownloadedReport:
    """Run the full browser workflow once; see the module docstring."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        with BrowserSession(settings) as session:
            history = ReportHistoryPage(session, settings, choice)
            history.open()
            before, triggered_at = history.trigger_run()
            _record_run(settings, choice, triggered_at, before, status="triggered")
            match, polls = history.wait_for_new_run(before, triggered_at)
            report = history.download(match, triggered_at, polls)
            _record_run(settings, choice, triggered_at, before, status="downloaded", report=report)
            return report
    except PlaywrightError as exc:  # browser crash, target closed, protocol error
        raise BrowserError(f"Browser failure: {str(exc).splitlines()[0]}") from exc


def retrieve_with_retries(
    settings: Settings,
    choice: ReportChoice,
    *,
    retrieve: Callable[[Settings, ReportChoice], DownloadedReport] = retrieve_report,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadedReport:
    """Retry transient browser failures (navigation, download, crash).

    Not retried: authentication (needs a person), stale-run ambiguity (fail
    closed) and report-not-ready (the polling budget is already spent).
    """
    attempts = 1 + settings.browser_retries
    for attempt in range(1, attempts + 1):
        try:
            return retrieve(settings, choice)
        except (AuthenticationError, StaleReportError, ReportNotReadyError, ConfigurationError):
            raise
        except BrowserError as exc:
            if attempt >= attempts:
                raise
            logger.warning("Browser attempt %d/%d failed (%s); retrying in 30s", attempt, attempts, exc)
            sleep(30)
    raise AssertionError("unreachable")  # pragma: no cover


def _record_run(
    settings: Settings,
    choice: ReportChoice,
    triggered_at: datetime,
    before: list[HistoryRow],
    *,
    status: str,
    report: DownloadedReport | None = None,
) -> None:
    """Non-secret metadata of the run this execution started (data/state/last_run.json)."""
    data: dict[str, Any] = {
        "status": status,
        "report_id": choice.report_id,
        "period": choice.period,
        "report_date": choice.report_date.isoformat(),
        "account_today": choice.account_today.isoformat(),
        "triggered_at_utc": triggered_at.isoformat(),
        "rows_before_trigger": [r.key for r in before[:20]],
    }
    if report is not None:
        data.update({k: v for k, v in asdict(report).items() if k != "metadata"})
        data["path"] = str(report.path)
    try:
        write_state(settings.state_dir / "last_run.json", data)
    except OSError as exc:  # pragma: no cover
        logger.warning("Could not write last_run.json: %s", exc)


def inspect_report_page(settings: Settings, choice: ReportChoice) -> dict[str, Any]:
    """Read-only selector check against the live page.  Clicks nothing but opens/closes Actions."""
    with BrowserSession(settings) as session:
        history = ReportHistoryPage(session, settings, choice)
        history._navigate(
            lambda: session.page.goto(settings.history_url(choice.report_id), wait_until="domcontentloaded"),
            "loading report history",
        )
        assert_authenticated(session)
        try:
            session.page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        session.page.wait_for_timeout(3_000)
        result: dict[str, Any] = {"url": safe_url(session.page.url), "selectors": {}}
        for key, candidates in session.selectors.items():
            if key.startswith("pattern_"):
                continue
            found = []
            for selector in candidates:
                try:
                    loc = session.page.locator(selector)
                    found.append({"selector": selector, "matches": loc.count(), "visible": loc.count() > 0 and loc.first.is_visible()})
                except Exception as exc:
                    found.append({"selector": selector, "error": type(exc).__name__})
            result["selectors"][key] = found
        rows = history.read_rows()
        result["rows"] = [
            {"text": r.text, "created": r.created_text, "created_utc": r.created_at.isoformat() if r.created_at else None,
             "status": r.status, "has_download": r.has_download}
            for r in rows[:10]
        ]
        menu = session.find_first("report_actions_menu", timeout_ms=3_000)
        if menu is not None:
            menu.click()
            session.page.wait_for_timeout(1_000)
            result["run_menu_item_found"] = session.find_first("run_report_menu_item", timeout_ms=2_000) is not None
            session.page.keyboard.press("Escape")
        result["screenshot"] = str(session.screenshot("inspect-report") or "")
        return result
