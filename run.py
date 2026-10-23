"""Amazon Ads Search Term report -> MySQL.  Command-line entry point.

    uv run python run.py                      full run (download, validate, import, verify, archive)
    uv run python run.py --dry-run            download + validate; MySQL untouched
    uv run python run.py --login              interactive Amazon Ads sign-in (visible browser)
    uv run python run.py --validate-csv FILE  validate a local CSV; MySQL untouched
    uv run python run.py --check-db           read-only database/schema/engine check
    uv run python run.py --inspect-report     read-only selector check on the live page

Exit codes:
    0 success (or an expected skip)    5 CSV validation failed (no DB change)
    1 unexpected error                 6 database/schema error (rolled back / nothing written,
    2 configuration error                unless the log says COMMIT OUTCOME UNKNOWN)
    3 Amazon sign-in required          7 post-commit verification failed (DATA IS COMMITTED)
    4 browser/report retrieval failed  8 archive failed after a verified import
                                       9 another run is in progress
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from app.config import (
    env_file_is_private,
    load_database_settings,
    load_env,
    load_settings,
    secret_values,
)
from app.exceptions import AutomationError, ConfigurationError, ReportNotAvailableError
from app.logging_setup import configure_logging, get_logger, stage
from app.storage import RunLock

logger = get_logger("run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the Amazon Ads Search Term report and load it into MySQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes:" + __doc__.split("Exit codes:", 1)[1] if __doc__ else None,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="open a visible browser to sign in to Amazon Ads")
    mode.add_argument("--dry-run", action="store_true", help="download and validate only; do not change MySQL")
    mode.add_argument("--validate-csv", metavar="PATH", type=Path, help="validate an existing CSV; do not change MySQL")
    mode.add_argument("--check-db", action="store_true", help="read-only check of the connection, schema and engine")
    mode.add_argument("--inspect-report", action="store_true", help="read-only check of selectors on the live report page")
    parser.add_argument(
        "--expected-date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="with --validate-csv: the date every row must have (default: the file must hold one date)",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser (debugging)")
    args = parser.parse_args(argv)
    if args.expected_date and not args.validate_csv:
        parser.error("--expected-date is only valid with --validate-csv")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env()
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    if args.headed:
        from dataclasses import replace

        settings = replace(settings, headless=False)

    settings.ensure_dirs()
    configure_logging(
        settings.log_dir,
        level=settings.log_level,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        secrets=secret_values(),
    )
    if env_file_is_private() is False:
        logger.warning(".env is readable by other users; run: chmod 600 .env")

    started = time.monotonic()
    try:
        if args.validate_csv:
            return cmd_validate_csv(settings, args.validate_csv, args.expected_date)
        if args.check_db:
            return cmd_check_db()
        with RunLock(settings.lock_file):
            if args.login:
                return cmd_login(settings)
            if args.inspect_report:
                return cmd_inspect(settings)
            return cmd_run(settings, dry_run=args.dry_run)
    except ReportNotAvailableError as exc:
        logger.warning("Skipped: %s", exc)
        return exc.exit_code
    except AutomationError as exc:
        logger.error("FAILED at stage '%s' (%s, exit %d) after %.1fs: %s", getattr(exc, "stage", "-"),
                     type(exc).__name__, exc.exit_code, time.monotonic() - started, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except Exception as exc:  # unexpected: full traceback in the log
        logger.exception("Unexpected error after %.1fs: %s", time.monotonic() - started, exc)
        return 1


def cmd_login(settings) -> int:
    from app.auth import interactive_login

    with stage("login"):
        interactive_login(settings)
    print("Sign-in complete. Scheduled runs will reuse data/session.", file=sys.stderr)
    return 0


def cmd_inspect(settings) -> int:
    from app.browser import inspect_report_page
    from app.pipeline import choose_for_now

    with stage("inspect"):
        choice = choose_for_now(settings)
        result = inspect_report_page(settings, choice)
    print(json.dumps(result, indent=2))
    return 0


def cmd_run(settings, *, dry_run: bool) -> int:
    from app.exceptions import AuthenticationError
    from app.pipeline import choices_for_now, run

    db = None if dry_run else load_database_settings()
    with stage("select"):
        choices = choices_for_now(settings)

    # Each report is imported on its own: a failure of one must not stop the other.
    exit_code = 0
    results: list[tuple[str, str, str, str]] = []  # period, date, status, detail
    for choice in choices:
        title = f"{choice.period.capitalize()} report | {choice.report_date}" + (" | dry run" if dry_run else "")
        _console_rule(title)
        started = time.monotonic()
        try:
            outcome = run(settings, db, dry_run=dry_run, choice=choice)
        except ReportNotAvailableError as exc:
            logger.warning("Skipped: %s", exc)
            results.append((choice.period, str(choice.report_date), "SKIPPED", "date has not started for the account"))
            continue
        except AutomationError as exc:
            logger.error("FAILED at stage '%s' (%s, exit %d): %s", getattr(exc, "stage", "-"),
                         type(exc).__name__, exc.exit_code, exc)
            first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            results.append((choice.period, str(choice.report_date), "FAILED", f"exit {exc.exit_code}: {first_line}"))
            exit_code = exit_code or exc.exit_code
            if isinstance(exc, AuthenticationError):
                break  # the other report needs the same sign-in
            continue
        took = time.monotonic() - started
        if outcome.status == "no-data":
            detail = f"Amazon has no rows yet for this day; nothing changed  ({took:.0f}s)"
            results.append((choice.period, str(choice.report_date), "NO DATA", detail))
        elif dry_run:
            detail = f"{outcome.rows:,} valid rows, MySQL not touched  ({took:.0f}s)"
            results.append((choice.period, str(choice.report_date), "DRY RUN", detail))
        else:
            detail = f"{outcome.rows:,} rows imported, {outcome.deleted or 0:,} replaced  ({took:.0f}s)"
            results.append((choice.period, str(choice.report_date), "OK", detail))

    _console_rule("Summary")
    for period, day, status, detail in results:
        print(f"  {period:<10} {day}   {status:<8} {detail}", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)
    return exit_code


def _console_rule(title: str, width: int = 72) -> None:
    line = f"== {title} " + "=" * max(3, width - len(title) - 4)
    print(f"\n{line}", file=sys.stderr, flush=True)


def cmd_validate_csv(settings, path: Path, expected: date | None) -> int:
    from app.pipeline import validate_only

    report = validate_only(settings, path, expected)
    print(json.dumps(report.summary(), indent=2))
    return 0


def cmd_check_db() -> int:
    from sqlalchemy import func, select

    from app.database import check_text_date_format, create_db_engine, describe_schema, inspect_table

    db = load_database_settings()
    engine = create_db_engine(db)
    try:
        with stage("check-db"):
            schema = inspect_table(engine, db.table)
            check_text_date_format(engine, schema, db.text_date_format)
            with engine.connect() as connection:
                total = connection.execute(select(func.count()).select_from(schema.table)).scalar_one()
                date_col = schema.table.c["Date"]
                low, high = connection.execute(select(func.min(date_col), func.max(date_col))).one()
        print(f"Connected: {db.safe_description()}")
        for line in describe_schema(schema):
            print(line)
        for warning in schema.warnings:
            print(f"note: {warning}")
        print(f"rows: {total}; Date range: {low} .. {high}")
        print("Schema is compatible and the engine is transactional. Nothing was changed.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
