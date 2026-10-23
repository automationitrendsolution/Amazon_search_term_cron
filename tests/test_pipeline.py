"""End-to-end workflow with a fake browser and a SQLite stand-in database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import pipeline
from app.exceptions import ArchiveError, DatabaseError, ReportNotAvailableError, ValidationError, VerificationError
from app.storage import ImportJournal

from .conftest import FakeDownload, make_row, rows_by_date, write_csv

pytestmark = pytest.mark.filterwarnings("ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal")

#: 09:00 IST Oct 22 -> report date Oct 21 (yesterday).
NOW = datetime(2026, 10, 22, 3, 30, tzinfo=UTC)


def fake_retriever(rows):
    calls = []

    def retrieve(settings, choice):
        calls.append(choice)
        path = write_csv(settings.download_dir / f"dl_{len(calls)}.csv", rows)
        return FakeDownload(path)

    retrieve.calls = calls
    return retrieve


def journal(settings) -> ImportJournal:
    return ImportJournal(settings.state_dir / "import_journal.json")


def test_full_run_imports_verifies_and_archives(settings, db_settings, engine) -> None:
    retriever = fake_retriever([make_row(term="n1"), make_row(term="n2")])
    outcome = pipeline.run(settings, db_settings, now=NOW, retriever=retriever, engine=engine)
    assert outcome.status == "success" and outcome.rows == 2 and outcome.deleted == 1
    assert retriever.calls[0].period == "yesterday"
    assert rows_by_date(engine) == {"2026-10-20": 2, "2026-10-21": 2, "2026-10-22": 1}
    assert outcome.file.parent == settings.processed_dir and outcome.file.exists()
    assert list(settings.download_dir.glob("*.csv")) == []
    assert journal(settings).current["status"] == "archived"


def test_after_cutoff_uses_today_report(settings, db_settings, engine) -> None:
    retriever = fake_retriever([make_row(day="Oct 22, 2026")])
    after_cutoff = datetime(2026, 10, 22, 7, 0, tzinfo=UTC)  # 12:30 IST
    pipeline.run(settings, db_settings, now=after_cutoff, retriever=retriever, engine=engine)
    assert retriever.calls[0].period == "today"
    assert rows_by_date(engine)["2026-10-22"] == 1


def test_both_mode_imports_yesterday_then_today(settings, db_settings, engine) -> None:
    from dataclasses import replace

    both = replace(settings, report_mode="both")
    choices = pipeline.choices_for_now(both, NOW)  # account date Oct 22 (IST)
    assert [(c.period, c.report_date.isoformat()) for c in choices] == [
        ("yesterday", "2026-10-21"),
        ("today", "2026-10-22"),
    ]
    for choice in choices:
        day = choice.report_date.strftime("%b %d, %Y")
        retriever = fake_retriever([make_row(day=day, term="x"), make_row(day=day, term="y")])
        pipeline.run(both, db_settings, now=NOW, retriever=retriever, engine=engine, choice=choice)
        assert retriever.calls == [choice]
    assert rows_by_date(engine) == {"2026-10-20": 2, "2026-10-21": 2, "2026-10-22": 2}


def test_cutoff_mode_is_a_single_choice(settings) -> None:
    assert [c.period for c in pipeline.choices_for_now(settings, NOW)] == ["yesterday"]


def test_wrong_date_csv_is_not_imported(settings, db_settings, engine) -> None:
    before = rows_by_date(engine)
    with pytest.raises(ValidationError):
        pipeline.run(settings, db_settings, now=NOW, retriever=fake_retriever([make_row(day="Oct 20, 2026")]),
                     engine=engine)
    assert rows_by_date(engine) == before
    assert list(settings.processed_dir.iterdir()) == []
    assert len(list(settings.failed_dir.glob("*.csv"))) == 1
    assert len(list(settings.failed_dir.glob("*.error.txt"))) == 1


def test_database_failure_keeps_data_and_file(settings, db_settings, engine, monkeypatch) -> None:
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text('CREATE TRIGGER boom BEFORE INSERT ON "search term report" '
                       "BEGIN SELECT RAISE(ABORT, 'disk full'); END"))
    before = rows_by_date(engine)
    with pytest.raises(DatabaseError):
        pipeline.run(settings, db_settings, now=NOW, retriever=fake_retriever([make_row()]), engine=engine)
    assert rows_by_date(engine) == before
    assert list(settings.processed_dir.iterdir()) == []
    note = next(settings.failed_dir.glob("*.error.txt")).read_text(encoding="utf-8")
    assert "rolled back" in note


def test_verification_failure_is_not_archived(settings, db_settings, engine, monkeypatch) -> None:
    def failing_verify(*args, **kwargs):
        raise VerificationError("simulated mismatch")

    monkeypatch.setattr(pipeline, "verify_import", failing_verify)
    with pytest.raises(VerificationError):
        pipeline.run(settings, db_settings, now=NOW, retriever=fake_retriever([make_row()]), engine=engine)
    assert list(settings.processed_dir.iterdir()) == []
    assert len(list(settings.failed_dir.glob("*.csv"))) == 1
    assert journal(settings).current["status"] == "verify-failed"


def test_archive_failure_is_reported_separately(settings, db_settings, engine, tmp_path: Path) -> None:
    from app.processor import process_csv

    path = write_csv(settings.download_dir / "x.csv", [make_row()])
    report = process_csv(path, datetime(2026, 10, 21).date())

    def broken_move(src, dst):
        raise PermissionError("read-only filesystem")

    with pytest.raises(ArchiveError, match="committed and verified"):
        pipeline.import_report(settings, db_settings, engine, report, move=broken_move)
    assert path.exists()  # left in place
    assert rows_by_date(engine)["2026-10-21"] == 1  # import itself stands
    assert journal(settings).current["status"] == "archive-failed"


def test_dry_run_never_touches_the_database(settings, engine) -> None:
    before = rows_by_date(engine)
    outcome = pipeline.run(settings, None, dry_run=True, now=NOW, retriever=fake_retriever([make_row()]))
    assert outcome.status == "dry-run" and outcome.rows == 1
    assert rows_by_date(engine) == before
    assert outcome.file.parent == settings.download_dir


def test_date_not_started_for_account_skips(settings, db_settings, engine, monkeypatch) -> None:
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    us = replace(settings, amazon_account_timezone=ZoneInfo("America/Los_Angeles"))
    winter_after_cutoff = datetime(2026, 12, 22, 7, 15, tzinfo=UTC)  # 12:45 IST, 23:15 PST Dec 21
    retriever = fake_retriever([make_row()])
    with pytest.raises(ReportNotAvailableError):
        pipeline.run(us, db_settings, now=winter_after_cutoff, retriever=retriever, engine=engine)
    assert retriever.calls == []


def test_recovery_of_unknown_commit_that_did_commit(settings, db_settings, engine) -> None:
    # A first run commits but "loses" the COMMIT acknowledgement.
    from app.processor import process_csv
    from app.database import inspect_table, replace_report_date
    from app.verifier import ExpectedImport

    path = write_csv(settings.download_dir / "prev.csv", [make_row(term="p")])
    report = process_csv(path, datetime(2026, 10, 21).date())
    replace_report_date(engine, inspect_table(engine, "search term report"), report)
    j = journal(settings)
    j.begin({"report_date": "2026-10-21", "file": str(path), "rows": 1,
             "expected": ExpectedImport.from_report(report).to_json()})
    j.update("commit-unknown")

    assert pipeline.recover_pending(settings, db_settings, engine).startswith("previous import is committed")
    assert j.pending() is None
    assert (settings.processed_dir / "prev.csv").exists()


def test_recovery_of_unknown_commit_that_rolled_back(settings, db_settings, engine) -> None:
    from app.processor import process_csv
    from app.verifier import ExpectedImport

    path = write_csv(settings.download_dir / "prev.csv", [make_row(term="p"), make_row(term="q")])
    report = process_csv(path, datetime(2026, 10, 21).date())
    j = journal(settings)
    j.begin({"report_date": "2026-10-21", "file": str(path), "rows": 2,
             "expected": ExpectedImport.from_report(report).to_json()})
    resolution = pipeline.recover_pending(settings, db_settings, engine)
    assert "NOT reflected" in resolution
    assert j.pending() is None
    assert (settings.failed_dir / "prev.csv").exists()


def test_recovery_runs_before_new_import(settings, db_settings, engine, monkeypatch) -> None:
    order = []
    monkeypatch.setattr(pipeline, "recover_pending", lambda *a: order.append("recover"))
    real = pipeline.replace_report_date
    monkeypatch.setattr(pipeline, "replace_report_date", lambda *a, **k: (order.append("import"), real(*a, **k))[1])
    pipeline.run(settings, db_settings, now=NOW, retriever=fake_retriever([make_row()]), engine=engine)
    assert order == ["recover", "import"]


def _db_error(committed, cause):
    err = DatabaseError("x", committed=committed)
    err.__cause__ = cause
    return err


@pytest.mark.parametrize(
    ("committed", "transient", "expected_calls"),
    [
        (False, True, 2),   # rolled back on a dropped connection -> safe to retry
        (False, False, 1),  # rolled back on a data error -> retrying would not help
        (None, True, 1),    # COMMIT outcome unknown -> never retried blindly
    ],
)
def test_import_retry_policy(settings, monkeypatch, committed, transient, expected_calls) -> None:
    from sqlalchemy.exc import OperationalError

    cause = OperationalError("SELECT 1", {}, Exception("gone away")) if transient else ValueError("bad")
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _db_error(committed, cause)
        return "ok"

    monkeypatch.setattr(pipeline, "replace_report_date", flaky)
    if expected_calls == 2:
        assert pipeline._replace_with_retry(settings, None, None, None, "%Y-%m-%d", [], sleep=lambda s: None) == "ok"
    else:
        with pytest.raises(DatabaseError):
            pipeline._replace_with_retry(settings, None, None, None, "%Y-%m-%d", [], sleep=lambda s: None)
    assert len(calls) == expected_calls
