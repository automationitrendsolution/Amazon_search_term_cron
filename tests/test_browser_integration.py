"""Real Playwright + Chromium against a LOCAL mock of the report-history page.

The mock reproduces the structure seen on the live Amazon Ads page (AG Grid
rows, an 'Actions' menu with Run/Copy/Delete, 'Processing' + '-' while pending,
'Completed' + a Download link when done).  It proves the browser code paths
work end to end; it does NOT prove the selectors match Amazon's current UI --
that needs ``run.py --inspect-report`` against the real account.

Skipped automatically when Chromium is not installed.
"""

from __future__ import annotations

import html
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from app import browser
from app.dates import ReportChoice
from app.exceptions import AuthenticationError, ReportNotReadyError

LA = ZoneInfo("America/Los_Angeles")


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed"),
]


class MockAmazon:
    """Server-side state for the mock history page."""

    def __init__(self, *, polls_until_ready: int = 2, run_works: bool = True, signed_in: bool = True) -> None:
        old = datetime.now(LA) - timedelta(days=1)
        self.rows = [{"created": old, "remaining": 0, "csv": "OLD STALE REPORT"}]
        self.polls_until_ready = polls_until_ready
        self.run_works = run_works
        self.signed_in = signed_in
        self.downloads: list[int] = []

    def page(self) -> str:
        if not self.signed_in:
            return "<html><body><form name='signIn'><input id='ap_email'></form></body></html>"
        rows = []
        for i, r in enumerate(sorted(self.rows, key=lambda r: r["created"], reverse=True)):
            idx = self.rows.index(r)
            if r["remaining"] > 0:
                r["remaining"] -= 1
                status, action = "Processing", "-"
            else:
                status, action = "Completed", f"<a href='/download/{idx}'>Download</a>"
            created = r["created"].strftime("%b %d, %Y %I:%M %p %Z").replace(" 0", " ")
            rows.append(
                f"<div class='ag-row' role='row'><div role='gridcell'>{status}</div>"
                f"<div role='gridcell'>{html.escape(created)}</div><div role='gridcell'>Yesterday</div>"
                f"<div role='gridcell'>{action}</div></div>"
            )
        return f"""<html><body>
<button id='actions' onclick="document.getElementById('menu').style.display='block'">Actions</button>
<div id='menu' role='menu' style='display:none'>
  <div role='menuitem' tabindex='0' onclick="fetch('/run',{{method:'POST'}});this.parentNode.style.display='none'">Run</div>
  <div role='menuitem' tabindex='0'>Copy</div><div role='menuitem' tabindex='0'>Delete</div>
</div>
<div role='grid'><div class='ag-center-cols-container'>{''.join(rows)}</div></div>
</body></html>"""

    def run(self) -> None:
        if self.run_works:
            self.rows.append({"created": datetime.now(LA), "remaining": self.polls_until_ready, "csv": "NEW REPORT"})


@pytest.fixture
def mock_amazon():
    state = {"mock": MockAmazon()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def do_GET(self):
            mock = state["mock"]
            if self.path.startswith("/download/"):
                idx = int(self.path.rsplit("/", 1)[1])
                mock.downloads.append(idx)
                body = mock.rows[idx]["csv"].encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=report.csv")
            else:
                body = mock.page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["mock"].run()
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


@pytest.fixture
def mock_settings(settings, mock_amazon):
    return replace(
        settings,
        history_url_template=mock_amazon["url"] + "/reporting/history?entityId={entity_id}&reportId={report_id}",
        amazon_account_timezone=LA,
        poll_interval_seconds=1,
        report_ready_timeout_seconds=20,
        browser_timeout_ms=10_000,
        download_timeout_ms=10_000,
        run_clock_skew_seconds=60,
    )


def choice() -> ReportChoice:
    today = datetime.now(LA).date()
    return ReportChoice(today, "22222222-2222-2222-2222-222222222222", "today", None, today)


def test_triggers_waits_and_downloads_only_the_new_run(mock_settings, mock_amazon) -> None:
    result = browser.retrieve_report(mock_settings, choice())
    assert result.path.read_text() == "NEW REPORT"  # never the stale row's file
    assert result.path.parent == mock_settings.download_dir
    assert mock_amazon["mock"].downloads == [1]
    assert result.polls >= 2
    assert (mock_settings.state_dir / "last_run.json").exists()


def test_no_new_run_times_out_without_downloading(mock_settings, mock_amazon) -> None:
    mock_amazon["mock"] = MockAmazon(run_works=False)
    with pytest.raises(ReportNotReadyError):
        browser.retrieve_report(replace(mock_settings, report_ready_timeout_seconds=4), choice())
    assert mock_amazon["mock"].downloads == []  # the old Download link was never clicked
    assert list(mock_settings.download_dir.glob("*.csv")) == []


def test_expired_session_stops_with_authentication_error(mock_settings, mock_amazon) -> None:
    mock_amazon["mock"] = MockAmazon(signed_in=False)
    with pytest.raises(AuthenticationError):
        browser.retrieve_report(mock_settings, choice())
    assert list(mock_settings.screenshot_dir.glob("*sign-in-required*.png"))
