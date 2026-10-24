"""End-to-end orchestration: select date -> download -> validate -> import -> verify -> archive.

Dependencies (clock, browser retrieval, DB engine) are injectable so the whole
workflow is testable without Amazon or a production database.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError

from .config import DatabaseSettings, Settings
from .database import (
    build_rows,
    check_text_date_format,
    count_for_date,
    create_db_engine,
    inspect_table,
    replace_report_date,
)
from .dates import ReportChoice, choose_report, select_report_date, utc_now
from .exceptions import (
    ArchiveError,
    DatabaseError,
    EmptyReportError,
    ReportNotAvailableError,
    ValidationError,
    VerificationError,
)
from .logging_setup import get_logger, set_context, stage
from .processor import ProcessedReport, process_csv
from .storage import ImportJournal, archive_processed, quarantine_failed, sha256_of
from .verifier import ExpectedImport, verify_import

logger = get_logger("pipeline")


@dataclass
class RunOutcome:
    status: str  # success | dry-run | skipped | recovered
    report_date: date | None = None
    rows: int = 0
    deleted: int | None = None
    file: Path | None = None
    details: dict[str, object] = field(default_factory=dict)


def journal_for(settings: Settings) -> ImportJournal:
    return ImportJournal(settings.state_dir / "import_journal.json")


def choose_for_now(settings: Settings, now: datetime | None = None) -> ReportChoice:
    moment = now or utc_now()
    report_date = select_report_date(moment, tz=settings.report_timezone, cutoff=settings.cutoff)
    set_context(report_date=report_date.isoformat())
    choice = choose_report(
        report_date,
        account_tz=settings.amazon_account_timezone,
        period_tz=settings.period_tz,
        report_id_today=settings.report_id_today,
        report_id_yesterday=settings.report_id_yesterday,
        name_today=settings.report_name_today,
        name_yesterday=settings.report_name_yesterday,
        now=moment,
    )
    logger.info(
        "Selected report date %s (%s %s); Amazon account date %s -> '%s' report %s",
        report_date,
        moment.astimezone(settings.report_timezone).strftime("%Y-%m-%d %H:%M:%S"),
        settings.report_timezone.key,
        choice.account_today,
        choice.period,
        choice.report_id,
    )
    return choice


def choices_for_now(settings: Settings, now: datetime | None = None) -> list[ReportChoice]:
    """The reports this run imports: one (REPORT_MODE=cutoff) or Yesterday then Today (both).

    In "both" mode the two dates are yesterday and today in the calendar Amazon
    uses for the saved reports' "Yesterday"/"Today" (``period_tz``, IST).  A date
    that has not started yet in the account's marketplace calendar (US Pacific,
    e.g. "today" before 12:30 IST) has no data, so that report is skipped.
    """
    if settings.report_mode != "both":
        return [choose_for_now(settings, now)]
    moment = now or utc_now()
    period_today = moment.astimezone(settings.period_tz).date()
    choices = []
    for report_date in (period_today - timedelta(days=1), period_today):
        try:
            choice = choose_report(
                report_date,
                account_tz=settings.amazon_account_timezone,
                period_tz=settings.period_tz,
                report_id_today=settings.report_id_today,
                report_id_yesterday=settings.report_id_yesterday,
                name_today=settings.report_name_today,
                name_yesterday=settings.report_name_yesterday,
                now=moment,
            )
        except ReportNotAvailableError as exc:
            logger.info("Not running the report for %s: %s", report_date, exc)
            continue
        logger.info("Selected report date %s -> '%s' report %s", report_date, choice.period, choice.report_id)
        choices.append(choice)
    return choices


# ---------------------------------------------------------------------------
# recovery of an interrupted earlier import
# ---------------------------------------------------------------------------


def recover_pending(settings: Settings, db: DatabaseSettings, engine: Engine) -> str | None:
    """Resolve an import the previous run did not finish, before any new write.

    The table -- not the journal -- is the source of truth: the stored rows
    for that date are compared with the expected counts and totals recorded
    before the transaction started.

    Returns the resolution, or None when nothing was pending.
    """
    journal = journal_for(settings)
    entry = journal.pending()
    if entry is None:
        return None
    with stage("recover", report_date=entry.get("report_date"), file=Path(str(entry.get("file", "-"))).name):
        logger.warning("Previous import ended in state %r; checking the table before continuing", entry.get("status"))
        expected = ExpectedImport.from_json(dict(entry["expected"]))  # type: ignore[arg-type]
        report = verify_import(
            engine,
            db.table,
            expected,
            text_date_format=db.text_date_format,
            check_other_dates=False,  # other writers may legitimately have run since
            raise_on_failure=False,
        )
        file = Path(str(entry.get("file"))) if entry.get("file") else None
        if report.ok:
            resolution = "previous import is committed and matches its CSV"
            if file is not None and file.exists() and file.parent != settings.processed_dir:
                try:
                    file = archive_processed(file, settings.processed_dir)
                except ArchiveError as exc:
                    logger.error("%s", exc)
            journal.update("resolved", resolution=resolution, file=str(file) if file else None)
        else:
            # Either rolled back (old data intact) or committed but wrong; the
            # date-scoped replace in this run (or the next one for that date)
            # overwrites it either way.  Keep the evidence.
            resolution = (
                "previous import is NOT reflected in the table (rolled back or since replaced): "
                + "; ".join(report.failures)
            )
            if file is not None and file.exists() and file.parent != settings.failed_dir:
                quarantine_failed(file, settings.failed_dir, f"recovery: {resolution}")
            journal.update("resolved", resolution=resolution)
        logger.warning("Recovery: %s", resolution)
        return resolution


# ---------------------------------------------------------------------------
# import of a validated report
# ---------------------------------------------------------------------------


def import_report(
    settings: Settings,
    db: DatabaseSettings,
    engine: Engine,
    report: ProcessedReport,
    *,
    move: Callable[[str, str], object] | None = None,
) -> RunOutcome:
    """Transactional replace + verification + archive for an already-validated CSV."""
    journal = journal_for(settings)
    path = report.source_path

    with stage("schema"):
        schema = inspect_table(engine, db.table)
        check_text_date_format(engine, schema, db.text_date_format)
        rows = build_rows(schema, report, db.text_date_format)  # value fit, before any write
        if not report.vcpm_in_source and schema.has_vcpm:
            logger.info("Source has no VCPM column; writing NULL to the existing VCPM column")

    with stage("import"):
        expected = ExpectedImport.from_report(report)
        journal.begin(
            {
                "report_date": report.report_date.isoformat(),
                "file": str(path),
                "sha256": sha256_of(path),
                "rows": report.row_count,
                "expected": expected.to_json(),
            }
        )
        try:
            result = _replace_with_retry(settings, engine, schema, report, db.text_date_format, rows)
        except DatabaseError as exc:
            if exc.committed is None:
                journal.update("commit-unknown", error=str(exc))
            else:
                journal.update("resolved", resolution=f"rolled back: {exc}")
            raise
        except ValidationError as exc:
            journal.update("resolved", resolution=f"rolled back: {exc}")
            raise
        expected.other_dates_before = result.other_dates_before
        journal.update("committed", deleted=result.deleted_rows, inserted=result.inserted_rows,
                       expected=expected.to_json())

    with stage("verify"):
        try:
            verification = verify_import(engine, db.table, expected, text_date_format=db.text_date_format)
        except (VerificationError, DatabaseError) as exc:
            journal.update("verify-failed", error=str(exc))
            raise VerificationError(str(exc)) from exc
        journal.update("verified", checks=verification.checks)

    with stage("archive"):
        try:
            archived = archive_processed(path, settings.processed_dir, **({"mover": move} if move else {}))
        except ArchiveError as exc:
            journal.update("archive-failed", error=str(exc))
            raise ArchiveError(
                f"{exc}. The import IS committed and verified; move the file manually."
            ) from exc
        journal.update("archived", file=str(archived))

    return RunOutcome(
        status="success",
        report_date=report.report_date,
        rows=report.row_count,
        deleted=result.deleted_rows,
        file=archived,
        details={"checks": verification.checks},
    )


def _replace_with_retry(settings, engine, schema, report, text_date_format, rows, *, sleep=time.sleep):
    """Retry the transaction only after a transient connection failure that was ROLLED BACK.

    Safe because the operation is a date-scoped replace: re-running it after a
    rollback yields the same end state.  A COMMIT with an unknown outcome
    (``committed is None``) is never retried -- the next run's recovery step
    decides what happened first.
    """
    for attempt in range(1, settings.db_max_attempts + 1):
        try:
            return replace_report_date(engine, schema, report, text_date_format=text_date_format, rows=rows)
        except DatabaseError as exc:
            transient = isinstance(exc.__cause__, (OperationalError, InterfaceError))
            if exc.committed is not False or not transient or attempt >= settings.db_max_attempts:
                raise
            logger.warning("Import attempt %d rolled back on a connection error; retrying in 10s", attempt)
            sleep(10)
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# full run
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    db: DatabaseSettings | None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    retriever: Callable[[Settings, ReportChoice], object] | None = None,
    engine: Engine | None = None,
    choice: ReportChoice | None = None,
) -> RunOutcome:
    """Default execution (or ``--dry-run`` when ``dry_run``) for one report."""
    started = time.monotonic()
    if choice is None:
        with stage("select"):
            choice = choose_for_now(settings, now)
    else:
        set_context(report_date=choice.report_date.isoformat(), file=None, rows=None)

    own_engine = False
    if not dry_run:
        assert db is not None
        if engine is None:
            engine = create_db_engine(db)
            own_engine = True
        try:
            with stage("preflight"):
                # Fail fast -- before spending up to 20 minutes on Amazon -- when the
                # database is unreachable or the schema is incompatible.
                recover_pending(settings, db, engine)
                schema = inspect_table(engine, db.table)
                check_text_date_format(engine, schema, db.text_date_format)
                with engine.connect() as connection:
                    current = count_for_date(connection, schema, choice.report_date, db.text_date_format)
                logger.info("Table currently holds %d row(s) for %s", current, choice.report_date)
        except Exception:
            if own_engine:
                engine.dispose()
            raise

    try:
        with stage("download"):
            if retriever is None:
                from .browser import retrieve_with_retries

                retriever = retrieve_with_retries
            downloaded = retriever(settings, choice)
            path: Path = downloaded.path  # type: ignore[attr-defined]
            set_context(file=path.name)

        with stage("validate"):
            try:
                report = process_csv(
                    path,
                    choice.report_date,
                    date_format=settings.csv_date_format,
                    allow_empty=settings.allow_empty_report,
                    duplicate_policy=settings.duplicate_policy,
                )
            except EmptyReportError as exc:
                if choice.period != "today":
                    quarantine_failed(path, settings.failed_dir, str(exc))
                    raise
                # The account's day has only just started: Amazon has no rows yet.
                # Nothing is written; the next hourly run picks the data up.
                path.unlink(missing_ok=True)
                logger.info("No data yet for %s (the day has just started for Amazon); nothing changed",
                            choice.report_date)
                return RunOutcome("no-data", choice.report_date, 0)
            except ValidationError as exc:
                quarantine_failed(path, settings.failed_dir, str(exc))
                raise
            set_context(rows=report.row_count)

        if dry_run:
            logger.info(
                "Dry run complete: %d valid row(s) for %s in %s; MySQL was not touched (%.1fs)",
                report.row_count, report.report_date, path, time.monotonic() - started,
            )
            return RunOutcome("dry-run", report.report_date, report.row_count, file=path,
                              details=report.summary())

        assert db is not None and engine is not None
        try:
            outcome = import_report(settings, db, engine, report)
        except ArchiveError:
            raise  # file intentionally left where it is
        except DatabaseError as exc:
            state = {False: "rolled back, no change", None: "COMMIT OUTCOME UNKNOWN", True: "committed"}
            quarantine_failed(path, settings.failed_dir, f"{exc} [{state[exc.committed]}]")
            raise
        except Exception as exc:  # validation-vs-schema, verification, unexpected
            quarantine_failed(path, settings.failed_dir, str(exc))
            raise
        logger.info(
            "Run complete: date=%s deleted=%s inserted=%d archived=%s duration=%.1fs",
            outcome.report_date, outcome.deleted, outcome.rows,
            outcome.file.name if outcome.file else "-", time.monotonic() - started,
        )
        return outcome
    finally:
        if own_engine and engine is not None:
            engine.dispose()


def validate_only(settings: Settings, path: Path, expected: date | None) -> ProcessedReport:
    """``--validate-csv``: validation only.  The file is not moved and MySQL is not touched."""
    with stage("validate", file=path.name):
        return process_csv(
            path,
            expected,
            date_format=settings.csv_date_format,
            allow_empty=settings.allow_empty_report,
            duplicate_policy=settings.duplicate_policy,
        )

