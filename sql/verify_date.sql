-- Read-only check of one imported date (compare with the CSV / the run log).
-- Set the date, then run:  mysql -h HOST -u USER -p powerbidb < sql/verify_date.sql
SET @d = '2026-10-21';

-- For a DATE column (use the half-open range form below for DATETIME/TIMESTAMP).
SELECT COUNT(*)            AS row_count,
       SUM(`Impressions`)  AS impressions,
       SUM(`Clicks`)       AS clicks,
       SUM(`Spend`)        AS spend,
       SUM(`Sales`)        AS sales,
       SUM(`Units`)        AS units
FROM powerbidb.`search term report`
WHERE `Date` = @d;

-- DATETIME/TIMESTAMP variant:
-- WHERE `Date` >= @d AND `Date` < @d + INTERVAL 1 DAY;

-- Least-privilege grant for the loader account (run once by a DBA; shown for reference):
-- CREATE USER 'search_term_loader'@'%' IDENTIFIED BY '...';
-- GRANT SELECT, INSERT, DELETE ON powerbidb.`search term report` TO 'search_term_loader'@'%';
