"""Run lock, recovery journal and report-file management.

* :class:`RunLock` -- an OS file lock held for the whole run, in addition to
  cron's ``flock -n``, so a manual run cannot overlap the scheduled one.
* :class:`ImportJournal` -- ``data/state/import_journal.json``: what the last
  import was doing, so a crash between DELETE and verification is resolved by
  checking the table on the next run instead of guessing.  It holds dates,
  counts, totals, file names and hashes only -- never credentials or cookies.
* file helpers -- unique names, move-never-overwrite, failure sidecars.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO

from .exceptions import ArchiveError, LockError
from .logging_setup import get_logger

logger = get_logger("storage")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")


def safe_name(text: str, fallback: str = "file") -> str:
    return _SAFE.sub("-", text).strip("-.") or fallback


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


class RunLock:
    """Non-blocking exclusive lock on ``path`` (fcntl on Linux, msvcrt on Windows)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise LockError(
                f"Another run holds {self.path}; not starting a second one (exit without changes)"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={datetime.now(tz=UTC).isoformat()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - closing releases it anyway
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------

#: Journal states, in order.  Anything but ``archived``/``resolved`` is "pending".
STATES = ("started", "committed", "commit-unknown", "verify-failed", "verified", "archive-failed", "archived")
PENDING_STATES = frozenset({"started", "committed", "commit-unknown", "verify-failed"})


class ImportJournal:
    """Atomic JSON record of the in-flight import plus a short history."""

    HISTORY = 50

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Import journal %s unreadable (%s); starting a new one", self.path, exc)
            self._quarantine()
        return {"current": None, "history": []}

    def _quarantine(self) -> None:
        try:
            self.path.replace(self.path.with_suffix(f".corrupt-{utc_stamp()}.json"))
        except OSError:
            pass

    def _write(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def current(self) -> dict[str, object] | None:
        entry = self.read().get("current")
        return entry if isinstance(entry, dict) else None

    def pending(self) -> dict[str, object] | None:
        entry = self.current
        return entry if entry and entry.get("status") in PENDING_STATES else None

    def begin(self, entry: dict[str, object]) -> None:
        data = self.read()
        entry = {**entry, "status": "started", "updated_at": datetime.now(tz=UTC).isoformat()}
        data["current"] = entry
        self._write(data)

    def update(self, status: str, **fields: object) -> None:
        if status not in STATES and status != "resolved":
            raise ValueError(f"unknown journal status {status!r}")
        data = self.read()
        entry = dict(data.get("current") or {})
        entry.update(fields)
        entry["status"] = status
        entry["updated_at"] = datetime.now(tz=UTC).isoformat()
        data["current"] = entry
        if status in {"archived", "resolved", "archive-failed"}:
            history = list(data.get("history") or [])
            history.append(entry)
            data["history"] = history[-self.HISTORY :]
        self._write(data)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unique_path(directory: Path, filename: str) -> Path:
    """``directory/filename``, suffixed ``-1``, ``-2``... so nothing is overwritten."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def download_filename(report_date: str, period: str, report_id: str) -> str:
    return f"{utc_stamp()}_{safe_name(report_date)}_{safe_name(period)}_{report_id[:8]}_search_term.csv"


def move_file(
    source: Path, directory: Path, *, mover: Callable[[str, str], object] = shutil.move
) -> Path:
    """Move ``source`` into ``directory`` without overwriting.  Raises ArchiveError."""
    target = unique_path(directory, source.name)
    try:
        mover(str(source), str(target))
    except OSError as exc:
        raise ArchiveError(f"Could not move {source} to {directory}: {exc}") from exc
    logger.info("Moved %s -> %s", source.name, target)
    return target


def archive_processed(source: Path, processed_dir: Path, **kwargs) -> Path:
    """Only called after commit AND verification succeeded."""
    return move_file(source, processed_dir, **kwargs)


def quarantine_failed(source: Path | None, failed_dir: Path, reason: str) -> Path | None:
    """Move a file to ``failed_dir`` with a ``.error.txt`` sidecar.  Never raises."""
    failed_dir.mkdir(parents=True, exist_ok=True)
    target: Path | None = None
    if source is not None and source.exists():
        try:
            target = move_file(source, failed_dir)
        except ArchiveError as exc:
            logger.error("Could not quarantine %s (%s); it stays where it is", source, exc)
            target = source
    note_base = target or failed_dir / f"{utc_stamp()}_no-file"
    try:
        note = unique_path(failed_dir, f"{note_base.name}.error.txt")
        note.write_text(f"{datetime.now(tz=UTC).isoformat()}\n{reason}\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        logger.error("Could not write failure note: %s", exc)
    return target


def write_state(path: Path, data: dict[str, object]) -> None:
    """Atomic non-secret JSON state (e.g. last run metadata)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
