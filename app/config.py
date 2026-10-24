"""Settings: environment variables (``.env``), paths, timezones, polling and DB.

Values come from the process environment first and ``.env`` second (exported
variables win, so cron or systemd can override a file value).  Nothing in here
is ever logged with its secret: :meth:`DatabaseSettings.safe_description` is
the only form of the connection that leaves this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_SELECTOR_FILE = PROJECT_ROOT / "config" / "selectors.json"

REPORTING_URL = "https://advertising.amazon.com/reporting"
DEFAULT_HISTORY_URL_TEMPLATE = (
    "https://advertising.amazon.com/reporting/history?entityId={entity_id}&reportId={report_id}"
)

#: Amazon report IDs are UUIDs (seen in reporting/history?reportId=...).
REPORT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_IDENTIFIER_OK = re.compile(r"^[^`\x00]{1,64}$")


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _require(name: str) -> str:
    value = _env(name)
    if value is None:
        raise ConfigurationError(f"{name} is not set (see .env.example)")
    return value


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true/false, got {raw!r}")


def _zone(name: str, default: str) -> ZoneInfo:
    raw = _env(name, default) or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(f"{name}={raw!r} is not a valid IANA timezone") from exc


def _cutoff(name: str, default: str) -> time:
    raw = _env(name, default) or default
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ConfigurationError(f"{name} must be HH:MM (24h), got {raw!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _path(name: str, default: str) -> Path:
    raw = Path(_env(name, default) or default)
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def _report_id(name: str) -> str | None:
    raw = _env(name)
    if raw is None:
        return None
    if not REPORT_ID_PATTERN.match(raw):
        raise ConfigurationError(f"{name}={raw!r} is not an Amazon report ID (UUID)")
    return raw.lower()


# ---------------------------------------------------------------------------
# settings objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    table: str
    connect_timeout: int = 30
    #: Format used when the destination Date column is a text column.
    text_date_format: str = "%Y-%m-%d"
    #: Optional MySQL session time_zone (matters only for TIMESTAMP columns).
    session_time_zone: str | None = None
    ssl_ca: str | None = None
    ssl_disabled: bool = False

    def safe_description(self) -> str:
        """Connection target without the password -- the only loggable form."""
        return f"mysql://{self.user}@{self.host}:{self.port}/{self.name} table=`{self.table}`"

    def connect_args(self) -> dict[str, object]:
        args: dict[str, object] = {
            "connection_timeout": self.connect_timeout,
            "use_pure": True,
        }
        if self.ssl_disabled:
            args["ssl_disabled"] = True
        elif self.ssl_ca:
            args["ssl_ca"] = self.ssl_ca
        return args


@dataclass(frozen=True)
class Settings:
    # report selection
    report_timezone: ZoneInfo
    cutoff: time
    amazon_account_timezone: ZoneInfo
    report_id_today: str | None
    report_id_yesterday: str | None
    report_name_today: str | None
    report_name_yesterday: str | None
    entity_id: str | None
    history_url_template: str
    reporting_url: str

    # browser
    headless: bool
    user_agent: str | None
    browser_timeout_ms: int
    download_timeout_ms: int
    poll_interval_seconds: int
    report_ready_timeout_seconds: int
    run_clock_skew_seconds: int
    login_timeout_seconds: int
    browser_no_sandbox: bool
    browser_retries: int

    # csv
    csv_date_format: str | None
    allow_empty_report: bool
    duplicate_policy: str

    # db
    db_max_attempts: int

    # paths
    download_dir: Path
    processed_dir: Path
    failed_dir: Path
    screenshot_dir: Path
    session_dir: Path
    state_dir: Path
    log_dir: Path
    selector_file: Path
    lock_file: Path

    # logging
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    #: "cutoff": one report per run, chosen by TODAY_REPORT_CUTOFF.
    #: "both": every run imports the Yesterday report, then the Today report.
    report_mode: str = "cutoff"

    #: Only for 'run.py --login' on a host without a display (the person types
    #: the two-step code).  Scheduled runs never sign in.
    amazon_email: str | None = None
    amazon_password: str | None = None

    #: The calendar Amazon uses for a saved report's "Today"/"Yesterday" (the
    #: user's timezone).  None = same as amazon_account_timezone.
    report_period_timezone: ZoneInfo | None = None

    @property
    def period_tz(self) -> ZoneInfo:
        return self.report_period_timezone or self.amazon_account_timezone

    @property
    def required_dirs(self) -> tuple[Path, ...]:
        return (
            self.download_dir,
            self.processed_dir,
            self.failed_dir,
            self.screenshot_dir,
            self.session_dir,
            self.state_dir,
            self.log_dir,
        )

    def ensure_dirs(self) -> None:
        """Create every runtime directory; lock down the browser profile."""
        for path in self.required_dirs:
            path.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.session_dir, 0o700)
        restrict_permissions(self.state_dir, 0o700)

    def history_url(self, report_id: str) -> str:
        if "{entity_id}" in self.history_url_template and not self.entity_id:
            raise ConfigurationError(
                "AMAZON_ENTITY_ID is not set. Copy the entityId=... value from the report's "
                "URL in Amazon Ads; without it the console opens under 'All accounts'."
            )
        return self.history_url_template.format(entity_id=self.entity_id or "", report_id=report_id)

    @property
    def browser_args(self) -> tuple[str, ...]:
        return ("--no-sandbox",) if self.browser_no_sandbox else ()


def restrict_permissions(path: Path, mode: int) -> None:
    """chmod on POSIX; a no-op on Windows (where ACLs apply instead)."""
    if os.name != "posix":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def load_env(env_file: Path | None = None) -> None:
    """Read ``.env`` without overriding variables that are already exported."""
    path = env_file or DEFAULT_ENV_FILE
    if path.exists():
        load_dotenv(path, override=False)


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment.  Call :func:`load_env` first."""
    duplicate_policy = (_env("DUPLICATE_POLICY", "warn") or "warn").lower()
    if duplicate_policy not in {"warn", "fail"}:
        raise ConfigurationError("DUPLICATE_POLICY must be 'warn' or 'fail'")
    log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigurationError(f"LOG_LEVEL {log_level!r} is not DEBUG/INFO/WARNING/ERROR")
    report_mode = (_env("REPORT_MODE", "cutoff") or "cutoff").lower()
    if report_mode not in {"cutoff", "both"}:
        raise ConfigurationError("REPORT_MODE must be 'cutoff' or 'both'")
    template = _env("REPORT_HISTORY_URL_TEMPLATE", DEFAULT_HISTORY_URL_TEMPLATE) or ""
    if "{report_id}" not in template:
        raise ConfigurationError("REPORT_HISTORY_URL_TEMPLATE must contain {report_id}")

    return Settings(
        report_mode=report_mode,
        # Observed live: Amazon resolves Today/Yesterday in the user's timezone (IST),
        # so it defaults to REPORT_TIMEZONE rather than the account's US timezone.
        report_period_timezone=_zone("REPORT_PERIOD_TIMEZONE", _env("REPORT_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"),
        amazon_email=_env("AMAZON_EMAIL"),
        amazon_password=_env("AMAZON_PASSWORD"),
        report_timezone=_zone("REPORT_TIMEZONE", "Asia/Kolkata"),
        cutoff=_cutoff("TODAY_REPORT_CUTOFF", "12:30"),
        amazon_account_timezone=_zone("AMAZON_ACCOUNT_TIMEZONE", "Asia/Kolkata"),
        report_id_today=_report_id("REPORT_ID_TODAY"),
        report_id_yesterday=_report_id("REPORT_ID_YESTERDAY"),
        report_name_today=_env("REPORT_NAME_TODAY"),
        report_name_yesterday=_env("REPORT_NAME_YESTERDAY"),
        entity_id=_env("AMAZON_ENTITY_ID"),
        history_url_template=template,
        reporting_url=_env("REPORTING_URL", REPORTING_URL) or REPORTING_URL,
        headless=_bool("HEADLESS", True),
        user_agent=_env("BROWSER_USER_AGENT"),
        browser_timeout_ms=_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        download_timeout_ms=_int("DOWNLOAD_TIMEOUT_MS", 180_000, minimum=1_000),
        poll_interval_seconds=_int("REPORT_POLL_INTERVAL_SECONDS", 30, minimum=1),
        report_ready_timeout_seconds=_int("REPORT_READY_TIMEOUT_SECONDS", 1_200, minimum=10),
        run_clock_skew_seconds=_int("RUN_CLOCK_SKEW_SECONDS", 120, minimum=0),
        login_timeout_seconds=_int("LOGIN_TIMEOUT_SECONDS", 900, minimum=30),
        browser_no_sandbox=_bool("BROWSER_NO_SANDBOX", False),
        browser_retries=_int("BROWSER_RETRIES", 1, minimum=0),
        csv_date_format=_env("CSV_DATE_FORMAT"),
        allow_empty_report=_bool("ALLOW_EMPTY_REPORT", False),
        duplicate_policy=duplicate_policy,
        db_max_attempts=_int("DB_MAX_ATTEMPTS", 2, minimum=1),
        download_dir=_path("DOWNLOAD_DIR", "data/downloads"),
        processed_dir=_path("PROCESSED_DIR", "data/processed"),
        failed_dir=_path("FAILED_DIR", "data/failed"),
        screenshot_dir=_path("SCREENSHOT_DIR", "data/screenshots"),
        session_dir=_path("SESSION_DIR", "data/session"),
        state_dir=_path("STATE_DIR", "data/state"),
        log_dir=_path("LOG_DIR", "logs"),
        selector_file=_path("SELECTOR_FILE", "config/selectors.json"),
        lock_file=_path("LOCK_FILE", "data/state/run.lock"),
        log_level=log_level,
        log_max_bytes=_int("LOG_MAX_BYTES", 5_000_000, minimum=10_000),
        log_backup_count=_int("LOG_BACKUP_COUNT", 10, minimum=1),
    )


def load_database_settings() -> DatabaseSettings:
    """DB settings; only needed by modes that touch MySQL."""
    name = _require("DB_NAME")
    table = _require("REPORT_TABLE")
    for label, value in (("DB_NAME", name), ("REPORT_TABLE", table)):
        if not _IDENTIFIER_OK.match(value):
            raise ConfigurationError(f"{label} is not a valid MySQL identifier")
    return DatabaseSettings(
        host=_require("DB_HOST"),
        port=_int("DB_PORT", 3306, minimum=1),
        name=name,
        user=_require("DB_USER"),
        password=_env("DB_PASSWORD", "") or "",
        table=table,
        connect_timeout=_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        text_date_format=_env("DB_TEXT_DATE_FORMAT", "%Y-%m-%d") or "%Y-%m-%d",
        session_time_zone=_env("DB_SESSION_TIME_ZONE"),
        ssl_ca=_env("DB_SSL_CA"),
        ssl_disabled=_bool("DB_SSL_DISABLED", False),
    )


def secret_values() -> list[str]:
    """Values the log redaction filter must never let through."""
    secrets = []
    for name in ("DB_PASSWORD", "AMAZON_PASSWORD"):
        value = os.environ.get(name)
        if value and len(value) >= 3:
            secrets.append(value)
    return secrets


def env_file_is_private(path: Path | None = None) -> bool | None:
    """True when ``.env`` is not group/world readable (POSIX only)."""
    target = path or DEFAULT_ENV_FILE
    if os.name != "posix" or not target.exists():
        return None
    return (target.stat().st_mode & 0o077) == 0
