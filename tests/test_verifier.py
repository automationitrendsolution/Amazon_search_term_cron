"""Post-import verification."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import inspect_table, replace_report_date
from app.exceptions import VerificationError
from app.processor import process_csv
from app.verifier import ExpectedImport, verify_import

from .conftest import REPORT_DATE, TABLE, make_row, write_csv

pytestmark = pytest.mark.filterwarnings("ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal")


@pytest.fixture
def imported(engine, tmp_path: Path):
    path = write_csv(tmp_path / "in.csv", [make_row(term="a", cost="1.10"), make_row(term="b", clicks="7", cost="2.25")])
    report = process_csv(path, REPORT_DATE)
    result = replace_report_date(engine, inspect_table(engine, TABLE), report)
    return ExpectedImport.from_report(report, result)


def test_successful_import_verifies(engine, imported) -> None:
    report = verify_import(engine, TABLE, imported)
    assert report.ok and report.stored_rows == 2
    assert any("other dates unchanged" in c for c in report.checks)
    assert any("Clicks total 12" in c for c in report.checks)


def test_row_count_mismatch(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'DELETE FROM "{TABLE}" WHERE "Customer Search Term" = \'a\''))
    with pytest.raises(VerificationError, match="ALREADY COMMITTED"):
        verify_import(engine, TABLE, imported)


def test_value_tampering_detected(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'UPDATE "{TABLE}" SET "Clicks" = 999 WHERE "Customer Search Term" = \'a\''))
    with pytest.raises(VerificationError, match="Clicks"):
        verify_import(engine, TABLE, imported)


def test_invalid_numeric_format_detected(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'UPDATE "{TABLE}" SET "Impressions" = \'n/a\' WHERE "Customer Search Term" = \'a\''))
    with pytest.raises(VerificationError, match="not valid numbers"):
        verify_import(engine, TABLE, imported)


def test_other_dates_change_detected(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'DELETE FROM "{TABLE}" WHERE "Customer Search Term" = \'old a\''))
    with pytest.raises(VerificationError, match="outside 2026-10-21 changed"):
        verify_import(engine, TABLE, imported)


def test_no_rows_stored(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'DELETE FROM "{TABLE}" WHERE "Date" = \'2026-10-21\''))
    with pytest.raises(VerificationError, match="no rows stored"):
        verify_import(engine, TABLE, imported)


def test_report_without_raising(engine, imported) -> None:
    with engine.begin() as c:
        c.execute(text(f'DELETE FROM "{TABLE}" WHERE "Customer Search Term" = \'a\''))
    report = verify_import(engine, TABLE, imported, raise_on_failure=False)
    assert not report.ok


def test_expected_round_trips_through_json(imported) -> None:
    again = ExpectedImport.from_json(imported.to_json())
    assert again == imported
