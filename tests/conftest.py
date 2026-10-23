"""Shared fixtures.

Tests never touch Amazon or the production MySQL server.  Database tests use
a throw-away SQLite file whose table reproduces the production names exactly
(``search term report`` with spaced/punctuated column names), which exercises
identifier quoting, the transaction and the date-scoped delete.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import DatabaseSettings, Settings, load_settings
from app.processor import VCPM_COLUMN

TABLE = "search term report"

HEADER = [
    "Date",
    "Portfolio name",
    "Campaign name",
    "Targeting",
    "Targeting match type",
    "Search term",
    "Impressions",
    "Clicks",
    "Total cost",
    "Sales",
    "Purchases",
]


def make_row(
    day: str = "Oct 21, 2026",
    term: str = "running shoes",
    *,
    campaign: str = "SP - Shoes",
    impressions: str = "100",
    clicks: str = "5",
    cost: str = "12.34",
    sales: str = "45.60",
    purchases: str = "2",
    portfolio: str = "Footwear",
    targeting: str = "shoes",
    match: str = "BROAD",
) -> list[str]:
    return [day, portfolio, campaign, targeting, match, term, impressions, clicks, cost, sales, purchases]


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None, *, bom: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header or HEADER)
        writer.writerows(rows)
    return path


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Never read the developer's real .env during tests.
    monkeypatch.setattr("app.config.DEFAULT_ENV_FILE", tmp_path / "no.env")
    for key in ("DOWNLOAD", "PROCESSED", "FAILED", "SCREENSHOT", "SESSION", "STATE", "LOG"):
        monkeypatch.setenv(f"{key}_DIR", str(tmp_path / key.lower()))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "state" / "run.lock"))
    monkeypatch.setenv("REPORT_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("AMAZON_ACCOUNT_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("TODAY_REPORT_CUTOFF", "12:30")
    monkeypatch.setenv("REPORT_ID_TODAY", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("REPORT_ID_YESTERDAY", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("AMAZON_ENTITY_ID", "ENTITYTEST")
    for name in ("CSV_DATE_FORMAT", "ALLOW_EMPTY_REPORT", "DUPLICATE_POLICY", "SELECTOR_FILE", "HEADLESS", "REPORT_MODE"):
        monkeypatch.delenv(name, raising=False)
    built = load_settings()
    built.ensure_dirs()
    return built


@pytest.fixture
def db_settings() -> DatabaseSettings:
    return DatabaseSettings(
        host="localhost", port=3306, name="powerbidb", user="test", password="pw-not-used", table=TABLE
    )


def create_table(engine: Engine, *, date_type: str = "DATE", vcpm: bool = True, drop: tuple[str, ...] = ()) -> None:
    columns = [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("Date", date_type),
        ("Portfolio name", "VARCHAR(255)"),
        ("Campaign Name", "VARCHAR(255)"),
        ("Targeting", "VARCHAR(255)"),
        ("Match Type", "VARCHAR(50)"),
        ("Customer Search Term", "VARCHAR(500)"),
        ("Impressions", "INTEGER"),
        ("Clicks", "INTEGER"),
        ("Spend", "DECIMAL(12,2)"),
        ("Sales", "DECIMAL(12,2)"),
        ("Units", "INTEGER"),
    ]
    if vcpm:
        columns.append((VCPM_COLUMN, "DECIMAL(12,2)"))
    ddl = ", ".join(f'"{name}" {kind}' for name, kind in columns if name not in drop)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE "{TABLE}" ({ddl})'))


def seed(engine: Engine, rows: list[tuple]) -> None:
    """rows: (date value, search term, impressions, clicks, spend)."""
    with engine.begin() as connection:
        for day, term, impressions, clicks, spend in rows:
            connection.execute(
                text(
                    f'INSERT INTO "{TABLE}" ("Date", "Campaign Name", "Customer Search Term", '
                    '"Impressions", "Clicks", "Spend", "Sales", "Units") '
                    "VALUES (:d, 'old campaign', :t, :i, :c, :s, 1.00, 1)"
                ),
                {"d": day, "t": term, "i": impressions, "c": clicks, "s": spend},
            )


def rows_by_date(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        result = connection.execute(text(f'SELECT "Date", COUNT(*) FROM "{TABLE}" GROUP BY "Date" ORDER BY 1'))
        return {str(d): n for d, n in result}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_table(eng)
    seed(
        eng,
        [
            ("2026-10-20", "old a", 10, 1, 1.50),
            ("2026-10-20", "old b", 20, 2, 2.50),
            ("2026-10-21", "stale x", 30, 3, 3.50),
            ("2026-10-22", "future y", 40, 4, 4.50),
        ],
    )
    yield eng
    eng.dispose()


@dataclass
class FakeDownload:
    path: Path


REPORT_DATE = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def _reset_logging():
    """CLI tests configure the 'stc' logger; restore propagation for caplog."""
    import logging

    yield
    logger = logging.getLogger("stc")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
