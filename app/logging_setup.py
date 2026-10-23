"""Logging: console + rotating file, with workflow context and secret redaction.

Every record carries ``stage``, ``report_date``, ``file`` and ``rows`` fields
taken from a context variable, so a line in the cron log says where in the
workflow it came from without each call site repeating it.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("log_context", default={})

FORMAT = (
    "%(asctime)s %(levelname)-7s [%(stage)s] date=%(report_date)s file=%(file)s "
    "rows=%(rows)s %(name)s: %(message)s"
)

#: Query strings (Amazon SSO tokens, signed download URLs) and URL userinfo.
_URL_QUERY = re.compile(r"(https?://[^\s?#'\"]+)\?[^\s'\"]*")
_URL_USERINFO = re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"stc.{name}")


def set_context(**fields: object) -> None:
    current = dict(_CONTEXT.get())
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = str(value)
    _CONTEXT.set(current)


@contextmanager
def stage(name: str, **fields: object) -> Iterator[None]:
    """Run a block with ``stage`` (and any extra fields) set in the log context."""
    token = _CONTEXT.set({**_CONTEXT.get(), "stage": name, **{k: str(v) for k, v in fields.items()}})
    try:
        yield
    except BaseException as exc:
        # Remember where it failed (innermost stage wins) for the final log line.
        if not hasattr(exc, "stage"):
            try:
                exc.stage = name  # type: ignore[attr-defined]
            except AttributeError:  # pragma: no cover - exceptions with __slots__
                pass
        raise
    finally:
        _CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CONTEXT.get()
        record.stage = ctx.get("stage", "-")
        record.report_date = ctx.get("report_date", "-")
        record.file = ctx.get("file", "-")
        record.rows = ctx.get("rows", "-")
        return True


class RedactingFilter(logging.Filter):
    """Strip secrets, URL query strings and URL credentials from messages."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        text = _URL_USERINFO.sub(r"\1***:***@", text)
        return _URL_QUERY.sub(r"\1?<redacted>", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        redacted = self.redact(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        record.msg, record.args = redacted, None
        return True


class ConsoleFormatter(logging.Formatter):
    """Compact terminal lines: ``HH:MM:SS  stage     message`` (the file log keeps every field)."""

    _LEVEL_TAG = {"WARNING": "WARN ", "ERROR": "ERROR", "CRITICAL": "ERROR"}
    _LEVEL_COLOR = {"WARNING": "\x1b[33m", "ERROR": "\x1b[31m", "CRITICAL": "\x1b[31;1m"}
    _DIM, _RESET = "\x1b[2m", "\x1b[0m"

    def __init__(self, color: bool = False) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stage_name = getattr(record, "stage", "-")
        tag = self._LEVEL_TAG.get(record.levelname, "     ")
        prefix = f"{self.formatTime(record, '%H:%M:%S')}  {tag} {'' if stage_name == '-' else stage_name:<9} "
        message = record.getMessage()
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"
        indent = "\n" + " " * len(prefix)
        lines = message.splitlines() or [""]
        body = lines[0] + "".join(indent + line for line in lines[1:])
        if not self.color:
            return prefix + body
        color = self._LEVEL_COLOR.get(record.levelname)
        if color:
            return f"{color}{prefix}{body}{self._RESET}"
        return f"{self._DIM}{prefix}{self._RESET}{body}"


def console_supports_color(stream: object) -> bool:
    """ANSI colours only on an interactive terminal (never in cron/Task Scheduler logs)."""
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name == "nt":  # enable VT processing on the Windows console
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backup_count: int = 10,
    secrets: Sequence[str] = (),
) -> None:
    root = logging.getLogger("stc")
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(FORMAT)
    filters: list[logging.Filter] = [ContextFilter(), RedactingFilter(secrets)]

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter(color=console_supports_color(sys.stderr)))
    handlers: list[logging.Handler] = [console]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "app.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        )
    except OSError as exc:  # pragma: no cover - unwritable log dir
        print(f"WARNING: file logging disabled: {exc}", file=sys.stderr)
    for handler in handlers:
        if handler.formatter is None:
            handler.setFormatter(formatter)
        for flt in filters:
            handler.addFilter(flt)
        root.addHandler(handler)
