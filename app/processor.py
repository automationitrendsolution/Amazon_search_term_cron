"""CSV validation and column mapping for the Amazon Search Term report.

The whole file is validated here, before any database connection is opened.
Nothing is fabricated: a value missing in the CSV stays missing (NULL), a
value that cannot be parsed fails the run, and columns we do not map are named
in the log rather than dropped silently.

Values are parsed into exact Python types -- ``date``, ``int``, ``Decimal``,
``str`` -- from the original text (``dtype=str``, no NA inference), so a
search term such as ``"null"`` or ``"NA"`` is kept as the text it is and money
values never pass through binary floating point.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from .exceptions import EmptyReportError, ValidationError
from .logging_setup import get_logger

logger = get_logger("processor")

# ---------------------------------------------------------------------------
# column contract
# ---------------------------------------------------------------------------

#: Amazon source column -> MySQL destination column (business mapping).
COLUMN_MAP: dict[str, str] = {
    "Date": "Date",
    "Portfolio name": "Portfolio name",
    "Campaign name": "Campaign Name",
    "Targeting": "Targeting",
    "Targeting match type": "Match Type",
    "Search term": "Customer Search Term",
    "Impressions": "Impressions",
    "Clicks": "Clicks",
    "Total cost": "Spend",
    "Sales": "Sales",
    "Purchases": "Units",
}
REQUIRED_SOURCE_COLUMNS: tuple[str, ...] = tuple(COLUMN_MAP)

#: Optional: mapped when the source has it, else NULL (if the table has the column).
VCPM_COLUMN = "Cost per 1,000 viewable impressions (VCPM)"
OPTIONAL_SOURCE_COLUMNS: dict[str, str] = {VCPM_COLUMN: VCPM_COLUMN}

TEXT_COLUMNS = ("Portfolio name", "Campaign name", "Targeting", "Targeting match type", "Search term")
COUNT_COLUMNS = ("Impressions", "Clicks", "Purchases")
MONEY_COLUMNS = ("Total cost", "Sales", VCPM_COLUMN)
#: Empty is a validation error for these; other columns may legitimately be empty.
#: Search term is not among them: Sponsored Display rows (ASIN/category targets)
#: have no shopper search term, so a blank one is stored as NULL.
NON_EMPTY_COLUMNS = ("Date", "Campaign name")
#: Identifies one search-term row for the duplicate check.
DUPLICATE_KEY = ("Date", "Campaign name", "Targeting", "Targeting match type", "Search term")

DEST_INTEGER_COLUMNS = tuple(COLUMN_MAP[c] for c in COUNT_COLUMNS)
DEST_DECIMAL_COLUMNS = ("Spend", "Sales", VCPM_COLUMN)
DEST_TEXT_COLUMNS = tuple(COLUMN_MAP[c] for c in TEXT_COLUMNS)

#: Tried in order when CSV_DATE_FORMAT is unset; ONE format must parse every
#: row.  Day-first numeric formats are deliberately absent (03/04 ambiguity).
DATE_FORMATS: tuple[str, ...] = (
    "%b %d, %Y",  # Oct 20, 2026  (seen in live Amazon Ads exports)
    "%B %d, %Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d-%b-%Y",
    "%Y%m%d",
)

#: Literal placeholders Amazon uses for "no value" in metric cells.
EMPTY_NUMERIC_TOKENS = frozenset({"", "-", "–", "—"})
_NUMBER = re.compile(
    r"^(?P<sign>[-+]?)\s*(?P<cur>[$₹€£¥])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)$"
)
_BINARY_SIGNATURES = (b"PK\x03\x04", b"%PDF", b"\x89PNG", b"\xd0\xcf\x11\xe0")


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


@dataclass
class ProcessedReport:
    source_path: Path
    report_date: date
    encoding: str
    #: One dict per CSV data row, keyed by DESTINATION column name.
    records: list[dict[str, object]]
    #: Destination columns present in ``records``.
    columns: list[str]
    vcpm_in_source: bool
    ignored_columns: list[str] = field(default_factory=list)
    exact_duplicate_rows: int = 0
    key_duplicate_rows: int = 0
    #: Destination numeric column -> sum of non-null values.
    totals: dict[str, Decimal] = field(default_factory=dict)
    #: Destination numeric column -> number of NULL values.
    null_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.records)

    def frame(self) -> pd.DataFrame:
        """Records as a DataFrame (destination column names)."""
        return pd.DataFrame.from_records(self.records, columns=self.columns)

    def summary(self) -> dict[str, object]:
        return {
            "file": self.source_path.name,
            "report_date": self.report_date.isoformat(),
            "rows": self.row_count,
            "encoding": self.encoding,
            "vcpm_in_source": self.vcpm_in_source,
            "ignored_columns": self.ignored_columns,
            "exact_duplicate_rows": self.exact_duplicate_rows,
            "key_duplicate_rows": self.key_duplicate_rows,
            "totals": {k: str(v) for k, v in self.totals.items()},
            "null_counts": self.null_counts,
        }


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def normalize_header(name: str) -> str:
    """Comparison key for a header: BOM/NBSP removed, whitespace collapsed, casefolded.

    Only whitespace and case are normalised, so distinct Amazon columns such as
    ``Purchases`` and ``Purchases (multi-touch)`` can never be confused.
    """
    cleaned = name.replace("﻿", "").replace(" ", " ")
    return " ".join(cleaned.split()).casefold()


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode CSV bytes: BOM-aware UTF-8/UTF-16, then cp1252 as a last resort."""
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as exc:
            raise ValidationError(f"File has a UTF-16 BOM but is not valid UTF-16: {exc}") from exc
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise ValidationError("File is neither UTF-8, UTF-16 nor Windows-1252 text") from exc
    logger.warning("CSV is not valid UTF-8; decoded as Windows-1252")
    return text, "cp1252"


def read_csv_frame(path: Path) -> tuple[pd.DataFrame, str]:
    """Read the file into an all-string DataFrame, checking its structure.

    Raises:
        ValidationError: missing/empty/binary/HTML file, ragged rows, or no header.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"Cannot read CSV {path}: {exc}") from exc
    if not raw.strip():
        raise ValidationError(f"CSV {path.name} is empty (0 bytes or whitespace only)")
    if raw.startswith(_BINARY_SIGNATURES):
        raise ValidationError(
            f"{path.name} is a binary file (ZIP/XLSX/PDF/image), not a CSV. "
            "Set the saved report's file format to CSV."
        )
    head = raw[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml")):
        raise ValidationError(f"{path.name} is an HTML/XML page, not a CSV (expired link or error page?)")

    text, encoding = decode_bytes(raw)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = [row for row in reader]
    except csv.Error as exc:
        raise ValidationError(f"Malformed CSV at line {reader.line_num}: {exc}") from exc

    # Fully blank lines (often a trailing newline) carry no data.
    numbered = [(i + 1, r) for i, r in enumerate(rows) if any(cell.strip() for cell in r)]
    if not numbered:
        raise ValidationError(f"CSV {path.name} has no header row")
    _, header = numbered[0]
    width = len(header)
    ragged = [f"line {n}: {len(r)} fields, header has {width}" for n, r in numbered[1:] if len(r) != width]
    if ragged:
        raise ValidationError(f"CSV {path.name} has rows with the wrong number of fields", ragged)
    frame = pd.DataFrame([r for _, r in numbered[1:]], columns=header, dtype=str)
    return frame, encoding


def map_headers(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return ``{canonical source name: actual header}`` and the unmapped headers.

    Raises:
        ValidationError: a required column is missing, or two headers collapse
            to the same name (ambiguous -- never resolved by guessing).
    """
    known = {normalize_header(c): c for c in (*REQUIRED_SOURCE_COLUMNS, *OPTIONAL_SOURCE_COLUMNS)}
    seen: dict[str, str] = {}
    problems: list[str] = []
    for header in headers:
        key = normalize_header(header)
        if not key:
            problems.append("a header cell is blank")
            continue
        if key in seen:
            problems.append(f"headers {seen[key]!r} and {header!r} are the same column after normalisation")
        seen[key] = header

    mapping: dict[str, str] = {}
    for key, canonical in known.items():
        if key in seen:
            mapping[canonical] = seen[key]
    missing = [c for c in REQUIRED_SOURCE_COLUMNS if c not in mapping]
    if missing:
        problems.append(f"missing required column(s): {', '.join(missing)}")
    if problems:
        raise ValidationError(
            "Unexpected report structure (is this a Search Term report with Time unit = Daily?)",
            problems + [f"headers found: {headers}"],
        )
    used = set(mapping.values())
    unmapped = [h for h in headers if h not in used]
    return mapping, unmapped


# ---------------------------------------------------------------------------
# value parsing
# ---------------------------------------------------------------------------


def parse_number(text: str) -> Decimal | None:
    """Parse one metric cell exactly.  ``""``/``"-"`` -> None.

    Accepts thousands separators and one leading currency symbol.  Raises
    ValueError on anything else (``"abc"``, ``"1.2.3"``, ``"12%"``, ``"1e5"``).
    """
    value = text.strip().replace(" ", "")
    if value in EMPTY_NUMERIC_TOKENS:
        return None
    match = _NUMBER.match(value)
    if not match:
        raise ValueError(f"not a number: {text!r}")
    try:
        number = Decimal(match.group("num").replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex guarantees digits
        raise ValueError(f"not a number: {text!r}") from exc
    return -number if match.group("sign") == "-" else number


def parse_dates(values: list[str], explicit_format: str | None = None) -> tuple[list[date], str]:
    """Parse every value with ONE format; return the dates and the format used."""
    formats = (explicit_format,) if explicit_format else DATE_FORMATS
    stripped = [v.strip() for v in values]
    for fmt in formats:
        try:
            return [datetime.strptime(v, fmt).date() for v in stripped], fmt
        except ValueError:
            continue
    bad = sorted({v for v in stripped if not _parses_any(v, formats)})[:10]
    raise ValidationError(
        "Malformed Date values"
        + (f" (CSV_DATE_FORMAT={explicit_format!r})" if explicit_format else ""),
        [f"unparseable date {v!r}" for v in bad] or ["dates use more than one format"],
    )


def _parses_any(value: str, formats: tuple[str, ...]) -> bool:
    for fmt in formats:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def process_csv(
    path: Path,
    expected_date: date | None,
    *,
    date_format: str | None = None,
    allow_empty: bool = False,
    duplicate_policy: str = "warn",
) -> ProcessedReport:
    """Validate ``path`` and return mapped records.  Raises ValidationError.

    ``expected_date`` is the date selected by the 12:30 IST rule; every row's
    Date must equal it.  ``None`` (``--validate-csv`` without
    ``--expected-date``) requires the file to contain exactly one date.
    """
    frame, encoding = read_csv_frame(path)
    mapping, unmapped = map_headers(list(frame.columns))
    if unmapped:
        logger.info(
            "Ignoring %d unmapped column(s): %s", len(unmapped), ", ".join(unmapped)
        )
    source = pd.DataFrame({canonical: frame[actual] for canonical, actual in mapping.items()})
    vcpm_in_source = VCPM_COLUMN in source.columns

    if source.empty:
        if not allow_empty:
            raise EmptyReportError(
                f"CSV {path.name} has a header but no data rows. Refusing to replace the date's "
                "rows with nothing (set ALLOW_EMPTY_REPORT=true if an empty day is legitimate)."
            )
        if expected_date is None:
            raise ValidationError("CSV has no data rows, so its date cannot be determined")
        logger.warning("CSV has no data rows; ALLOW_EMPTY_REPORT=true")
        return ProcessedReport(
            source_path=path,
            report_date=expected_date,
            encoding=encoding,
            records=[],
            columns=_dest_columns(vcpm_in_source),
            vcpm_in_source=vcpm_in_source,
            ignored_columns=unmapped,
            totals={c: Decimal(0) for c in _numeric_dest(vcpm_in_source)},
            null_counts={c: 0 for c in _numeric_dest(vcpm_in_source)},
        )

    problems: list[str] = []
    line_of = lambda idx: idx + 2  # noqa: E731 - header is line 1

    # -- required non-empty --------------------------------------------------
    for column in NON_EMPTY_COLUMNS:
        empty = source.index[source[column].str.strip() == ""].tolist()
        problems += [f"line {line_of(i)}: {column} is empty" for i in empty[:20]]
    blank_terms = int((source["Search term"].str.strip() == "").sum())
    if blank_terms:
        logger.info("%d row(s) have no search term (e.g. Sponsored Display); stored as NULL", blank_terms)

    # -- dates ---------------------------------------------------------------
    report_dates: list[date] = []
    if not any(p.endswith("Date is empty") for p in problems):
        try:
            report_dates, used_format = parse_dates(source["Date"].tolist(), date_format)
            logger.info("Date column parsed with format %s", used_format)
        except ValidationError as exc:
            problems += exc.problems
    distinct = sorted(set(report_dates))
    if report_dates:
        if expected_date is not None and distinct != [expected_date]:
            problems.append(
                f"report contains date(s) {[d.isoformat() for d in distinct]} but the selected "
                f"report date is {expected_date.isoformat()} -- refusing to load another day's data"
            )
        elif expected_date is None and len(distinct) != 1:
            problems.append(f"report contains {len(distinct)} dates {distinct}; expected exactly one")

    # -- numbers -------------------------------------------------------------
    numeric: dict[str, list[Decimal | None]] = {}
    for column in (*COUNT_COLUMNS, *MONEY_COLUMNS):
        if column not in source.columns:
            continue
        parsed: list[Decimal | None] = []
        for idx, raw in source[column].items():
            try:
                value = parse_number(raw)
            except ValueError:
                problems.append(f"line {line_of(idx)}: {column} {raw!r} is not a valid number")
                parsed.append(None)
                continue
            if value is not None and column in COUNT_COLUMNS:
                if value != value.to_integral_value():
                    problems.append(f"line {line_of(idx)}: {column} {raw!r} is not a whole number")
                elif value < 0:
                    problems.append(f"line {line_of(idx)}: {column} {raw!r} is negative")
            parsed.append(value)
        numeric[column] = parsed

    if problems:
        raise ValidationError(f"CSV {path.name} failed validation ({len(problems)} problem(s))", problems)

    report_date = distinct[0]

    # -- duplicates ----------------------------------------------------------
    exact_dupes = int(source.duplicated(keep="first").sum())
    key_dupes = int(source.duplicated(subset=list(DUPLICATE_KEY), keep="first").sum())
    warnings: list[str] = []
    if exact_dupes:
        warnings.append(f"{exact_dupes} row(s) are exact duplicates of an earlier row")
    if key_dupes:
        warnings.append(
            f"{key_dupes} row(s) repeat an earlier (date, campaign, targeting, match type, "
            "search term) key -- Amazon emits one row per ad group, which is not mapped"
        )
    if warnings:
        if duplicate_policy == "fail":
            raise ValidationError("Duplicate rows found and DUPLICATE_POLICY=fail", warnings)
        # Expected for an hourly, per-ad-group report: hour and ad group are not mapped.
        logger.info(
            "Repeated rows kept as-is (hour/ad group not mapped): %d exact, %d same key", exact_dupes, key_dupes
        )

    negatives = {
        c: sum(1 for v in numeric[c] if v is not None and v < 0) for c in MONEY_COLUMNS if c in numeric
    }
    for column, count in negatives.items():
        if count:
            warnings.append(f"{count} negative value(s) in {column}")
            logger.warning("%d negative value(s) in %s (kept as reported)", count, column)

    # -- build destination records -------------------------------------------
    records: list[dict[str, object]] = []
    for pos, idx in enumerate(source.index):
        record: dict[str, object] = {"Date": report_dates[pos]}
        for column in TEXT_COLUMNS:
            text = source.at[idx, column]
            # Kept verbatim (internal spacing and case preserved); only a cell
            # that is entirely blank becomes NULL.
            record[COLUMN_MAP[column]] = text if text.strip() else None
        for column, values in numeric.items():
            value = values[pos]
            dest = COLUMN_MAP.get(column, column)
            record[dest] = int(value) if (value is not None and column in COUNT_COLUMNS) else value
        records.append(record)

    totals: dict[str, Decimal] = {}
    nulls: dict[str, int] = {}
    for column, values in numeric.items():
        dest = COLUMN_MAP.get(column, column)
        totals[dest] = sum((v for v in values if v is not None), Decimal(0))
        nulls[dest] = sum(1 for v in values if v is None)

    report = ProcessedReport(
        source_path=path,
        report_date=report_date,
        encoding=encoding,
        records=records,
        columns=_dest_columns(vcpm_in_source),
        vcpm_in_source=vcpm_in_source,
        ignored_columns=unmapped,
        exact_duplicate_rows=exact_dupes,
        key_duplicate_rows=key_dupes,
        totals=totals,
        null_counts=nulls,
        warnings=warnings,
    )
    logger.info(
        "CSV valid: %s row(s) for %s | %s",
        f"{report.row_count:,}",
        report_date,
        ", ".join(f"{k} {v:,}" for k, v in totals.items()),
    )
    return report


def _dest_columns(vcpm: bool) -> list[str]:
    columns = list(COLUMN_MAP.values())
    return columns + [VCPM_COLUMN] if vcpm else columns


def _numeric_dest(vcpm: bool) -> list[str]:
    cols = [*DEST_INTEGER_COLUMNS, "Spend", "Sales"]
    return cols + [VCPM_COLUMN] if vcpm else cols
