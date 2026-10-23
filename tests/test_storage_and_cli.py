"""Locking, file handling, logging redaction and the CLI."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import run
from app.exceptions import LockError
from app.logging_setup import RedactingFilter
from app.storage import ImportJournal, RunLock, move_file, quarantine_failed, unique_path

from .conftest import make_row, write_csv


def test_lock_prevents_overlap(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with RunLock(path):
        with pytest.raises(LockError):
            RunLock(path).acquire()
    with RunLock(path):  # released: can be taken again
        pass


def test_unique_path_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("1")
    (tmp_path / "a-1.csv").write_text("2")
    assert unique_path(tmp_path, "a.csv").name == "a-2.csv"


def test_move_does_not_overwrite(tmp_path: Path) -> None:
    dest = tmp_path / "processed"
    dest.mkdir()
    (dest / "r.csv").write_text("existing")
    source = tmp_path / "r.csv"
    source.write_text("new")
    moved = move_file(source, dest)
    assert moved.name == "r-1.csv"
    assert (dest / "r.csv").read_text() == "existing"


def test_quarantine_writes_reason(tmp_path: Path) -> None:
    source = tmp_path / "r.csv"
    source.write_text("x")
    target = quarantine_failed(source, tmp_path / "failed", "bad date")
    assert target.exists() and not source.exists()
    assert "bad date" in (tmp_path / "failed" / "r.csv.error.txt").read_text()


def test_journal_states(tmp_path: Path) -> None:
    journal = ImportJournal(tmp_path / "j.json")
    assert journal.pending() is None
    journal.begin({"report_date": "2026-10-21"})
    assert journal.pending()["status"] == "started"
    journal.update("committed")
    assert journal.pending()["status"] == "committed"
    journal.update("archived")
    assert journal.pending() is None
    assert journal.read()["history"][-1]["status"] == "archived"


def test_corrupt_journal_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "j.json"
    path.write_text("{not json")
    assert ImportJournal(path).pending() is None
    assert list(tmp_path.glob("j.corrupt-*.json"))


def test_redaction() -> None:
    flt = RedactingFilter(["s3cr3t-pw"])
    record = logging.LogRecord("stc.x", logging.INFO, "f", 1,
                               "pw=%s url=%s", ("s3cr3t-pw", "https://a.com/x?token=abc"), None)
    flt.filter(record)
    message = record.getMessage()
    assert "s3cr3t-pw" not in message and "token=abc" not in message
    assert "mysql+mysqlconnector://***:***@" in flt.redact("mysql+mysqlconnector://u:p@host/db")


# -- CLI -------------------------------------------------------------------------


def test_cli_validate_csv_ok(settings, tmp_path: Path, capsys) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row()])
    assert run.main(["--validate-csv", str(path), "--expected-date", "2026-10-21"]) == 0
    assert '"rows": 1' in capsys.readouterr().out
    assert path.exists()  # validation never moves the file


def test_cli_validate_csv_failure_exit_code(settings, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(clicks="many")])
    assert run.main(["--validate-csv", str(path)]) == 5


def test_cli_validate_csv_wrong_expected_date(settings, tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row()])
    assert run.main(["--validate-csv", str(path), "--expected-date", "2026-10-22"]) == 5


def test_cli_refuses_to_overlap(settings) -> None:
    with RunLock(settings.lock_file):
        assert run.main(["--dry-run"]) == 9


def test_cli_dry_run_with_fake_browser(settings, monkeypatch) -> None:
    from dataclasses import dataclass

    @dataclass
    class D:
        path: Path

    def fake(s, choice):
        rows = [make_row(day=choice.report_date.strftime("%b %d, %Y"))]
        return D(write_csv(s.download_dir / "dl.csv", rows))

    monkeypatch.setattr("app.browser.retrieve_with_retries", fake)
    assert run.main(["--dry-run"]) == 0


def test_cli_configuration_error(settings, monkeypatch) -> None:
    monkeypatch.setenv("TODAY_REPORT_CUTOFF", "25:99")
    assert run.main(["--dry-run"]) == 2


def test_cli_check_db_requires_settings(settings, monkeypatch) -> None:
    for name in ("DB_HOST", "DB_NAME", "DB_USER", "REPORT_TABLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("app.config.load_env", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_env", lambda *a, **k: None)
    assert run.main(["--check-db"]) == 2


def test_cli_expected_date_requires_validate() -> None:
    with pytest.raises(SystemExit):
        run.parse_args(["--expected-date", "2026-10-21"])
