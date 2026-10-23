"""MySQL access: schema inspection, compatibility checks, transactional import.

The destination ``powerbidb.`search term report``` is a production object.
This module never issues DDL -- no CREATE/ALTER/DROP/TRUNCATE/RENAME -- and
only ever deletes rows matching the selected report date.

Import contract (:func:`replace_report_date`):

1. the caller has already validated the whole CSV;
2. the live schema is inspected and every mapped column checked;
3. the table's storage engine must be transactional (InnoDB);
4. on ONE connection, in ONE transaction: fingerprint the other dates, delete
   the selected date, insert the new rows, re-count the date;
5. commit only when the in-transaction count equals the CSV row count;
   anything else rolls back.

The table name contains spaces, so every statement is built from a reflected
SQLAlchemy ``Table``; the dialect quotes identifiers.  Bind parameters get
synthetic names (``p0``, ``p1``...) because column names such as
``Cost per 1,000 viewable impressions (VCPM)`` are not safe bind names.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import (
    Column,
    MetaData,
    Table,
    and_,
    bindparam,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    not_,
    or_,
    select,
    text,
)
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from .config import DatabaseSettings
from .exceptions import DatabaseError, SchemaError, ValidationError
from .logging_setup import get_logger
from .processor import (
    COLUMN_MAP,
    DEST_DECIMAL_COLUMNS,
    DEST_INTEGER_COLUMNS,
    DEST_TEXT_COLUMNS,
    VCPM_COLUMN,
    ProcessedReport,
)

logger = get_logger("database")

DATE_COLUMN = "Date"
REQUIRED_DEST_COLUMNS: tuple[str, ...] = tuple(COLUMN_MAP.values())
TRANSACTIONAL_ENGINES = frozenset({"innodb"})
INSERT_CHUNK_SIZE = 1_000

_INT_BOUNDS = {
    "tinyint": 2**7,
    "smallint": 2**15,
    "mediumint": 2**23,
    "integer": 2**31,
    "int": 2**31,
    "bigint": 2**63,
}


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def create_db_engine(settings: DatabaseSettings) -> Engine:
    """SQLAlchemy engine on the MySQL Connector driver.  The URL is never logged."""
    url = URL.create(
        "mysql+mysqlconnector",
        username=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.name,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1_800,
        hide_parameters=True,  # never echo row values / secrets in exception text
        connect_args=settings.connect_args(),
    )
    if settings.session_time_zone:
        tz = settings.session_time_zone

        @event.listens_for(engine, "connect")
        def _set_time_zone(dbapi_connection, _record):  # pragma: no cover - needs MySQL
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET time_zone = %s", (tz,))
            finally:
                cursor.close()

    logger.info("Database target: %s", settings.safe_description())
    return engine


# ---------------------------------------------------------------------------
# schema inspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    #: date | datetime | integer | decimal | float | text | other
    kind: str
    type_name: str
    nullable: bool
    has_default: bool
    autoincrement: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    unsigned: bool = False

    def describe(self) -> str:
        return f"{self.name!r} {self.type_name}{'' if self.nullable else ' NOT NULL'}"


@dataclass
class TableSchema:
    table: Table
    columns: dict[str, ColumnInfo]
    engine_name: str
    #: Destination columns this import writes (mapped + VCPM when present).
    write_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def date_kind(self) -> str:
        return self.columns[DATE_COLUMN].kind

    @property
    def has_vcpm(self) -> bool:
        return VCPM_COLUMN in self.columns


def classify_type(col_type: sqltypes.TypeEngine) -> tuple[str, str]:
    """Map a reflected SQLAlchemy type to a coarse kind and a display name."""
    try:
        name = str(col_type.compile()).lower()
    except Exception:  # pragma: no cover - exotic types
        name = type(col_type).__name__.lower()
    if isinstance(col_type, sqltypes.DateTime):  # DATETIME and TIMESTAMP
        return "datetime", name
    if isinstance(col_type, sqltypes.Date):
        return "date", name
    if isinstance(col_type, sqltypes.Integer):
        return "integer", name
    if isinstance(col_type, sqltypes.Float):  # FLOAT/DOUBLE/REAL (subclass of Numeric)
        return "float", name
    if isinstance(col_type, sqltypes.Numeric):
        return "decimal", name
    if isinstance(col_type, sqltypes.String):  # CHAR/VARCHAR/TEXT
        return "text", name
    return "other", name


def table_engine(connection: Connection, schema_name: str | None, table_name: str) -> str:
    """Storage engine from INFORMATION_SCHEMA (MySQL); 'sqlite' for test databases."""
    dialect = connection.dialect.name
    if dialect == "sqlite":
        return "sqlite"
    if dialect != "mysql":
        raise SchemaError(f"Unsupported database dialect {dialect!r}")
    row = connection.execute(
        text(
            "SELECT ENGINE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = COALESCE(:schema, DATABASE()) AND TABLE_NAME = :table"
        ),
        {"schema": schema_name, "table": table_name},
    ).first()
    if row is None:
        raise SchemaError(f"Table `{table_name}` was not found in INFORMATION_SCHEMA.TABLES")
    return str(row[0] or "")


def inspect_table(engine: Engine, table_name: str, *, schema_name: str | None = None) -> TableSchema:
    """Reflect the destination table and check its engine.  Read-only.

    Raises:
        SchemaError: missing table, required column absent, or non-transactional engine.
        DatabaseError: connection or query failure.
    """
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            if not inspector.has_table(table_name, schema=schema_name):
                raise SchemaError(f"Destination table `{table_name}` does not exist; it is not created automatically")
            raw_columns = inspector.get_columns(table_name, schema=schema_name)
            primary_key = inspector.get_pk_constraint(table_name, schema=schema_name).get("constrained_columns") or []
            dialect = connection.dialect.name
            engine_name = table_engine(connection, schema_name, table_name)
            table = Table(table_name, MetaData(), schema=schema_name, autoload_with=connection)
    except SchemaError:
        raise
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not inspect `{table_name}`: {_safe_error(exc)}") from exc

    columns: dict[str, ColumnInfo] = {}
    for raw in raw_columns:
        col_type = raw["type"]
        kind, type_name = classify_type(col_type)
        # MySQL reports AUTO_INCREMENT as autoincrement=True; in SQLite (tests)
        # a single INTEGER PRIMARY KEY is the rowid and fills itself.
        rowid = dialect == "sqlite" and kind == "integer" and primary_key == [raw["name"]]
        columns[raw["name"]] = ColumnInfo(
            name=raw["name"],
            kind=kind,
            type_name=type_name,
            nullable=bool(raw.get("nullable", True)),
            has_default=raw.get("default") is not None,
            autoincrement=raw.get("autoincrement") is True or rowid,
            length=getattr(col_type, "length", None),
            precision=getattr(col_type, "precision", None),
            scale=getattr(col_type, "scale", None),
            unsigned=bool(getattr(col_type, "unsigned", False)),
        )

    if engine_name.lower() not in TRANSACTIONAL_ENGINES and engine_name != "sqlite":
        raise SchemaError(
            f"`{table_name}` uses storage engine {engine_name!r}, which is not transactional. "
            "Delete + insert could not be rolled back as a unit, so the import is refused. "
            "(The schema is never changed automatically.)"
        )
    schema = TableSchema(table=table, columns=columns, engine_name=engine_name)
    check_compatibility(schema)
    return schema


# -- compatibility -----------------------------------------------------------

_ALLOWED_KINDS: dict[str, frozenset[str]] = {
    DATE_COLUMN: frozenset({"date", "datetime", "text"}),
    **{c: frozenset({"integer", "decimal", "float", "text"}) for c in DEST_INTEGER_COLUMNS},
    **{c: frozenset({"decimal", "float", "text"}) for c in DEST_DECIMAL_COLUMNS},
    **{c: frozenset({"text"}) for c in DEST_TEXT_COLUMNS},
}


def check_compatibility(schema: TableSchema) -> None:
    """Every mapped column must exist with a compatible type.  Raises SchemaError."""
    problems: list[str] = []
    for name in REQUIRED_DEST_COLUMNS:
        column = schema.columns.get(name)
        if column is None:
            close = [c for c in schema.columns if c.casefold() == name.casefold()]
            hint = f" (found {close[0]!r}: column names are matched exactly)" if close else ""
            problems.append(f"required column {name!r} is missing{hint}")
            continue
        if column.kind not in _ALLOWED_KINDS[name]:
            problems.append(
                f"column {column.describe()} is incompatible: expected one of "
                f"{sorted(_ALLOWED_KINDS[name])}"
            )
    if schema.has_vcpm:
        vcpm = schema.columns[VCPM_COLUMN]
        if vcpm.kind not in _ALLOWED_KINDS[VCPM_COLUMN]:
            problems.append(f"column {vcpm.describe()} is incompatible with a money value")

    written = set(REQUIRED_DEST_COLUMNS) | ({VCPM_COLUMN} if schema.has_vcpm else set())
    for column in schema.columns.values():
        if column.name in written:
            continue
        if not column.nullable and not column.has_default and not column.autoincrement:
            problems.append(
                f"unmapped column {column.describe()} has no default; inserts would fail"
            )

    if problems:
        raise SchemaError(
            "Destination schema is not compatible with the Search Term mapping "
            "(nothing was changed):\n  - " + "\n  - ".join(problems)
        )
    schema.write_columns = [*REQUIRED_DEST_COLUMNS] + ([VCPM_COLUMN] if schema.has_vcpm else [])
    for name in schema.write_columns:
        column = schema.columns[name]
        if name not in (DATE_COLUMN, *DEST_TEXT_COLUMNS) and column.kind == "text":
            schema.warnings.append(f"numeric value {name!r} is stored in text column {column.type_name}")
        if column.kind == "float":
            schema.warnings.append(f"{name!r} is {column.type_name}; values are stored as binary floating point")
    for message in schema.warnings:
        logger.warning("Schema note: %s", message)


# ---------------------------------------------------------------------------
# date predicate and value conversion
# ---------------------------------------------------------------------------


def date_predicate(schema: TableSchema, report_date: date, text_date_format: str):
    """WHERE clause selecting exactly ``report_date``'s rows.

    DATE      -> equality
    DATETIME/TIMESTAMP -> half-open [midnight, next midnight) in the session zone
    text      -> equality with the DB_TEXT_DATE_FORMAT rendering
    """
    column = schema.table.c[DATE_COLUMN]
    kind = schema.date_kind
    if kind == "date":
        return column == bindparam("report_date", report_date, type_=sqltypes.Date())
    if kind == "datetime":
        start = datetime.combine(report_date, time.min)
        return and_(
            column >= bindparam("range_start", start, type_=sqltypes.DateTime()),
            column < bindparam("range_end", start + timedelta(days=1), type_=sqltypes.DateTime()),
        )
    return column == bindparam("report_date_text", report_date.strftime(text_date_format))


def other_dates_predicate(schema: TableSchema, report_date: date, text_date_format: str):
    """Every row NOT on the selected date (NULL dates included)."""
    column = schema.table.c[DATE_COLUMN]
    return or_(not_(date_predicate(schema, report_date, text_date_format)), column.is_(None))


def convert_value(column: ColumnInfo, value: object, text_date_format: str) -> object:
    """Python value for ``column``; raises ValueError when it would not fit."""
    if value is None:
        if not column.nullable and not column.has_default:
            raise ValueError(f"NULL into NOT NULL column {column.name!r}")
        return None
    if column.name == DATE_COLUMN:
        assert isinstance(value, date)
        if column.kind == "date":
            return value
        if column.kind == "datetime":
            return datetime.combine(value, time.min)
        return _fit_text(column, value.strftime(text_date_format))

    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        number = Decimal(value)
        if column.kind == "integer":
            if number != number.to_integral_value():
                raise ValueError(f"{column.name!r} is an integer column but value {number} has decimals")
            bound = _INT_BOUNDS.get(column.type_name.split("(")[0].split()[0], 2**63)
            low, high = (0, 2 * bound - 1) if column.unsigned else (-bound, bound - 1)
            if not low <= number <= high:
                raise ValueError(f"{number} is out of range for {column.describe()}")
            return int(number)
        if column.kind == "decimal":
            return _fit_decimal(column, number)
        if column.kind == "float":
            return float(number)
        if column.kind == "text":
            return _fit_text(column, format(number, "f"))
        raise ValueError(f"cannot store a number in {column.describe()}")

    if isinstance(value, str):
        if column.kind != "text":
            raise ValueError(f"cannot store text in {column.describe()}")
        return _fit_text(column, value)
    raise ValueError(f"unsupported value type {type(value).__name__} for {column.name!r}")


def _fit_text(column: ColumnInfo, value: str) -> str:
    if column.length is not None and len(value) > column.length:
        raise ValueError(
            f"value of {len(value)} characters exceeds {column.describe()} "
            "(MySQL would truncate or reject it)"
        )
    return value


def _fit_decimal(column: ColumnInfo, number: Decimal) -> Decimal:
    scale = column.scale if column.scale is not None else 0
    try:
        quantized = number.quantize(Decimal(1).scaleb(-scale))
    except InvalidOperation as exc:
        raise ValueError(f"{number} cannot be represented in {column.describe()}") from exc
    if quantized != number:
        raise ValueError(f"{number} has more than {scale} decimal place(s) for {column.describe()}")
    if column.precision is not None:
        integer_digits = len(quantized.as_tuple().digits) - scale
        if integer_digits > column.precision - scale:
            raise ValueError(f"{number} exceeds the precision of {column.describe()}")
    if column.unsigned and quantized < 0:
        raise ValueError(f"negative {number} for unsigned {column.describe()}")
    return quantized


def build_rows(schema: TableSchema, report: ProcessedReport, text_date_format: str) -> list[dict[str, object]]:
    """Convert validated records to bind dicts (``p0``..``pN``) for the INSERT.

    VCPM is written as NULL when the source lacks it but the table has it.
    Raises ValidationError (nothing has been written yet) on any value that
    does not fit its destination column.
    """
    problems: list[str] = []
    rows: list[dict[str, object]] = []
    for line, record in enumerate(report.records, start=2):
        row: dict[str, object] = {}
        for i, name in enumerate(schema.write_columns):
            value = record.get(name) if name in record else None  # VCPM absent -> NULL
            try:
                row[f"p{i}"] = convert_value(schema.columns[name], value, text_date_format)
            except ValueError as exc:
                problems.append(f"line {line}: {exc}")
        rows.append(row)
        if len(problems) > 50:
            break
    if problems:
        raise ValidationError("CSV values do not fit the destination schema", problems)
    return rows


def check_text_date_format(engine: Engine, schema: TableSchema, text_date_format: str, sample: int = 50) -> None:
    """For a text Date column, existing values must use DB_TEXT_DATE_FORMAT.

    Otherwise the date-scoped DELETE would miss the old rows and the import
    would silently duplicate them.
    """
    if schema.date_kind != "text":
        return
    column = schema.table.c[DATE_COLUMN]
    try:
        with engine.connect() as connection:
            values = connection.execute(
                select(column).where(column.is_not(None)).distinct().limit(sample)
            ).scalars().all()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not sample Date values: {_safe_error(exc)}") from exc
    if not values:
        logger.info("Table has no Date values yet; text dates will be written as %s", text_date_format)
        return
    good = 0
    for value in values:
        try:
            datetime.strptime(str(value).strip(), text_date_format)
            good += 1
        except ValueError:
            pass
    if good != len(values):
        raise SchemaError(
            f"`Date` is a text column and {len(values) - good} of {len(values)} sampled values do not "
            f"match DB_TEXT_DATE_FORMAT={text_date_format!r} (e.g. {values[0]!r}). Set "
            "DB_TEXT_DATE_FORMAT to the stored format; a mismatch would leave old rows behind."
        )


# ---------------------------------------------------------------------------
# fingerprints (used before and after the import)
# ---------------------------------------------------------------------------


def fingerprint_columns(schema: TableSchema) -> list[str]:
    """Numeric-typed destination columns whose SUM is part of a fingerprint."""
    return [
        c
        for c in (*DEST_INTEGER_COLUMNS, "Spend", "Sales")
        if schema.columns[c].kind in {"integer", "decimal"}
    ]


def other_dates_fingerprint(
    connection: Connection, schema: TableSchema, report_date: date, text_date_format: str
) -> dict[str, str]:
    """Row count and numeric sums of every row outside the selected date."""
    table = schema.table
    sums = fingerprint_columns(schema)
    query = select(
        func.count().label("rows"), *[func.sum(table.c[c]).label(f"s{i}") for i, c in enumerate(sums)]
    ).where(other_dates_predicate(schema, report_date, text_date_format))
    result = connection.execute(query).one()
    fingerprint = {"rows": str(result[0])}
    for i, name in enumerate(sums):
        value = result[i + 1]
        fingerprint[f"sum:{name}"] = _norm_number(value)
    return fingerprint


def _norm_number(value: object) -> str:
    if value is None:
        return "0"
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:  # pragma: no cover
        return str(value)


def count_for_date(connection: Connection, schema: TableSchema, report_date: date, text_date_format: str) -> int:
    query = select(func.count()).select_from(schema.table).where(
        date_predicate(schema, report_date, text_date_format)
    )
    return int(connection.execute(query).scalar_one())


# ---------------------------------------------------------------------------
# the transactional import
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    report_date: date
    deleted_rows: int
    inserted_rows: int
    other_dates_before: dict[str, str]
    committed: bool


def replace_report_date(
    engine: Engine,
    schema: TableSchema,
    report: ProcessedReport,
    *,
    text_date_format: str = "%Y-%m-%d",
    rows: list[dict[str, object]] | None = None,
) -> ImportResult:
    """Delete the selected date's rows and insert the CSV's, atomically.

    Raises:
        DatabaseError: ``committed=False`` when the transaction was rolled back
            (no change), ``committed=None`` when COMMIT itself failed and the
            outcome is unknown.
        ValidationError: a value does not fit the schema (checked before any write).
    """
    report_date = report.report_date
    bind_rows = rows if rows is not None else build_rows(schema, report, text_date_format)
    table = schema.table
    statement = insert(table).values(
        {table.c[name]: bindparam(f"p{i}") for i, name in enumerate(schema.write_columns)}
    )

    try:
        connection = engine.connect()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not connect to MySQL: {_safe_error(exc)}", committed=False) from exc

    with connection:
        transaction = connection.begin()
        try:
            before = other_dates_fingerprint(connection, schema, report_date, text_date_format)
            deleted = connection.execute(
                delete(table).where(date_predicate(schema, report_date, text_date_format))
            ).rowcount
            logger.info("Deleted %s existing row(s) for %s (uncommitted)", deleted, report_date)
            for chunk in _chunks(bind_rows, INSERT_CHUNK_SIZE):
                connection.execute(statement, chunk)
            in_transaction = count_for_date(connection, schema, report_date, text_date_format)
            if in_transaction != len(bind_rows):
                raise DatabaseError(
                    f"After insert the table holds {in_transaction} row(s) for {report_date}, "
                    f"expected {len(bind_rows)}; rolling back",
                    committed=False,
                )
        except Exception as exc:
            try:
                transaction.rollback()
                logger.error("Transaction ROLLED BACK for %s: %s", report_date, _safe_error(exc))
            except Exception as rollback_exc:  # pragma: no cover - connection gone
                logger.error("Rollback failed (%s); MySQL discards the uncommitted work", rollback_exc)
            if isinstance(exc, (DatabaseError, ValidationError)):
                raise
            raise DatabaseError(
                f"Import failed and was rolled back; no rows were changed: {_safe_error(exc)}",
                committed=False,
            ) from exc

        try:
            transaction.commit()
        except Exception as exc:
            raise DatabaseError(
                f"COMMIT failed for {report_date}: {_safe_error(exc)}. The outcome is UNKNOWN; "
                "the next run verifies the table before doing anything else.",
                committed=None,
            ) from exc

    logger.info(
        "Transaction COMMITTED for %s: deleted=%s inserted=%d", report_date, deleted, len(bind_rows)
    )
    return ImportResult(
        report_date=report_date,
        deleted_rows=int(deleted if deleted is not None and deleted >= 0 else -1),
        inserted_rows=len(bind_rows),
        other_dates_before=before,
        committed=True,
    )


def _chunks(items: list[dict[str, object]], size: int) -> Iterable[list[dict[str, object]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _safe_error(exc: BaseException) -> str:
    """Exception text without SQL parameter values (rows) or connection URLs."""
    message = str(getattr(exc, "orig", None) or exc)
    return message.split("\n[SQL:")[0].split("\n(Background on")[0][:500]


# ---------------------------------------------------------------------------
# --check-db
# ---------------------------------------------------------------------------


def describe_schema(schema: TableSchema) -> list[str]:
    lines = [f"engine: {schema.engine_name}", f"date column kind: {schema.date_kind}"]
    for column in schema.columns.values():
        mark = "*" if column.name in schema.write_columns else " "
        lines.append(f" {mark} {column.describe()} [{column.kind}]")
    lines.append(f"VCPM column present: {schema.has_vcpm}")
    return lines


def column_values_for_date(
    connection: Connection, schema: TableSchema, names: list[str], report_date: date, text_date_format: str
) -> list[tuple]:
    columns: list[Column] = [schema.table.c[n] for n in names]
    return list(
        connection.execute(select(*columns).where(date_predicate(schema, report_date, text_date_format))).all()
    )
