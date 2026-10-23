"""CSV mapping and validation."""

from __future__ import annotations

import csv
import io

from decimal import Decimal
from pathlib import Path

import pytest

from app.exceptions import ValidationError
from app.processor import VCPM_COLUMN, map_headers, normalize_header, parse_number, process_csv

from .conftest import HEADER, REPORT_DATE, make_row, write_csv


def csv_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def test_header_mapping_to_destination_columns(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row()])
    report = process_csv(path, REPORT_DATE)
    record = report.records[0]
    assert record == {
        "Date": REPORT_DATE,
        "Portfolio name": "Footwear",
        "Campaign Name": "SP - Shoes",
        "Targeting": "shoes",
        "Match Type": "BROAD",
        "Customer Search Term": "running shoes",
        "Impressions": 100,
        "Clicks": 5,
        "Spend": Decimal("12.34"),
        "Sales": Decimal("45.60"),
        "Units": 2,
    }
    assert report.vcpm_in_source is False
    assert report.encoding == "utf-8"


def test_headers_with_bom_whitespace_and_case(tmp_path: Path) -> None:
    messy = ["﻿ Date ", "Portfolio  name", "CAMPAIGN NAME", "Targeting", "Targeting match type ",
             "Search term", "Impressions", "Clicks", "Total cost", "Sales", "Purchases"]
    path = write_csv(tmp_path / "r.csv", [make_row()], header=messy, bom=False)
    assert process_csv(path, REPORT_DATE).row_count == 1


def test_distinct_columns_are_not_confused() -> None:
    headers = HEADER + ["Purchases (multi-touch)", "Sales (multi-touch)"]
    mapping, unmapped = map_headers(headers)
    assert mapping["Purchases"] == "Purchases"
    assert unmapped == ["Purchases (multi-touch)", "Sales (multi-touch)"]
    assert normalize_header("Purchases") != normalize_header("Purchases (multi-touch)")


def test_duplicate_headers_after_normalisation_rejected() -> None:
    with pytest.raises(ValidationError, match="same column"):
        map_headers(HEADER + [" date"])


def test_unexpected_columns_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="stc")
    path = write_csv(tmp_path / "r.csv", [make_row() + ["x"]], header=HEADER + ["Ad group name"])
    report = process_csv(path, REPORT_DATE)
    assert report.ignored_columns == ["Ad group name"]
    assert "Ad group name" in caplog.text


def test_vcpm_mapped_when_present(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row() + ["3.21"]], header=HEADER + [VCPM_COLUMN])
    report = process_csv(path, REPORT_DATE)
    assert report.vcpm_in_source
    assert report.records[0][VCPM_COLUMN] == Decimal("3.21")


def test_missing_column(tmp_path: Path) -> None:
    header = [h for h in HEADER if h != "Search term"]
    row = make_row()
    del row[HEADER.index("Search term")]
    path = write_csv(tmp_path / "r.csv", [row], header=header)
    with pytest.raises(ValidationError, match="Search term"):
        process_csv(path, REPORT_DATE)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"   \r\n", "empty"),
        (b"PK\x03\x04binary", "binary"),
        (b"<!DOCTYPE html><html>sign in</html>", "HTML"),
    ],
)
def test_empty_and_non_csv_files(tmp_path: Path, content: bytes, message: str) -> None:
    path = tmp_path / "r.csv"
    path.write_bytes(content)
    with pytest.raises(ValidationError, match=message):
        process_csv(path, REPORT_DATE)


def test_header_only_rejected_by_default(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [])
    with pytest.raises(ValidationError, match="no data rows"):
        process_csv(path, REPORT_DATE)
    assert process_csv(path, REPORT_DATE, allow_empty=True).row_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impressions", "abc"),
        ("clicks", "1.5"),
        ("clicks", "-3"),
        ("cost", "12.3.4"),
        ("sales", "12%"),
        ("purchases", "1e3"),
    ],
)
def test_invalid_numeric_values(tmp_path: Path, field: str, value: str) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(**{field: value})])
    with pytest.raises(ValidationError, match="line 2"):
        process_csv(path, REPORT_DATE)


def test_number_parsing() -> None:
    assert parse_number("1,234.50") == Decimal("1234.50")
    assert parse_number("$12.30") == Decimal("12.30")
    assert parse_number(" ") is None
    assert parse_number("-") is None
    assert parse_number("-4.10") == Decimal("-4.10")
    with pytest.raises(ValueError):
        parse_number("12,34")


def test_empty_metric_stays_null_not_zero(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(sales="", impressions="-")])
    report = process_csv(path, REPORT_DATE)
    assert report.records[0]["Sales"] is None
    assert report.records[0]["Impressions"] is None
    assert report.null_counts["Sales"] == 1


def test_malformed_date(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(day="2026-13-45")])
    with pytest.raises(ValidationError, match="unparseable date '2026-13-45'"):
        process_csv(path, REPORT_DATE)


def test_mixed_date_formats_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(day="Oct 21, 2026"), make_row(day="2026-10-21", term="b")])
    with pytest.raises(ValidationError):
        process_csv(path, REPORT_DATE)


@pytest.mark.parametrize("day", ["Oct 21, 2026", "2026-10-21", "10/21/2026", "October 21, 2026"])
def test_supported_date_formats(tmp_path: Path, day: str) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(day=day)])
    assert process_csv(path, REPORT_DATE).report_date == REPORT_DATE


def test_selected_date_mismatch_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(day="Oct 20, 2026")])
    with pytest.raises(ValidationError, match="refusing to load another day"):
        process_csv(path, REPORT_DATE)


def test_multiple_dates_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(), make_row(day="Oct 20, 2026", term="b")])
    with pytest.raises(ValidationError):
        process_csv(path, REPORT_DATE)
    with pytest.raises(ValidationError, match="exactly one"):
        process_csv(path, None)


def test_required_text_empty(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(campaign="  ")])
    with pytest.raises(ValidationError, match="Campaign name is empty"):
        process_csv(path, REPORT_DATE)


def test_blank_search_term_stored_as_null(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(term=""), make_row(term="b")])
    report = process_csv(path, REPORT_DATE)
    assert [r["Customer Search Term"] for r in report.records] == [None, "b"]


def test_ragged_rows_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row()[:-1]])
    with pytest.raises(ValidationError, match="wrong number of fields"):
        process_csv(path, REPORT_DATE)


def test_search_terms_preserved_verbatim(tmp_path: Path) -> None:
    terms = ["null", "NA", "  shoes  for men", "b0c1x2y3z4", "café crème", "N/A"]
    path = write_csv(tmp_path / "r.csv", [make_row(term=t) for t in terms])
    report = process_csv(path, REPORT_DATE)
    assert [r["Customer Search Term"] for r in report.records] == terms


def test_portfolio_blank_becomes_null(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(portfolio="")])
    assert process_csv(path, REPORT_DATE).records[0]["Portfolio name"] is None


def test_duplicates_warn_by_default_and_can_fail(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(), make_row()])
    report = process_csv(path, REPORT_DATE)
    assert report.row_count == 2  # never silently deduplicated
    assert report.exact_duplicate_rows == 1
    with pytest.raises(ValidationError, match="Duplicate"):
        process_csv(path, REPORT_DATE, duplicate_policy="fail")


def test_cp1252_fallback(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_bytes(csv_text([make_row(term="café")]).encode("cp1252"))
    report = process_csv(path, REPORT_DATE)
    assert report.encoding == "cp1252"
    assert report.records[0]["Customer Search Term"] == "café"


def test_utf16_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_bytes(csv_text([make_row()]).encode("utf-16"))
    assert process_csv(path, REPORT_DATE).encoding == "utf-16"


def test_totals(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "r.csv", [make_row(cost="1.10"), make_row(term="b", cost="2.20")])
    assert process_csv(path, REPORT_DATE).totals["Spend"] == Decimal("3.30")
