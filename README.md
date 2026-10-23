# Amazon_search_term_report_cron

Hourly automation that runs the **Amazon Ads Search Term report**, downloads the
CSV for the correct day, validates it, and replaces that day's rows in the
existing MySQL table **`powerbidb`.`search term report`**. Rows for every other
date are left untouched.

This is a standalone project. It does not import, modify or share state with
`amazon-order-report-automation` or `Amazon_ad_campaign_report_cron`.

> **Status:** the browser workflow has not yet been run against your real Amazon
> Ads account, and the import has not yet been run against the production MySQL
> table. See [Verified vs. unverified](#verified-vs-unverified) before relying on it.

---

## Contents

1. [How it works](#how-it-works)
2. [Folder structure](#folder-structure)
3. [Prerequisites](#prerequisites)
4. [Install with uv](#install-with-uv)
5. [Configuration (.env)](#configuration-env)
6. [Amazon Ads setup and login](#amazon-ads-setup-and-login)
7. [Selectors and how to verify them](#selectors-and-how-to-verify-them)
8. [Browser workflow](#browser-workflow)
9. [Date cutoff rule](#date-cutoff-rule)
10. [CSV mapping and validation](#csv-mapping-and-validation)
11. [Database import (transaction)](#database-import-transaction)
12. [Post-import verification](#post-import-verification)
13. [Files, state and recovery](#files-state-and-recovery)
14. [CLI usage and exit codes](#cli-usage-and-exit-codes)
15. [Tests](#tests)
16. [Ubuntu deployment with cron](#ubuntu-deployment-with-cron)
17. [Troubleshooting](#troubleshooting)
18. [Security and credentials](#security-and-credentials)
19. [Verified vs. unverified](#verified-vs-unverified)

---

## How it works

```
cron (hourly, flock) ─► run.py
   1 select    12:30 Asia/Kolkata rule → report date; Today/Yesterday saved report for that date
   2 preflight MySQL reachable? schema compatible? InnoDB? finish any interrupted earlier import
   3 download  Playwright: open report history → Actions ▸ Run → reload until THIS run shows Download → save CSV
   4 validate  headers, dates (= selected date), numbers, duplicates — nothing touches MySQL until this passes
   5 import    one transaction: DELETE rows of that date → INSERT CSV rows → re-count → COMMIT (or ROLLBACK)
   6 verify    fresh connection: row count, NULL counts, metric totals, other dates unchanged
   7 archive   move the CSV to data/processed/ (only after 5 and 6 succeed)
```

On any failure the CSV goes to `data/failed/`, with a `.error.txt` file next to it
explaining why, and the process exits with a specific code
(see [exit codes](#cli-usage-and-exit-codes)).

## Folder structure

```
Amazon_search_term_report_cron/
├── app/
│   ├── __init__.py
│   ├── auth.py            session checks, interactive --login (never answers CAPTCHA/MFA)
│   ├── browser.py         Playwright workflow, run identification, stale-download protection
│   ├── config.py          .env loading/validation, paths, timezones, DB settings
│   ├── database.py        schema inspection, compatibility, transactional date-scoped replace
│   ├── processor.py       CSV decoding, header mapping, validation, typed records
│   ├── verifier.py        post-commit verification
│   ├── dates.py           12:30 IST rule + choosing the Today/Yesterday saved report
│   ├── pipeline.py        orchestration and recovery
│   ├── storage.py         run lock, import journal, safe file moves
│   ├── logging_setup.py   console + rotating file logs, context fields, secret redaction
│   └── exceptions.py      error classes → exit codes
├── config/selectors.json  every UI selector (ordered candidates) + row-text patterns
├── data/
│   ├── downloads/         new downloads (unique names)
│   ├── processed/         imported AND verified files
│   ├── failed/            files from failed runs + .error.txt notes
│   ├── screenshots/       failure screenshots
│   ├── session/           persistent Chromium profile = Amazon session (a credential, chmod 700)
│   └── state/             import_journal.json, last_run.json, run.lock (no secrets)
├── deploy/                crontab.example, logrotate.example
├── logs/                  app.log (rotating), cron.log
├── sql/                   read-only inspection/verification SQL
├── tests/                 pytest suite
├── .env.example           template; copy to .env
├── pyproject.toml, uv.lock
└── run.py                 CLI
```

The spec listed seven modules under `app/`. Five small helper modules were added
(`dates`, `pipeline`, `storage`, `logging_setup`, `exceptions`) so that each
listed module keeps a single job and can be tested on its own.

## Prerequisites

* Ubuntu 22.04/24.04 (or any Linux with glibc), or Windows/macOS for development
* Python 3.12 or 3.13 (`requires-python = ">=3.12,<3.14"`); uv installs it if missing
* [uv](https://docs.astral.sh/uv/) ≥ 0.5
* Network access to `advertising.amazon.com` and to the MySQL server
* A MySQL account with `SELECT, INSERT, DELETE` on `powerbidb`.`search term report`
  (see `sql/verify_date.sql`). No DDL privileges are needed.

## Install with uv

```bash
# uv itself (as the runtime user)
curl -LsSf https://astral.sh/uv/install.sh | sh

cd /opt/Amazon_search_term_report_cron
uv sync --locked                          # exact versions from uv.lock (.venv is created)
uv run playwright install chromium        # browser binary (per user, ~150 MB)
sudo "$(which uv)" run playwright install-deps chromium   # Ubuntu system libraries (once)
uv run pytest -v                          # all tests should pass
```

How the project was created (for reference; do not re-run):

```bash
uv init Amazon_search_term_report_cron --app --python 3.12
uv add playwright pandas sqlalchemy mysql-connector-python python-dotenv
uv add --dev pytest
```

To change dependencies, use `uv add <pkg>` / `uv remove <pkg>` and commit
`pyproject.toml` **and** `uv.lock` together. Do not use `pip install`.

## Configuration (.env)

```bash
cp .env.example .env
chmod 600 .env
```

`.env.example` documents every variable. The main ones:

| Variable | Purpose |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | MySQL connection (password is never logged) |
| `REPORT_TABLE` | `search term report` (no quotes; quoting is handled by SQLAlchemy) |
| `DB_TEXT_DATE_FORMAT` | only used if `Date` is a VARCHAR column; must match existing rows |
| `REPORT_TIMEZONE`, `TODAY_REPORT_CUTOFF` | `Asia/Kolkata`, `12:30` (the business rule) |
| `AMAZON_ACCOUNT_TIMEZONE` | the advertiser account's timezone (US accounts: `America/Los_Angeles`) |
| `REPORT_ID_TODAY`, `REPORT_ID_YESTERDAY` | IDs of the two saved Search Term reports |
| `AMAZON_ENTITY_ID` | `entityId` from the report URL (required by the console) |
| `REPORT_POLL_INTERVAL_SECONDS`, `REPORT_READY_TIMEOUT_SECONDS` | polling cadence and limit |
| `ALLOW_EMPTY_REPORT` | `false`: a header-only CSV is rejected instead of wiping the day |
| `DUPLICATE_POLICY` | `warn` (keep and log) or `fail` |

Variables already exported in the environment take precedence over `.env`.

## Amazon Ads setup and login

### 1. Create two saved Search Term reports (one time, in the Amazon Ads UI)

Under **Reporting → Sponsored ads reports → Create report**:

| Setting | Report A | Report B |
|---|---|---|
| Report category / type | Sponsored Products → **Search term** | same |
| Time unit | **Daily** (the CSV must have a `Date` column) | same |
| Report period | **Today** | **Yesterday** |
| File format | **CSV** | CSV |
| Columns | at least: Date, Portfolio name, Campaign name, Targeting, Targeting match type, Search term, Impressions, Clicks, Total cost, Sales, Purchases (optionally VCPM) | same |

Open each report's history page. Its URL has the form
`https://advertising.amazon.com/reporting/history?entityId=ENTITY…&reportId=xxxxxxxx-…`.
Copy `reportId` into `REPORT_ID_TODAY` / `REPORT_ID_YESTERDAY`, and copy `entityId`
into `AMAZON_ENTITY_ID`.

> **Why two saved reports and not a date picker?** Re-running a saved report from
> its history page has been observed to work on this console (Actions ▸ Run). Editing
> a report's date range through the UI has not been verified, and the automation
> does not attempt it. With a relative period ("Today"/"Yesterday") the same
> definition stays valid every day. The CSV's `Date` column is still checked
> against the selected date, so a wrong report can never be imported.

### 2. Interactive login (creates the persistent session)

```bash
uv run python run.py --login
```

A visible Chromium window opens on `https://advertising.amazon.com/reporting`.
Sign in normally and complete any CAPTCHA, OTP or two-step verification yourself.
If Amazon offers "Don't require code on this browser", tick it. The command
finishes on its own once the reporting page is showing. The session is stored in
`data/session/`. The automation **never** fills in passwords and never solves
CAPTCHA or MFA challenges.

On a headless Ubuntu server, choose one of:

* `ssh -X user@server` (X11 forwarding), then run `--login`;
* `xvfb-run` is **not** enough on its own, because you have to see and use the window.
  Use a VNC/remote desktop session instead;
* sign in on a desktop machine with the same project, then copy `data/session/` to
  the server (`rsync -a --chmod=D700,F600`). Use the same OS family and
  Chromium version if you can.

To keep Amazon from treating the headless scheduled runs as a different browser,
set `BROWSER_USER_AGENT` to the user agent string of the headed browser
(see `.env.example`).

When the session expires, scheduled runs exit with code **3** and save a
screenshot. Run `--login` again to fix it.

## Selectors and how to verify them

All locators live in `config/selectors.json` as **ordered candidate lists**. The
first visible match wins. If Amazon changes its UI, put a working selector at the
top of the affected key; no code change is needed. Keys:

| Key | Used for | Status |
|---|---|---|
| `sign_in_marker`, `security_challenge_marker` | detect expired session / CAPTCHA / MFA | verified on live Amazon pages (sibling project) |
| `report_history_loaded_marker` (`role=grid`) | history page rendered | verified-elsewhere |
| `report_actions_menu`, `run_report_menu_item` | Actions ▸ **Run** | verified-elsewhere |
| `history_row` (`.ag-center-cols-container .ag-row`) | run rows | verified-elsewhere |
| `history_row_download` (`a:has-text('Download')`) | Download link inside a row | verified-elsewhere |
| `pattern_created_at` | parses "Oct 20, 2026 2:00 AM PDT" from a row | verified-elsewhere format |
| `pattern_status_failed` | failed-run wording | **unverified** |
| `reporting_loaded_marker`, `run_report_button`, `run_report_confirm_button`, `report_not_found_marker` | fallbacks | **unverified** |

"Verified-elsewhere" means the selector was confirmed on 2026‑10‑20 on the live
Amazon Ads report-history page for a **Campaign** report, by the sibling
`Amazon_ad_campaign_report_cron` project. The Search Term report uses the same
reporting console, but this has **not** been confirmed for your Search Term reports.

**Verify them** (read-only: it opens and closes the Actions menu but never clicks Run):

```bash
uv run python run.py --inspect-report --headed
```

This prints, for every key, how many elements each candidate matches. It also
prints the parsed history rows (status, Date created, whether a Download link
exists) and saves a screenshot. Check that:

1. `history_row` matches the visible runs;
2. every row shows `created` and `created_utc` (otherwise fix `pattern_created_at`);
3. `run_menu_item_found` is `true`.

Then do one `--dry-run` (below) before enabling cron.

## Browser workflow

`app/browser.py → retrieve_report()`:

1. Launch Chromium with the persistent profile `data/session/`, with the browser
   timezone pinned to `AMAZON_ACCOUNT_TIMEZONE`.
2. Open `reporting/history?entityId=…&reportId=…` for the chosen saved report.
3. Stop if Amazon shows sign-in, CAPTCHA or MFA (exit 3), or the error panel
   "Sorry, something went wrong". Wait for the grid.
4. **Snapshot** every visible run row and its *Date created*, record the trigger
   time (UTC), then click **Actions ▸ Run**. The run fails if Run is missing or disabled.
5. Write `data/state/last_run.json`: report ID, period, report date, trigger time
   and the pre-trigger rows.
6. Reload every `REPORT_POLL_INTERVAL_SECONDS`. On each reload, look for **this
   execution's run**: a row whose *Date created* was **not** in the snapshot and is
   no earlier than the trigger minute minus `RUN_CLOCK_SKEW_SECONDS`.
7. When that row (and only that row) shows a Download link, find it again by its
   *Date created*, click the link, and save the file as
   `data/downloads/<UTC stamp>_<date>_<period>_<report id>_search_term.csv`.
8. Check the file is non-empty, readable and not an HTML page, then pass it on.

**Stale-download protection — the run fails rather than guessing when:**

* nothing new has appeared yet. An older row with a Download link on top of the
  grid is never used;
* a new row's *Date created* cannot be parsed, or uses an unknown timezone
  abbreviation;
* two or more new runs have appeared since the trigger;
* the new run has the same *Date created* minute as an older run.

Timeouts and errors: navigation timeouts, missing selectors, download timeouts
and browser crashes raise `BrowserError` (exit 4). These are retried once
(`BROWSER_RETRIES`). Authentication problems, stale-run ambiguity and
report-not-ready timeouts are not retried. Each failure saves a screenshot to
`data/screenshots/`. URLs are logged without their query strings, and page
HTML and cookies are never saved.

## Date cutoff rule

> **`REPORT_MODE=both`** skips the cutoff rule: every run imports the
> *Yesterday* report for the account's yesterday, then the *Today* report for the
> account's today (dates in `AMAZON_ACCOUNT_TIMEZONE`). Each report is its own
> date-scoped transaction; if one fails, the other still runs and the exit code is
> the first failure's. The default, `REPORT_MODE=cutoff`, is described below.

Implemented in `app/dates.py → select_report_date()`. The rule is always evaluated
in **Asia/Kolkata**, using timezone-aware datetimes and `zoneinfo`, whatever the
server's timezone is:

| Time in IST | Report date |
|---|---|
| 00:00:00 – 12:29:59 | yesterday |
| 12:30:00 exactly | today |
| 12:30:01 – 23:59:59 | today |

The function accepts an injectable `now` or `clock`, and rejects naive datetimes.

**Account timezone.** Amazon evaluates a saved report's "Today"/"Yesterday" in the
*advertiser account's* timezone. `choose_report()` therefore compares the selected
date with the account's current date to pick the right saved report. For a US
account (Pacific time):

* 10:00 IST on Oct 22 is 21:30 PDT on Oct 21. The selected date is Oct 21,
  which is the account's **Today** report.
* 13:00 IST on Oct 22 is 00:30 PDT on Oct 22. The selected date is Oct 22,
  again the **Today** report.
* 12:30–13:29 IST in winter (PST): the selected date (IST today) has **not started
  yet** in Pacific time. The run logs "Skipping" and exits 0 without changing
  anything. The next hourly run picks it up.

For an India-based account (`AMAZON_ACCOUNT_TIMEZONE=Asia/Kolkata`) it is simply
before 12:30 → Yesterday report, from 12:30 → Today report.

## CSV mapping and validation

| Amazon column | MySQL column |
|---|---|
| Date | Date |
| Portfolio name | Portfolio name |
| Campaign name | Campaign Name |
| Targeting | Targeting |
| Targeting match type | Match Type |
| Search term | Customer Search Term |
| Impressions | Impressions |
| Clicks | Clicks |
| Total cost | Spend |
| Sales | Sales |
| Purchases | Units |
| *(Cost per 1,000 viewable impressions (VCPM), if present)* | Cost per 1,000 viewable impressions (VCPM) |

If the table has a VCPM column and the CSV does not, VCPM is written as **NULL**.
Nothing is ever made up.

Validation (`app/processor.py`), **all before any database connection**:

* **File level:** non-empty; not ZIP/XLSX/PDF/HTML; UTF‑8 (with or without BOM),
  UTF‑16 with BOM, or a logged Windows‑1252 fallback; every row has the header's
  field count.
* **Headers:** BOM, non-breaking spaces, extra whitespace and case are normalised.
  Only those are normalised, so `Purchases` and `Purchases (multi-touch)` stay
  distinct. All 11 required columns must be present. Two headers that normalise
  to the same name are an error. Unmapped columns are **named in the log** and ignored.
* **Dates:** one format must parse every row (`Oct 21, 2026` as seen in live exports,
  `2026-10-21`, `10/21/2026`…; override with `CSV_DATE_FORMAT`). Every row must equal
  the selected report date.
* **Numbers:** parsed exactly (`Decimal`, no floats). Thousands separators and a
  leading currency symbol are accepted. `abc`, `1.2.3`, `12%` and `1e3` are
  rejected. Impressions, Clicks and Purchases must be non-negative whole numbers.
  An empty cell or `-` becomes **NULL**, not 0.
* **Text:** kept verbatim (no trimming or case changes). Search terms such as
  `null` or `NA` are kept as text because pandas NA inference is off. A blank
  Portfolio name becomes NULL. Date and Campaign name must not be empty. A blank Search term (Sponsored Display rows
  target ASINs/categories, not keywords) is kept and stored as NULL.
* **Duplicates:** exact duplicates and repeated
  (date, campaign, targeting, match type, search term) keys are counted and logged.
  Amazon emits one row per ad group, and ad group is not mapped. Rows are **never
  deduplicated**. `DUPLICATE_POLICY=fail` rejects such files instead.
* **Header-only file:** rejected unless `ALLOW_EMPTY_REPORT=true`, so a
  partial or empty report cannot wipe a day.

## Database import (transaction)

`app/database.py`. The production table is never altered: no
CREATE/ALTER/DROP/TRUNCATE/RENAME, and a test enforces this.

1. **Inspect** with SQLAlchemy reflection and `INFORMATION_SCHEMA.TABLES.ENGINE`.
2. **Refuse** to continue (exit 6, nothing changed) if:
   * a mapped column is missing (names match exactly);
   * a column type is incompatible (e.g. a numeric `Customer Search Term`, or an
     integer `Spend`);
   * an unmapped `NOT NULL` column has no default;
   * the engine is not InnoDB;
   * a text `Date` column holds values that are not in `DB_TEXT_DATE_FORMAT`.
3. **Fit check per value**, still before writing: VARCHAR lengths, DECIMAL
   scale/precision (rounding away digits is refused), integer ranges, and NOT NULL.
4. **One connection, one transaction:**
   * fingerprint the rows outside the date (count plus metric sums);
   * `DELETE … WHERE <date predicate>`;
   * `INSERT` the rows in batches of 1,000;
   * `SELECT COUNT(*)` for the date inside the transaction.
5. **COMMIT** only if that count equals the CSV row count. Otherwise **ROLLBACK**.

Date predicate by column type:

| Column type | Predicate |
|---|---|
| `DATE` | `` `Date` = :d `` |
| `DATETIME` / `TIMESTAMP` | `` `Date` >= :d 00:00:00 AND `Date` < :d+1 00:00:00 `` (half-open; for TIMESTAMP, `DB_SESSION_TIME_ZONE` sets the session zone) |
| `VARCHAR` / `CHAR` | `` `Date` = :d rendered with DB_TEXT_DATE_FORMAT `` |

The table name and column names contain spaces, commas and parentheses. Every
statement is built from the reflected `Table`, so the MySQL dialect quotes the
identifiers. Bind parameters use synthetic names (`p0`…`pN`).

The log records the selected date, rows deleted and inserted, and whether the
transaction was COMMITTED or ROLLED BACK.

**Retries.** The transaction is retried (`DB_MAX_ATTEMPTS`) only after a
connection error **that was rolled back**. Because the operation is a date-scoped
replace, running it again gives the same result. If COMMIT itself fails, the
outcome is unknown. In that case the import is **not** retried; see recovery below.

## Post-import verification

`app/verifier.py` runs on a new connection after COMMIT and checks:

* the required columns still exist;
* the number of rows for the date equals the CSV row count. When the CSV has rows,
  the table must too;
* Impressions, Clicks, Units, Spend and Sales are valid numbers (integers where
  required), and their NULL counts and **totals equal the CSV's**;
* the count and metric sums of **all other dates are unchanged**. If they changed,
  another process may be writing to the table at the same time.

If verification fails, the run exits with code **7** and the log says
**"THE TRANSACTION IS ALREADY COMMITTED"**. The CSV is kept in `data/failed/` for
investigation.

## Files, state and recovery

* Filenames are unique (UTC timestamp, date, period, report ID). A move never
  overwrites an existing file; `-1`, `-2`… is appended. Files are never bulk-deleted.
* `data/processed/` only receives files whose import committed **and** passed
  verification. If the archive move itself fails, the run exits with code **8**:
  the data is fine and the file is left where it was.
* `data/state/import_journal.json` records each import's progress through
  `started → committed → verified → archived`, along with the expected counts and
  totals (no secrets).
* **Recovery:** if a previous run stopped in `started`, `committed`,
  `commit-unknown` or `verify-failed`, the next run first compares the table with
  the recorded expectations. It then marks that import as committed (and archives
  the file) or as not applied (and moves the file to `failed/`). Only after that
  does it start a new destructive operation.
* `data/session/` (chmod 700) is the Amazon login and must be treated like a
  password. `data/state/` holds no cookies or credentials.

## CLI usage and exit codes

```bash
uv run python run.py                     # full run (what cron executes)
uv run python run.py --dry-run           # Amazon download + CSV validation; MySQL untouched
uv run python run.py --login             # interactive sign-in (visible browser)
uv run python run.py --validate-csv FILE [--expected-date YYYY-MM-DD]   # local file, MySQL untouched
uv run python run.py --check-db          # read-only: connection, schema, engine, date format
uv run python run.py --inspect-report    # read-only selector check on the live page
uv run python run.py --dry-run --headed  # watch the browser
```

| Exit | Meaning | Database state |
|---|---|---|
| 0 | success, or skipped (date not started for the account) | updated / unchanged |
| 1 | unexpected error (traceback in `logs/app.log`) | see log |
| 2 | configuration error | unchanged |
| 3 | Amazon sign-in needed (expired session, CAPTCHA, MFA) | unchanged |
| 4 | browser or report retrieval failed (timeout, selector, download, stale-run ambiguity) | unchanged |
| 5 | CSV validation failed | unchanged |
| 6 | database or schema error | rolled back / unchanged, **unless** the log says `COMMIT OUTCOME UNKNOWN` |
| 7 | post-commit verification failed | **committed** |
| 8 | archive move failed after a verified import | committed and verified |
| 9 | another run holds the lock | unchanged |

## Tests

```bash
uv run pytest -v                 # everything (~45 s, including real-Chromium tests)
uv run pytest -v -m "not integration"   # skip the Chromium tests (~5 s)
```

| File | Covers |
|---|---|
| `test_dates.py` | 12:29:59 / 12:30:00 / 12:30:01 / midnight IST, other-timezone datetimes, child processes with `TZ` set to LA, UTC, Kiritimati and EST5EDT, naive datetimes rejected, Today/Yesterday choice in the account timezone, not-yet-started skip |
| `test_processor.py` | header mapping, BOM/whitespace/case, distinct columns kept distinct, missing and duplicate headers, empty/binary/HTML/header-only files, invalid numbers, NULL not 0, malformed/mixed/mismatched dates, verbatim search terms, duplicates, cp1252 and UTF‑16 |
| `test_database.py` | schema compatibility, missing table/column, wrong types, non-InnoDB refused, value fit, **rollback after a failed insert restores the deleted rows**, count-mismatch rollback, COMMIT-unknown, **other dates preserved**, idempotent reruns, DATETIME half-open range, text dates, no DDL in the code, password never logged |
| `test_verifier.py` | success, count mismatch, changed values, invalid stored numbers, other dates changed, no rows |
| `test_pipeline.py` | full run → processed only after verify, wrong-date CSV, DB failure, verification failure, archive failure, dry run leaves MySQL untouched, skip, recovery both ways, recovery before import, retry policy |
| `test_browser_logic.py` | Date-created parsing, **stale Download never used**, old run finishing ≠ ours, clock skew, two new runs / same-minute collision / unreadable row fail closed, polling success/timeout/failed, missing download control, download timeout, retry policy |
| `test_browser_integration.py` | **real Playwright + Chromium** against a local mock of the history page: Run → Processing → Completed → only the new run downloaded; no new run → timeout, nothing downloaded; sign-in page → exit 3 with a screenshot |
| `test_storage_and_cli.py` | lock contention (exit 9), no-overwrite moves, failure notes, journal, log redaction, CLI exit codes |

Tests use a temporary SQLite database with the same table and column names
(spaces, commas, parentheses), plus a local HTTP mock of Amazon. They never
contact Amazon or MySQL. `time.tzset` exists only on POSIX, so on Windows one
in-process timezone test is skipped; the subprocess variant still runs.

## Ubuntu deployment with cron

```bash
# 1. runtime user and code
sudo adduser --disabled-password --gecos "" amzreports
sudo mkdir -p /opt/Amazon_search_term_report_cron
sudo chown amzreports: /opt/Amazon_search_term_report_cron
sudo -iu amzreports
git clone <repo> /opt/Amazon_search_term_report_cron     # or unzip the archive
cd /opt/Amazon_search_term_report_cron

# 2. uv, dependencies, browser
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
uv sync --locked
uv run playwright install chromium
exit; sudo /home/amzreports/.local/bin/uv --directory /opt/Amazon_search_term_report_cron run playwright install-deps chromium; sudo -iu amzreports

# 3. configuration and permissions
cd /opt/Amazon_search_term_report_cron
cp .env.example .env && chmod 600 .env && nano .env
chmod 700 data/session data/state
uv run pytest -q -m "not integration"

# 4. database check (read-only)
uv run python run.py --check-db

# 5. Amazon login (needs a display: ssh -X / VNC, see above), then verify selectors
uv run python run.py --login
uv run python run.py --inspect-report

# 6. full dry run: runs and downloads the real report, validates, writes nothing to MySQL
uv run python run.py --dry-run; echo "exit=$?"

# 7. first real run by hand, then check the date in MySQL (sql/verify_date.sql)
uv run python run.py; echo "exit=$?"

# 8. schedule
crontab -e        # paste deploy/crontab.example (adjust paths: `which uv`)
sudo cp deploy/logrotate.example /etc/logrotate.d/amazon_search_term_report
```

The cron line runs from the project directory as `amzreports`, so it can reach
`.env`, `data/session` and the uv-managed `.venv`. It uses `flock -n` so runs never
overlap (the app also holds `data/state/run.lock`), and `timeout 55m` so a hung
run cannot block the next hour. The server's timezone does not affect which date
is selected.

**Logs:** `logs/app.log` (rotating, 5 MB × 10) and `logs/cron.log` (rotated by
logrotate). Each line carries the workflow stage, selected date, file and row count:

```
2026-10-22 09:05:41 INFO    [import] date=2026-10-21 file=…_search_term.csv rows=1834 stc.database: Transaction COMMITTED for 2026-10-21: deleted=1790 inserted=1834
```

**Updating safely:**

```bash
cd /opt/Amazon_search_term_report_cron
crontab -l > /tmp/cron.bak && crontab -r   # optional: pause
git pull                                    # or unpack the new archive (keep .env, data/)
uv sync --locked
uv run pytest -q -m "not integration"
uv run python run.py --dry-run
crontab /tmp/cron.bak                       # resume
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| exit 3, `sign-in-required` / `security-challenge` screenshot | `uv run python run.py --login`; set `BROWSER_USER_AGENT` if MFA comes back every run |
| exit 4, "Sorry, something went wrong" | `AMAZON_ENTITY_ID` is missing or wrong, or this login cannot see the report |
| exit 4, "selector key '…'" | `--inspect-report --headed`; move a working selector to the top of that key in `config/selectors.json` |
| exit 4, "still 'pending' after …s" | Amazon is slow: raise `REPORT_READY_TIMEOUT_SECONDS` (keep it under ~50 min) |
| exit 4, "Failing closed" / "cannot tell which" | someone else started a run at the same moment; the next hourly run retries. Check `pattern_created_at` if the row text was "could not be read" |
| exit 4, "Run action is disabled" | a run is already in progress on Amazon's side; the next hour retries |
| exit 5, "missing required column(s)" | the saved report's columns differ; add the columns in Amazon Ads (Time unit **Daily**) |
| exit 5, "report contains date(s) … but the selected report date is …" | Today/Yesterday report IDs swapped, or `AMAZON_ACCOUNT_TIMEZONE` is wrong |
| exit 5, "no data rows" | Amazon returned an empty day; set `ALLOW_EMPTY_REPORT=true` only if that is legitimate |
| exit 6, schema message | fix the table manually (the app never alters it) or the mapping; `sql/inspect_schema.sql` |
| exit 6, `COMMIT OUTCOME UNKNOWN` | nothing to do by hand; the next run checks and records what happened. Inspect with `sql/verify_date.sql` |
| exit 7 | data is committed. Compare `sql/verify_date.sql` with the CSV in `data/failed/`. Look for other writers to the table |
| exit 8 | move the file named in the log from `data/downloads/` to `data/processed/` |
| exit 9 | a run is still going (`ps aux | grep run.py`) |
| Chromium fails to start | `uv run playwright install chromium`, `sudo … playwright install-deps chromium`; in containers `BROWSER_NO_SANDBOX=true` |

To validate a file by hand: `uv run python run.py --validate-csv data/failed/<file>.csv --expected-date YYYY-MM-DD`.

## Security and credentials

* `.env` (chmod 600) holds the DB password. The app warns if `.env` is readable by
  other users. Logs pass through a redaction filter that removes the DB password,
  URL credentials and every URL query string (Amazon SSO tokens, signed download
  links). SQL errors are logged without parameter values (`hide_parameters`).
* `data/session/` is the Amazon login (cookies). It is chmod 700, git-ignored, and
  never read or logged by the app. Anyone with a copy can act as the account.
* No passwords are typed by the automation, and CAPTCHA, OTP and MFA are never bypassed.
* Screenshots can show account data. They are git-ignored; prune them periodically.
* `.gitignore` excludes `.venv`, `.env`, `data/*` (except `.gitkeep`) and `logs/*`.
* The MySQL account needs only `SELECT, INSERT, DELETE` on the one table.

## Verified vs. unverified

**Tested here (automated, passing):** everything in [Tests](#tests). That includes
the date rule, CSV validation, the transactional replace and rollback on a SQL
database with the production identifiers, verification, recovery, file handling,
locking and the CLI. It also includes the real Playwright/Chromium workflow
against a **local mock** of the history page.

**Not yet verified — needs your environment:**

1. **Amazon Ads UI for the Search Term report.** The selectors come from the live
   Campaign-report history page (verified 2026‑10‑20 in the sibling project).
   Confirm them with `--inspect-report` and one `--dry-run`. Wording for failed
   runs (`pattern_status_failed`) has never been observed live.
2. **That saved Search Term reports offer "Today"/"Yesterday" periods** and contain
   the 11 columns with these exact headers (and whether VCPM is available).
   `--dry-run` shows the headers and any ignored columns.
3. **The production table schema.** Run `--check-db` (read-only). Check that
   column names match the mapping exactly (e.g. `Campaign Name` vs `Campaign name`),
   that the types are compatible, that the engine is InnoDB, and how `Date` is
   stored. SQLite stood in for MySQL in the tests, so MySQL-specific behaviour
   (INFORMATION_SCHEMA engine query, mysql-connector binding of the spaced
   identifiers, TIMESTAMP session zone) has not been exercised.
4. **`AMAZON_ACCOUNT_TIMEZONE`.** Row times showing `PDT` on the live page suggest a
   US (Pacific) account. Confirm in Amazon Ads account settings.
5. **Headless session reuse** on the Ubuntu server (MFA prompts) depends on Amazon's
   risk checks. Keep `BROWSER_USER_AGENT` the same as the browser used for `--login`.
#   A m a z o n _ s e a r c h _ t e r m _ c r o n  
 