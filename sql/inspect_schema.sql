-- Read-only inspection of the destination table. Safe to run in production:
-- nothing here modifies data or schema.
--   mysql -h HOST -u USER -p powerbidb < sql/inspect_schema.sql
-- (`uv run python run.py --check-db` performs the same checks from Python.)

-- 1. Storage engine: must be InnoDB for the delete+insert to be atomic.
SELECT TABLE_NAME, ENGINE, TABLE_ROWS, TABLE_COLLATION
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'powerbidb' AND TABLE_NAME = 'search term report';

-- 2. Columns and types the importer maps to.
SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA,
       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'powerbidb' AND TABLE_NAME = 'search term report'
ORDER BY ORDINAL_POSITION;

-- 3. Required columns that are MISSING (should return no rows).
SELECT required.name AS missing_column
FROM (
    SELECT 'Date' AS name UNION ALL SELECT 'Portfolio name' UNION ALL SELECT 'Campaign Name'
    UNION ALL SELECT 'Targeting' UNION ALL SELECT 'Match Type' UNION ALL SELECT 'Customer Search Term'
    UNION ALL SELECT 'Impressions' UNION ALL SELECT 'Clicks' UNION ALL SELECT 'Spend'
    UNION ALL SELECT 'Sales' UNION ALL SELECT 'Units'
) AS required
LEFT JOIN INFORMATION_SCHEMA.COLUMNS c
  ON c.TABLE_SCHEMA = 'powerbidb' AND c.TABLE_NAME = 'search term report' AND c.COLUMN_NAME = required.name
WHERE c.COLUMN_NAME IS NULL;

-- 4. Triggers on the table (they could change what a DELETE/INSERT does).
SELECT TRIGGER_NAME, EVENT_MANIPULATION, ACTION_TIMING
FROM INFORMATION_SCHEMA.TRIGGERS
WHERE EVENT_OBJECT_SCHEMA = 'powerbidb' AND EVENT_OBJECT_TABLE = 'search term report';

-- 5. How existing dates are stored (sample) and the per-date row counts.
SELECT `Date`, COUNT(*) AS row_count
FROM powerbidb.`search term report`
GROUP BY `Date`
ORDER BY `Date` DESC
LIMIT 14;

-- 6. Privileges of the current user (needs SELECT, INSERT, DELETE on the table).
SHOW GRANTS FOR CURRENT_USER();
