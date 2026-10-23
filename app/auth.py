"""Amazon Ads authentication: session checks and interactive login.

Security boundary: scheduled runs never sign in.  They only *reuse* the
persistent browser profile (``data/session``) that a person signed in to with
``run.py --login``; when that session has expired, or Amazon shows any
challenge, the run stops with exit code 3.

``--login`` with a display: a visible browser, the person does everything.
``--login`` without a display (a server): a hidden browser fills
AMAZON_EMAIL / AMAZON_PASSWORD from .env and the person types the two-step
code in the terminal.  No code is ever generated or guessed, an image CAPTCHA
stops the login, and cookies are never read or logged.
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
        terminal_login(settings, session_factory=session_factory, sleep=sleep, clock=clock)
        return
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


# ---------------------------------------------------------------------------
# --login on a server without a display: hidden browser, OTP typed in the terminal
# ---------------------------------------------------------------------------

#: Amazon invalidates a code after a few tries; stopping first avoids locking the account.
MAX_OTP_ATTEMPTS = 3


def _print_to_terminal(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def terminal_login(
    settings: Settings,
    *,
    session_factory: Callable[..., BrowserSession] | None = None,
    prompt: Callable[[str], str] = input,
    announce: Callable[[str], None] = _print_to_terminal,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Sign in with a hidden browser: email/password from .env, two-step code from the person.

    Only ``run.py --login`` reaches this, with a person at the keyboard.  It never
    generates or guesses a code and stops on an image CAPTCHA.  Scheduled runs
    still never sign in (they stop with exit 3).
    """
    if session_factory is None:
        from .browser import BrowserSession

        session_factory = BrowserSession

    with session_factory(settings, headless=True) as session:
        page = session.page
        logger.info("No display: signing in with a hidden browser (two-step code typed here)")
        try:
            page.goto(settings.reporting_url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Initial navigation did not finish (%s); continuing", exc)
        _settle(page)

        if is_sign_in_page(page, session.selectors):
            if not (settings.amazon_email and settings.amazon_password):
                raise ConfigurationError(
                    "No display, so --login signs in from the terminal and needs AMAZON_EMAIL and "
                    "AMAZON_PASSWORD in .env (or connect with 'ssh -X' / MobaXterm for a visible browser)."
                )
            logger.info("Signing in to Amazon as %s", _mask_email(settings.amazon_email))
            _submit_email(session, settings)
            _submit_password(session, settings)
            _stop_on_captcha(session)
            _answer_otp_if_asked(session, prompt=prompt, announce=announce)
        else:
            logger.info("The saved session is still signed in; no sign-in form shown")

        _wait_for_reporting_page(session, settings, sleep=sleep, clock=clock, timeout_seconds=90)
        assert_authenticated(session)
        sleep(5)  # let Chromium flush cookies to the profile
        logger.info("Signed in; the session is stored in the persistent profile %s", settings.session_dir)


def _wait_for_reporting_page(session, settings: Settings, *, sleep, clock, timeout_seconds: int) -> None:
    deadline = clock() + timeout_seconds
    stable = 0
    last_reason: str | None = None
    while clock() < deadline:
        try:
            reason = not_signed_in_reason(session.page, session.selectors)
        except Exception as exc:
            reason = f"check failed: {type(exc).__name__}"
        if reason != last_reason:
            logger.info("Sign-in check: %s", reason or "signed in, confirming")
            last_reason = reason
        stable = stable + 1 if reason is None else 0
        if stable >= 3:
            break
        sleep(2)
    else:
        session.capture_failure("login-not-completed")
        raise AuthenticationError(
            f"Amazon Ads did not open after sign-in ({last_reason}). A screenshot was saved in "
            "data/screenshots/. Nothing else was changed; run --login again."
        )
    if "/reporting" not in session.page.url:
        try:
            session.page.goto(settings.reporting_url, wait_until="domcontentloaded")
            sleep(3)
        except Exception as exc:
            logger.warning("Could not open the reporting page after sign-in: %s", exc)


def _submit_email(session, settings: Settings) -> None:
    """Fill the email and continue.  Absent when Amazon shows email and password together."""
    field = session.find_first("signin_email_input", timeout_ms=5_000)
    if field is None:
        logger.debug("No email field; assuming the password step")
        return
    field.fill(settings.amazon_email or "")
    button = session.find_first("signin_continue_button", timeout_ms=5_000)
    if button is None:
        return
    button.click()
    _settle(session.page)
    _assert_credentials_accepted(session)


def _submit_password(session, settings: Settings) -> None:
    field = session.find_first("signin_password_input", timeout_ms=10_000)
    if field is None:
        _stop_on_captcha(session)
        session.capture_failure("signin-password-field-missing")
        raise AuthenticationError(
            "The password field never appeared. Amazon may have changed the form "
            "(config/selectors.json 'signin_password_input')."
        )
    field.fill(settings.amazon_password or "")
    remember = session.find_first("signin_remember_me", timeout_ms=1_500)
    if remember is not None:
        try:
            remember.check()
        except Exception:
            pass
    submit = session.find_first("signin_submit_button", timeout_ms=5_000)
    if submit is None:
        session.capture_failure("signin-submit-missing")
        raise AuthenticationError("Could not find the sign-in button (config/selectors.json 'signin_submit_button').")
    submit.click()
    _settle(session.page)
    _assert_credentials_accepted(session)


def _assert_credentials_accepted(session) -> None:
    """Wrong credentials are never retried: repeating them can lock the account."""
    banner = session.find_first("signin_error_message", timeout_ms=2_000)
    if banner is None:
        return
    try:
        message = banner.inner_text(timeout=2_000).strip()
    except Exception:
        message = "(could not read the message)"
    session.capture_failure("signin-rejected")
    raise AuthenticationError(f"Amazon rejected the sign-in: {message[:300]} Check AMAZON_EMAIL and AMAZON_PASSWORD.")


def _stop_on_captcha(session) -> None:
    if session.find_first("captcha_marker", timeout_ms=1_500) is None:
        return
    session.capture_failure("signin-captcha")
    raise AuthenticationError(
        "Amazon is showing an image CAPTCHA, which cannot be answered from a terminal. "
        "Sign in once with a visible browser ('ssh -X' / MobaXterm), then scheduled runs reuse it."
    )


def _answer_otp_if_asked(session, *, prompt: Callable[[str], str], announce: Callable[[str], None]) -> None:
    """Fill Amazon's two-step code, read from the person at the terminal."""
    field = session.find_first("otp_input", timeout_ms=4_000)
    if field is None:
        logger.info("Amazon did not ask for a two-step code")
        return
    announce("")
    announce("=" * 72)
    announce("Amazon is asking for a two-step verification code.")
    announce(_otp_destination(session.page))
    announce("Type it below. It is not stored or logged.")
    announce("=" * 72)
    for attempt in range(1, MAX_OTP_ATTEMPTS + 1):
        field.fill(read_otp(prompt, announce, attempt))
        # Ticking this is what lets the scheduled runs reuse the session without a code.
        remember = session.find_first("otp_remember_device", timeout_ms=1_500)
        if remember is not None:
            try:
                remember.check()
                logger.info("Ticked 'Don't require code on this browser'")
            except Exception:
                pass
        submit = session.find_first("otp_submit_button", timeout_ms=4_000)
        if submit is None:
            session.capture_failure("otp-submit-missing")
            raise AuthenticationError("The code box has no submit button (config/selectors.json 'otp_submit_button').")
        submit.click()
        _settle(session.page)
        field = session.find_first("otp_input", timeout_ms=3_000)
        if field is None:
            announce("  Code accepted.")
            return
        announce("  Amazon did not accept that code. Check for a newer one.")
    session.capture_failure("otp-rejected")
    raise AuthenticationError(
        f"The two-step code was not accepted after {MAX_OTP_ATTEMPTS} attempts. Stopping to avoid "
        "locking the account; request a fresh code and run --login again."
    )


def read_otp(prompt: Callable[[str], str], announce: Callable[[str], None], attempt: int) -> str:
    """Read a code from the terminal; spaces and dashes are ignored."""
    while True:
        try:
            raw = prompt(f"  Verification code (attempt {attempt}/{MAX_OTP_ATTEMPTS}): ")
        except EOFError as exc:
            raise AuthenticationError("No terminal input available; run --login from an interactive shell.") from exc
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            return digits
        announce("  That was empty. Type the digits Amazon sent, e.g. 123456.")


def _otp_destination(page: Page) -> str:
    """Amazon's own line saying where it sent the code, if shown."""
    try:
        text = page.inner_text("body", timeout=2_000)
    except Exception:
        return "Check the phone or app registered on the account."
    for line in (ln.strip() for ln in text.splitlines()):
        low = line.lower()
        if "code" in low and ("sent" in low or "ending in" in low or "authenticator" in low):
            return line[:200]
    return "Check the phone or app registered on the account."


def _settle(page: Page) -> None:
    """Wait for the navigation a form submission triggers (never raises)."""
    for state in ("domcontentloaded", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=15_000)
        except Exception:
            pass


def _mask_email(email: str) -> str:
    """someone@example.com -> s*****e@example.com"""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"
