"""--login on a server without a display: credentials from .env, code typed by the person."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import auth
from app.exceptions import AuthenticationError, ConfigurationError

ADS = "https://advertising.amazon.com/reporting"
SIGN_IN = "https://www.amazon.com/ap/signin?x=1"


class FakeField:
    def __init__(self, log: list, name: str, on_click=None) -> None:
        self.log, self.name, self.on_click = log, name, on_click

    def fill(self, value: str) -> None:
        self.log.append((self.name, value))

    def check(self) -> None:
        self.log.append((self.name, "checked"))

    def click(self) -> None:
        self.log.append((self.name, "click"))
        if self.on_click:
            self.on_click()

    def inner_text(self, timeout: int = 0) -> str:
        return "Your password is incorrect"


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url

    def goto(self, url: str, wait_until: str = "") -> None:
        if self.url != SIGN_IN:
            self.url = url

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        pass

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        return "A text message with a code has been sent to your phone ending in 42"

    def locator(self, selector: str):
        raise RuntimeError("not used")


class FakeSession:
    """Amazon's sign-in pages as a tiny state machine."""

    def __init__(self, *, otp_codes_accepted: set[str], signed_in: bool = False, reject_password: bool = False):
        self.log: list = []
        self.page = FakePage(ADS if signed_in else SIGN_IN)
        self.selectors = {"sign_in_marker": (), "security_challenge_marker": ()}
        self.state = "done" if signed_in else "email"
        self.accepted = otp_codes_accepted
        self.reject_password = reject_password
        self.last_code: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def capture_failure(self, label: str) -> dict:
        self.log.append(("failure", label))
        return {}

    def _to(self, state: str) -> None:
        self.state = state
        if state == "done":
            self.page.url = ADS

    def _submit_code(self) -> None:
        if self.last_code in self.accepted:
            self._to("done")

    def find_first(self, key: str, *, timeout_ms: int = 0):
        s = self.state
        if key == "signin_email_input" and s == "email":
            return FakeField(self.log, "email")
        if key == "signin_continue_button" and s == "email":
            return FakeField(self.log, "continue", lambda: self._to("password"))
        if key == "signin_password_input" and s == "password":
            return FakeField(self.log, "password")
        if key == "signin_submit_button" and s == "password":
            return FakeField(self.log, "sign-in", lambda: self._to("rejected" if self.reject_password else "otp"))
        if key == "signin_error_message" and s == "rejected":
            return FakeField(self.log, "error")
        if key == "otp_input" and s == "otp":
            field = FakeField(self.log, "otp")
            field.fill = lambda value: (self.log.append(("otp", value)), setattr(self, "last_code", value))
            return field
        if key == "otp_remember_device" and s == "otp":
            return FakeField(self.log, "remember-device")
        if key == "otp_submit_button" and s == "otp":
            return FakeField(self.log, "otp-submit", self._submit_code)
        return None


@pytest.fixture
def creds(settings):
    return replace(settings, amazon_email="buyer@example.com", amazon_password="s3cret!")


def run_login(settings, session, answers):
    replies = iter(answers)
    said: list[str] = []
    auth.terminal_login(
        settings,
        session_factory=lambda *_a, **_k: session,
        prompt=lambda _text: next(replies),
        announce=said.append,
        sleep=lambda _s: None,
    )
    return said


def test_signs_in_with_env_credentials_and_typed_code(creds) -> None:
    session = FakeSession(otp_codes_accepted={"123456"})
    said = run_login(creds, session, ["123 456"])
    assert ("email", "buyer@example.com") in session.log
    assert ("password", "s3cret!") in session.log
    assert ("otp", "123456") in session.log  # spaces removed
    assert ("remember-device", "checked") in session.log
    assert "  Code accepted." in said
    assert any("ending in 42" in line for line in said)


def test_wrong_code_is_retried_then_accepted(creds) -> None:
    session = FakeSession(otp_codes_accepted={"222222"})
    run_login(creds, session, ["111111", "", "222222"])
    assert [v for k, v in session.log if k == "otp"] == ["111111", "222222"]


def test_gives_up_after_three_wrong_codes(creds) -> None:
    session = FakeSession(otp_codes_accepted=set())
    with pytest.raises(AuthenticationError, match="not accepted after 3"):
        run_login(creds, session, ["1", "2", "3"])
    assert ("failure", "otp-rejected") in session.log


def test_rejected_password_is_not_retried(creds) -> None:
    session = FakeSession(otp_codes_accepted=set(), reject_password=True)
    with pytest.raises(AuthenticationError, match="rejected the sign-in"):
        run_login(creds, session, [])
    assert sum(1 for k, _ in session.log if k == "sign-in") == 1


def test_missing_credentials_explain_what_to_set(settings) -> None:
    with pytest.raises(ConfigurationError, match="AMAZON_EMAIL and AMAZON_PASSWORD"):
        run_login(settings, FakeSession(otp_codes_accepted=set()), [])


def test_already_signed_in_needs_nothing(settings) -> None:
    session = FakeSession(otp_codes_accepted=set(), signed_in=True)
    run_login(settings, session, [])
    assert not any(k in ("email", "password", "otp") for k, _ in session.log)


def test_no_terminal_input_stops_cleanly() -> None:
    def no_input(_text):
        raise EOFError

    with pytest.raises(AuthenticationError, match="interactive shell"):
        auth.read_otp(no_input, lambda _t: None, 1)


def test_password_is_redacted_from_logs(monkeypatch) -> None:
    from app.config import secret_values

    monkeypatch.setenv("AMAZON_PASSWORD", "s3cret!")
    assert "s3cret!" in secret_values()


def test_email_is_masked() -> None:
    assert auth._mask_email("buyer@example.com") == "b***r@example.com"
