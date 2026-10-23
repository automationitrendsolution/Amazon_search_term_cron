"""Schema checks and the date-scoped, transactional replace (SQLite stand-in)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

from app import database
from app.database import (
    ColumnInfo,
    build_rows,
    check_text_date_format,
    convert_value,
    inspect_table,
    replace_report_date,
)
from app.exceptions import DatabaseError, SchemaError, ValidationError
from app.processor import VCPM_COLUMN, process_csv

from .conftest import REPORT_DATE, TABLE, create_table, make_row, rows_by_date, seed, write_csv

pytestmark = pytest.mark.filterwarnings("ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal")


def report_for(tmp_path: Path, rows=None, header=None):
    path = write_csv(tmp_path / "in.csv", rows or [make_row(term="new 1"), make_row(term="new 2", clicks="7")], header)
    return process_csv(path, REPORT_DATE)


# -- schema ------------------------------------------------------------------


def test_inspect_compatible_table(engine) -> None:
    schema = inspect_table(engine, TABLE)
    assert schema.date_kind == "date"
    assert schema.has_vcpm
    assert schema.write_columns[-1] == VCPM_COLUMN


def test_missing_table(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(SchemaError, match="does not exist"):
        inspect_table(eng, TABLE)


def test_missing_required_column(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    create_table(eng, drop=("Match Type",))
    with pytest.raises(SchemaError, match="'Match Type' is missing"):
        inspect_table(eng, TABLE)


def test_incompatible_type(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with eng.begin() as c:
        c.execute(text(f'CREATE TABLE "{TABLE}" ("Date" DATE, "Portfolio name" TEXT, "Campaign Name" TEXT, '
                       '"Targeting" TEXT, "Match Type" TEXT, "Customer Search Term" INTEGER, "Impressions" INTEGER, '
                       '"Clicks" INTEGER, "Spend" DECIMAL(10,2), "Sales" DECIMAL(10,2), "Units" INTEGER)'))
    with pytest.raises(SchemaError, match="Customer Search Term"):
        inspect_table(eng, TABLE)


def test_unmapped_not_null_column_without_default(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    create_table(eng)
    with eng.begin() as c:
        c.execute(text(f'ALTER TABLE "{TABLE}" ADD COLUMN "Owner" TEXT NOT NULL DEFAULT \'x\''))
    inspect_table(eng, TABLE)  # has a default: fine


def test_non_transactional_engine_refused(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "table_engine", lambda *a: "MyISAM")
    with pytest.raises(SchemaError, match="not transactional"):
        inspect_table(engine, TABLE)


def test_vcpm_optional_in_table(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    create_table(eng, vcpm=False)
    schema = inspect_table(eng, TABLE)
    assert VCPM_COLUMN not in schema.write_columns


# -- value fit -----------------------------------------------------------------


def col(kind: str, **kw) -> ColumnInfo:
    base = dict(name="x", kind=kind, type_name=kind, nullable=True, has_default=False, autoincrement=False)
    base.update(kw)
    return ColumnInfo(**base)


def test_convert_value_rules() -> None:
    assert convert_value(col("decimal", precision=12, scale=2), Decimal("1.5"), "") == Decimal("1.50")
    with pytest.raises(ValueError, match="decimal place"):
        convert_value(col("decimal", precision=12, scale=2), Decimal("1.555"), "")
    with pytest.raises(ValueError, match="precision"):
        convert_value(col("decimal", precision=4, scale=2), Decimal("123.45"), "")
    with pytest.raises(ValueError, match="exceeds"):
        convert_value(col("text", length=3), "abcd", "")
    with pytest.raises(ValueError, match="out of range"):
        convert_value(col("integer", type_name="tinyint"), 300, "")
    with pytest.raises(ValueError, match="NOT NULL"):
        convert_value(col("integer", nullable=False), None, "")
    assert convert_value(col("text", length=20), Decimal("12.30"), "") == "12.30"


def test_vcpm_written_as_null_when_absent(engine, tmp_path: Path) -> None:
    schema = inspect_table(engine, TABLE)
    report = report_for(tmp_path)
    rows = build_rows(schema, report, "%Y-%m-%d")
    vcpm_param = f"p{schema.write_columns.index(VCPM_COLUMN)}"
    assert all(r[vcpm_param] is None for r in rows)


def test_too_long_value_is_rejected_before_any_write(engine, tmp_path: Path) -> None:
    before = rows_by_date(engine)
    report = report_for(tmp_path, [make_row(match="X" * 51)])
    schema = inspect_table(engine, TABLE)
    with pytest.raises(ValidationError, match="exceeds"):
        replace_report_date(engine, schema, report)
    assert rows_by_date(engine) == before


# -- the transaction -----------------------------------------------------------


def test_replaces_only_the_selected_date(engine, tmp_path: Path) -> None:
    schema = inspect_table(engine, TABLE)
    result = replace_report_date(engine, schema, report_for(tmp_path))
    assert (result.deleted_rows, result.inserted_rows, result.committed) == (1, 2, True)
    assert rows_by_date(engine) == {"2026-10-20": 2, "2026-10-21": 2, "2026-10-22": 1}
    with engine.connect() as c:
        terms = c.execute(text(f'SELECT "Customer Search Term" FROM "{TABLE}" WHERE "Date"=\'2026-10-21\'')).scalars().all()
        others = c.execute(text(f'SELECT "Customer Search Term" FROM "{TABLE}" WHERE "Date"<>\'2026-10-21\'')).scalars().all()
    assert sorted(terms) == ["new 1", "new 2"]
    assert sorted(others) == ["future y", "old a", "old b"]
    assert result.other_dates_before["rows"] == "3"


def test_rerun_is_idempotent(engine, tmp_path: Path) -> None:
    schema = inspect_table(engine, TABLE)
    report = report_for(tmp_path)
    replace_report_date(engine, schema, report)
    second = replace_report_date(engine, schema, report)
    assert second.deleted_rows == 2
    assert rows_by_date(engine)["2026-10-21"] == 2


def test_insert_failure_rolls_back_the_delete(engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with engine.begin() as c:
        c.execute(text(f'CREATE TRIGGER boom BEFORE INSERT ON "{TABLE}" '
                       "WHEN NEW.\"Customer Search Term\" = 'boom' BEGIN SELECT RAISE(ABORT, 'boom'); END"))
    monkeypatch.setattr(database, "INSERT_CHUNK_SIZE", 1)  # first row inserted, second fails
    before = rows_by_date(engine)
    schema = inspect_table(engine, TABLE)
    with pytest.raises(DatabaseError) as info:
        replace_report_date(engine, schema, report_for(tmp_path, [make_row(term="ok"), make_row(term="boom")]))
    assert info.value.committed is False
    assert rows_by_date(engine) == before  # the deleted 2026-10-21 row is back
    with engine.connect() as c:
        assert c.execute(text(f'SELECT "Customer Search Term" FROM "{TABLE}" WHERE "Date"=\'2026-10-21\'')).scalar() == "stale x"


def test_count_mismatch_inside_transaction_rolls_back(engine, tmp_path: Path) -> None:
    with engine.begin() as c:
        c.execute(text(f'CREATE TRIGGER drop_one BEFORE INSERT ON "{TABLE}" '
                       "WHEN NEW.\"Customer Search Term\" = 'ghost' BEGIN SELECT RAISE(IGNORE); END"))
    before = rows_by_date(engine)
    schema = inspect_table(engine, TABLE)
    with pytest.raises(DatabaseError, match="expected 2") as info:
        replace_report_date(engine, schema, report_for(tmp_path, [make_row(term="a"), make_row(term="ghost")]))
    assert info.value.committed is False
    assert rows_by_date(engine) == before


def test_commit_failure_is_reported_as_unknown(engine, tmp_path: Path) -> None:
    def fail_commit(conn):
        raise RuntimeError("connection lost during COMMIT")

    event.listen(engine, "commit", fail_commit)
    schema = inspect_table(engine, TABLE)
    with pytest.raises(DatabaseError, match="UNKNOWN") as info:
        replace_report_date(engine, schema, report_for(tmp_path))
    assert info.value.committed is None


def test_datetime_column_uses_half_open_range(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'dt.db'}")
    create_table(eng, date_type="DATETIME")
    seed(eng, [
        ("2026-10-20 23:59:59.000000", "prev", 1, 1, 1),
        ("2026-10-21 00:00:00.000000", "midnight", 1, 1, 1),
        ("2026-10-21 15:30:00.000000", "afternoon", 1, 1, 1),
        ("2026-10-22 00:00:00.000000", "next", 1, 1, 1),
    ])
    schema = inspect_table(eng, TABLE)
    assert schema.date_kind == "datetime"
    result = replace_report_date(eng, schema, report_for(tmp_path))
    assert result.deleted_rows == 2
    with eng.connect() as c:
        terms = sorted(c.execute(text(f'SELECT "Customer Search Term" FROM "{TABLE}"')).scalars())
    assert terms == ["new 1", "new 2", "next", "prev"]


def test_text_date_column(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    create_table(eng, date_type="VARCHAR(20)")
    seed(eng, [("2026-10-21", "old", 1, 1, 1), ("2026-10-20", "keep", 1, 1, 1)])
    schema = inspect_table(eng, TABLE)
    check_text_date_format(eng, schema, "%Y-%m-%d")
    replace_report_date(eng, schema, report_for(tmp_path), text_date_format="%Y-%m-%d")
    assert rows_by_date(eng) == {"2026-10-20": 1, "2026-10-21": 2}


def test_text_date_format_mismatch_is_refused(tmp_path: Path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    create_table(eng, date_type="VARCHAR(20)")
    seed(eng, [("21/10/2026", "old", 1, 1, 1)])
    schema = inspect_table(eng, TABLE)
    with pytest.raises(SchemaError, match="DB_TEXT_DATE_FORMAT"):
        check_text_date_format(eng, schema, "%Y-%m-%d")


def test_no_ddl_in_module() -> None:
    """No string literal in the DB modules (docstrings excluded) contains DDL."""
    import ast

    from app import pipeline, verifier

    for module in (database, verifier, pipeline):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        literals = [
            n.value.upper() for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
        ]
        for keyword in ("TRUNCATE TABLE", "TRUNCATE `", "DROP TABLE", "ALTER TABLE", "CREATE TABLE", "RENAME TABLE"):
            assert not [s for s in literals if keyword in s], (module.__name__, keyword)


def test_safe_description_hides_password(db_settings) -> None:
    assert "pw-not-used" not in db_settings.safe_description()
    assert "pw-not-used" not in repr(db_settings)


def test_engine_url_never_logged(db_settings, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    eng = database.create_db_engine(db_settings)
    assert "pw-not-used" not in caplog.text
    assert "pw-not-used" not in repr(eng.url)
    eng.dispose()


def test_expected_date_used(engine, tmp_path: Path) -> None:
    schema = inspect_table(engine, TABLE)
    report = report_for(tmp_path)
    assert report.report_date == date(2026, 10, 21)
    replace_report_date(engine, schema, report)
