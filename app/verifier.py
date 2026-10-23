"""Post-commit verification.

A successful COMMIT only proves the statements ran.  This module re-reads the
table on a fresh connection and checks that what is stored is what the CSV
said, and that nothing outside the selected date moved.

Every failure here means **the data is already committed**: the caller keeps
the CSV in ``data/failed/`` and says so in the error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .database import (
    REQUIRED_DEST_COLUMNS,
    ImportResult,
    TableSchema,
    _safe_error,
    column_values_for_date,
    inspect_table,
    other_dates_fingerprint,
)
from .exceptions import DatabaseError, SchemaError, VerificationError
from .logging_setup import get_logger
from .processor import DEST_INTEGER_COLUMNS, ProcessedReport

logger = get_logger("verifier")

NUMERIC_COLUMNS = (*DEST_INTEGER_COLUMNS, "Spend", "Sales")


@dataclass
class ExpectedImport:
    """What the table must contain for the date (also kept in the state journal)."""

    report_date: date
    row_count: int
    totals: dict[str, Decimal]
    null_counts: dict[str, int]
    other_dates_before: dict[str, str] | None = None

    @classmethod
    def from_report(cls, report: ProcessedReport, result: ImportResult | None = None) -> ExpectedImport:
        return cls(
            report_date=report.report_date,
            row_count=report.row_count,
            totals={k: v for k, v in report.totals.items() if k in NUMERIC_COLUMNS},
            null_counts={k: v for k, v in report.null_counts.items() if k in NUMERIC_COLUMNS},
            other_dates_before=result.other_dates_before if result else None,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "report_date": self.report_date.isoformat(),
            "row_count": self.row_count,
            "totals": {k: str(v) for k, v in self.totals.items()},
            "null_counts": self.null_counts,
            "other_dates_before": self.other_dates_before,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> ExpectedImport:
        return cls(
            report_date=date.fromisoformat(str(data["report_date"])),
            row_count=int(data["row_count"]),  # type: ignore[arg-type]
            totals={k: Decimal(v) for k, v in dict(data.get("totals") or {}).items()},
            null_counts={k: int(v) for k, v in dict(data.get("null_counts") or {}).items()},
            other_dates_before=data.get("other_dates_before"),  # type: ignore[arg-type]
        )


@dataclass
class VerificationReport:
    report_date: date
    stored_rows: int
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def verify_import(
    engine: Engine,
    table_name: str,
    expected: ExpectedImport,
    *,
    text_date_format: str = "%Y-%m-%d",
    check_other_dates: bool = True,
    raise_on_failure: bool = True,
) -> VerificationReport:
    """Re-read the table and compare with ``expected``.

    Raises:
        VerificationError: any check failed (data is committed).
        DatabaseError: the verification queries themselves failed.
    """
    try:
        schema: TableSchema = inspect_table(engine, table_name)
    except SchemaError as exc:
        return _finish(
            VerificationReport(expected.report_date, -1, failures=[f"schema check failed: {exc}"]),
            raise_on_failure,
        )
    missing = [c for c in REQUIRED_DEST_COLUMNS if c not in schema.columns]
    report = VerificationReport(expected.report_date, stored_rows=-1)
    if missing:
        report.failures.append(f"required column(s) missing after import: {missing}")
        return _finish(report, raise_on_failure)
    report.checks.append("required columns present")

    try:
        with engine.connect() as connection:
            rows = column_values_for_date(
                connection, schema, list(NUMERIC_COLUMNS), expected.report_date, text_date_format
            )
            after = (
                other_dates_fingerprint(connection, schema, expected.report_date, text_date_format)
                if check_other_dates and expected.other_dates_before is not None
                else None
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"Verification query failed (the import IS committed): {_safe_error(exc)}", committed=True
        ) from exc

    report.stored_rows = len(rows)

    # -- row count -----------------------------------------------------------
    if expected.row_count > 0 and not rows:
        report.failures.append(f"no rows stored for {expected.report_date} but the CSV had {expected.row_count}")
    elif len(rows) != expected.row_count:
        report.failures.append(
            f"{len(rows)} row(s) stored for {expected.report_date}, expected {expected.row_count} "
            "(no deduplication is applied, so these must match exactly)"
        )
    else:
        report.checks.append(f"row count {len(rows)} matches the CSV")

    # -- numeric values: valid format, null counts, totals ----------------------
    for index, name in enumerate(NUMERIC_COLUMNS):
        total = Decimal(0)
        nulls = 0
        invalid = 0
        for row in rows:
            value = row[index]
            if value is None:
                nulls += 1
                continue
            try:
                number = Decimal(str(value).strip())
            except InvalidOperation:
                invalid += 1
                continue
            if name in DEST_INTEGER_COLUMNS and number != number.to_integral_value():
                invalid += 1
                continue
            total += number
        if invalid:
            report.failures.append(f"{invalid} stored {name!r} value(s) are not valid numbers")
        expected_nulls = expected.null_counts.get(name)
        if expected_nulls is not None and nulls != expected_nulls:
            report.failures.append(f"{name!r}: {nulls} NULL(s) stored, CSV had {expected_nulls}")
        expected_total = expected.totals.get(name)
        if expected_total is not None:
            tolerance = Decimal("0") if schema.columns[name].kind != "float" else Decimal("1e-6") * max(len(rows), 1)
            if abs(total - expected_total) > tolerance:
                report.failures.append(f"{name!r}: stored total {total} != CSV total {expected_total}")
            else:
                report.checks.append(f"{name} total {total} matches")

    # -- other dates untouched -------------------------------------------------
    if after is not None:
        if after != expected.other_dates_before:
            changed = {
                k: (expected.other_dates_before.get(k), v)  # type: ignore[union-attr]
                for k, v in after.items()
                if expected.other_dates_before.get(k) != v  # type: ignore[union-attr]
            }
            report.failures.append(
                f"rows outside {expected.report_date} changed during the import: {changed} "
                "(another writer may be active on this table)"
            )
        else:
            report.checks.append(f"other dates unchanged ({after.get('rows')} row(s))")

    return _finish(report, raise_on_failure)


def _finish(report: VerificationReport, raise_on_failure: bool) -> VerificationReport:
    for check in report.checks:
        logger.info("Verified: %s", check)
    if report.failures:
        for failure in report.failures:
            logger.error("Verification FAILED: %s", failure)
        if raise_on_failure:
            raise VerificationError(
                f"Post-import verification failed for {report.report_date}. THE TRANSACTION IS "
                "ALREADY COMMITTED; the CSV is kept in data/failed for investigation:\n  - "
                + "\n  - ".join(report.failures)
            )
    return report
