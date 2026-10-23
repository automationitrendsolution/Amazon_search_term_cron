"""Amazon Ads authentication: session checks and interactive login.

Security boundary: this module never types a password, never answers a
CAPTCHA, OTP or MFA prompt, and never reads or logs cookies.  The scheduled
run only *reuses* the persistent browser profile (``data/session``) that a
person signed in to with ``run.py --login``.  When that session has expired,
or Amazon shows any challenge, the run stops with exit code 3.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import Settings
from .exceptions import AuthenticationError, ConfigurationError
from .logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

    from .browser import BrowserSession

logger = get_logger("auth")

SIGN_IN_URL_MARKERS = ("/ap/signin", "/ap/mfa", "/ap/cvf", "/ap/challenge", "/ap/register")

Selectors = Mapping[str, Sequence[str]]


def any_visible(page: Page, selectors: Sequence[str]) -> bool:
    """True when any selector currently matches a visible element.  Never raises."""
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible():
                return True
        except Exception:  # a malformed selector must not break the check
            continue
    return False


def is_sign_in_page(page: Page, selectors: Selectors) -> bool:
    path = page.url.lower().split("?", 1)[0]
    if any(marker in path for marker in SIGN_IN_URL_MARKERS):
        return True
    return any_visible(page, selectors.get("sign_in_marker", ()))


def has_security_challenge(page: Page, selectors: Selectors) -> bool:
    return any_visible(page, selectors.get("security_challenge_marker", ()))


def assert_authenticated(session: BrowserSession) -> None:
    """Stop (AuthenticationError) on a sign-in page or a security challenge."""
    page = session.page
    if has_security_challenge(page, session.selectors):
        session.capture_failure("security-challenge")
        raise AuthenticationError(
            "Amazon is showing a security challenge (CAPTCHA/OTP/MFA). It is never answered "
            "automatically. A person must run: uv run python run.py --login"
        )
    if is_sign_in_page(page, session.selectors):
        session.capture_failure("sign-in-required")
        raise AuthenticationError(
            "The saved Amazon Ads session is missing or expired (sign-in page shown). "
            "Re-authenticate with: uv run python run.py --login"
        )


def not_signed_in_reason(page: Page, selectors: Selectors) -> str | None:
    """Why the page does not count as a signed-in Ads page, or None when it does."""
    host = urlsplit(page.url).hostname or ""
    if not host.lower().startswith("advertising.amazon."):
        return f"host {host or '(none)'} is not advertising.amazon.*"
    path = page.url.lower().split("?", 1)[0]
    if any(marker in path for marker in SIGN_IN_URL_MARKERS):
        return "sign-in URL"
    for key in ("sign_in_marker", "security_challenge_marker"):
        for selector in selectors.get(key, ()):
            try:
                if page.locator(selector).first.is_visible():
                    return f"{key} matched {selector!r}"
            except Exception:
                continue
    return None


def is_authenticated_reporting_page(page: Page, selectors: Selectors) -> bool:
    """Signed in and on advertising.amazon.com (not an /ap/ sign-in path)."""
    return not_signed_in_reason(page, selectors) is None


def _current_page(session: BrowserSession) -> Page:
    """The session's page, or the newest open tab if it was closed or replaced."""
    page = session.page
    if not page.is_closed():
        return page
    open_pages = [p for p in session.context.pages if not p.is_closed()]
    if not open_pages:
        raise AuthenticationError("The browser window was closed before sign-in finished. Nothing was saved.")
    session._page = open_pages[-1]
    logger.info("Original tab closed; following the newest open tab")
    return session._page


def interactive_login(
    settings: Settings,
    *,
    session_factory: Callable[..., BrowserSession] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Open a visible browser on the reporting page and wait for a person to sign in.

    Success = the page stays on advertising.amazon.com, without sign-in or
    challenge markers, for three consecutive checks.  The persistent profile
    then holds the session for headless runs.
    """
    if os.name == "posix" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise ConfigurationError(
            "--login needs a visible browser but no DISPLAY is set. Use 'ssh -X' (X11 "
            "forwarding), a VNC/remote desktop session, or sign in on a desktop and copy "
            "data/session (see README, 'Amazon Ads login')."
        )
    if session_factory is None:
        from .browser import BrowserSession

        session_factory = BrowserSession

    with session_factory(settings, headless=False) as session:
        page = session.page
        logger.info("Opening %s for interactive sign-in", settings.reporting_url)
        try:
            page.goto(settings.reporting_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Initial navigation did not finish (%s); continue in the browser window", exc)
        print(
            "\nA browser window is open. Sign in to Amazon Ads and complete any CAPTCHA, OTP or\n"
            "two-step verification yourself (tick \"Don't require code on this browser\" if offered).\n"
            "This command finishes on its own once the Amazon Ads reporting page is showing.\n",
            file=sys.stderr,
        )
        deadline = clock() + settings.login_timeout_seconds
        stable = 0
        last_reason: str | None = None
        while clock() < deadline:
            try:
                page = _current_page(session)
                reason = not_signed_in_reason(page, session.selectors)
            except AuthenticationError:
                raise
            except Exception as exc:  # page navigating mid-check
                reason = f"check failed: {type(exc).__name__}: {exc}"
            if reason != last_reason:
                logger.info("Sign-in check: %s", reason or "signed in, confirming")
                last_reason = reason
            ok = reason is None
            stable = stable + 1 if ok else 0
            if stable >= 3:
                break
            sleep(2)
        else:
            session.capture_failure("login-timeout")
            raise AuthenticationError(
                f"Sign-in was not completed within {settings.login_timeout_seconds}s "
                "(LOGIN_TIMEOUT_SECONDS). Nothing was saved."
            )
        if "/reporting" not in page.url:
            try:
                page.goto(settings.reporting_url, wait_until="domcontentloaded")
                sleep(3)
            except Exception as exc:
                logger.warning("Could not open the reporting page after sign-in: %s", exc)
        assert_authenticated(session)
        # Give Chromium a moment to flush cookies/local storage to the profile.
        sleep(5)
        logger.info("Signed in; the session is stored in the persistent profile %s", settings.session_dir)
