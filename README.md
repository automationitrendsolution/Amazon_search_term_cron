# Amazon Search Term Report → MySQL

Every hour, this tool downloads the **Search Term report** from Amazon Ads and puts it into the
MySQL table **`search term report`**. Power BI then reads the table.

It works the same way a person would. It opens Amazon Ads in a browser, clicks **Run** on a saved
report, waits for it to finish, and downloads the CSV. It then checks the file and replaces that
day's rows in MySQL. Rows for other days are never touched.

---

## Contents

**For everyone**
1. [At a glance](#1-at-a-glance)
2. [Everyday commands](#2-everyday-commands)
3. [What happens during a run](#3-what-happens-during-a-run)
4. [Which dates get imported](#4-which-dates-get-imported)
5. [Where things are stored](#5-where-things-are-stored)
6. [When something goes wrong](#6-when-something-goes-wrong)

**For whoever sets it up**

7. [First-time setup](#7-first-time-setup)
8. [Running it every hour](#8-running-it-every-hour)
9. [Settings (.env)](#9-settings-env)
10. [Security](#10-security)

**For developers**

11. [Technical reference](#11-technical-reference)
12. [Tests](#12-tests)
13. [What has been verified](#13-what-has-been-verified)

---

## 1. At a glance

| | |
|---|---|
| **What** | Amazon Ads *Search Term* report (Sponsored Products / Display) |
| **Source** | Two saved reports in Amazon Ads: one for *Today*, one for *Yesterday* |
| **Destination** | MySQL table `search term report` (server and database are set in `.env`) |
| **How often** | Every hour (Windows Task Scheduler or Linux cron) |
| **Time per run** | About 5 minutes per report, so about 10 minutes for both |
| **What it changes** | Only the rows of the date being imported. Every other date stays as it is. |
| **If anything is wrong** | It stops **before** writing, so the database keeps its previous data |

---

## 2. Everyday commands

Run these from the project folder. On Windows, open PowerShell in the folder. If the virtual
environment is active (the prompt starts with `(amazon-search-term-report-cron)`), type `python run.py …`.
Otherwise use `uv run python run.py …`. Both do the same thing.

| I want to… | Command | Changes the database? |
|---|---|---|
| Import the reports now | `python run.py` | Yes |
| Test without writing anything | `python run.py --dry-run` | No |
| Watch the browser while it works | `python run.py --dry-run --headed` | No |
| Sign in to Amazon again | `python run.py --login` | No |
| Check the database connection and table | `python run.py --check-db` | No |
| Check the Amazon report page | `python run.py --inspect-report` | No |
| Check a CSV file I already have | `python run.py --validate-csv FILE.csv --expected-date 2026-10-22` | No |

> In PowerShell, always type `python` before `run.py`. Typing `run.py` on its own gives
> *"The term 'run.py' is not recognized"*.

### What a normal run looks like

```
== Yesterday report | 2026-10-21 ===================================
18:00:05        preflight Table currently holds 0 row(s) for 2026-10-21
18:00:16        download  Run triggered ...
18:00:50        download  Status poll 2: pending Processing ...
18:03:40        validate  CSV valid: 184,233 row(s) for 2026-10-21 | Impressions ..., Spend ..., Sales ...
18:04:12        import    Transaction COMMITTED for 2026-10-21: deleted=0 inserted=184233
18:04:14        verify    Verified: row count 184233 matches the CSV
...
== Today report | 2026-10-22 =======================================
...
== Summary =========================================================
  yesterday  2026-10-21   OK       184,233 rows imported, 0 replaced  (250s)
  today      2026-10-22   OK       25,186 rows imported, 15,181 replaced  (420s)
```

Warnings show in **yellow** and errors in **red**. The full detail of every run is also saved in
`logs/app.log`.

---

## 3. What happens during a run

```
 1. Pick dates     Which day(s) to import and which saved report to use
 2. Check MySQL    Is the database reachable and does the table look right?
 3. Amazon         Open the saved report → Actions ▸ Run → wait → Download the CSV
 4. Check the CSV  Right columns? Right date? Valid numbers? (MySQL not touched yet)
 5. Import         Delete that date's rows → insert the new rows → count them
                   → save only if the count matches; otherwise undo everything
 6. Double-check   Re-read the table: row count and totals (Spend, Sales, ...) match the CSV,
                   and other dates are unchanged
 7. File it        Move the CSV to data/processed/
```

If any step fails, the CSV goes to `data/failed/` with a `.error.txt` note explaining why. The
database keeps its previous data.

---

## 4. Which dates get imported

This is set by `REPORT_MODE` in `.env`.

### Two calendars (important)

| | Calendar | Setting |
|---|---|---|
| What Amazon means by **"Today" / "Yesterday"** in a saved report | **India (IST)**, your Amazon user's timezone | `REPORT_PERIOD_TIMEZONE` (defaults to `REPORT_TIMEZONE`) |
| The **date on each CSV row** (and in MySQL) | **US Pacific**, the marketplace | `AMAZON_ACCOUNT_TIMEZONE=America/Los_Angeles` |

So the *Today* report contains the Pacific-time day **with the same date as today in India**. That
day only starts at **12:30 PM India time** (midnight Pacific; 1:30 PM in winter). Before then, the
*Today* report has no data yet. This was observed live on 2026-10-23: at 00:31 IST, Amazon's
"Yesterday" report already returned the previous India date.

### `REPORT_MODE=both` (recommended, currently in use)

Every run imports **yesterday** and **today**, using India's dates:

| Time in India | Reports run |
|---|---|
| 12:00 AM – 12:29 PM | **Yesterday** only. Today's Pacific day hasn't started, so it is skipped without contacting Amazon |
| 12:30 PM – 11:59 PM | **Yesterday**, then **Today** |

Yesterday keeps being refreshed until midnight India time. That picks up the end of the Pacific day
and Amazon's late sales and attribution updates. If one report fails, the other still runs.

### `REPORT_MODE=cutoff` (one report per run)

| Time in India | Report | Date imported |
|---|---|---|
| 12:00 AM – 12:29 PM | Yesterday | yesterday (India date) |
| 12:30 PM – 11:59 PM | Today | today (India date) |

In winter, runs between 12:30 and 1:30 PM India time are **skipped safely**, because the Pacific day
hasn't started yet. The next hourly run picks it up.

Before importing, the app always checks that every row in the CSV has the expected date. A wrong
report can never be imported under the wrong date.

---

## 5. Where things are stored

| Place | What's in it |
|---|---|
| MySQL `search term report` | The imported data (the server and database are set in `.env`) |
| `data/processed/` | CSVs that were imported and verified |
| `data/failed/` | CSVs that were rejected, plus a `.error.txt` note for each |
| `data/downloads/` | CSVs being downloaded (normally empty) |
| `data/screenshots/` | Browser screenshots taken when something goes wrong |
| `data/session/` | The saved Amazon login. **Treat it like a password.** |
| `logs/app.log` | Full log of every run |

### Columns: Amazon CSV → MySQL

| Amazon CSV | MySQL column |
|---|---|
| Date | `Date` |
| Portfolio name | `Portfolio name` |
| Campaign name | `Campaign Name` |
| Targeting | `Targeting` |
| Targeting match type | `Match Type` |
| Search term | `Customer Search Term` |
| Impressions | `Impressions` |
| Clicks | `Clicks` |
| Total cost | `Spend` |
| Sales | `Sales` |
| Purchases | `Units` |
| Cost per 1,000 viewable impressions (VCPM), *if present* | `Cost per 1,000 viewable impressions (VCPM)` |

Good to know:
* **Other CSV columns** (Hour, Ad group name, CPC, CTR…) are ignored. The log lists them.
* **The same search term can appear on several rows.** The report is hourly and split by ad group,
  and neither is imported. All rows are kept, so the totals are correct.
* **Blank search terms are stored as NULL.** Sponsored Display rows target products or categories
  instead of keywords, so they have no search term.
* **Empty number cells are stored as NULL, not 0.**

---

## 6. When something goes wrong

Every run ends with an **exit code**, and the summary shows it. In most cases the database is
**unchanged**, and the next hourly run tries again.

| Code | Meaning | What to do |
|---|---|---|
| **0** | Success, or skipped because the day has not started for Amazon yet | Nothing |
| **2** | A setting in `.env` is wrong | Read the message and fix `.env` |
| **3** | Amazon wants you to sign in again (session expired, CAPTCHA or OTP) | `python run.py --login` |
| **4** | Problem in Amazon Ads (slow, page changed, download failed) | Usually fixes itself next hour; see below |
| **5** | The CSV did not pass the checks | Read the `.error.txt` in `data/failed/` |
| **6** | Database problem (connection, permissions, table) | `python run.py --check-db` |
| **7** | Data was saved, but the double-check found a difference | See below. The data **is** in MySQL |
| **8** | Data saved and verified, but the CSV could not be moved | Move the file named in the log to `data/processed/` |
| **9** | Another run is still in progress | Wait; it will finish |
| **1** | Unexpected error | Look at the end of `logs/app.log` |

### Common messages

| Message | Fix |
|---|---|
| `Access denied for user ...` | Wrong DB user/password in `.env`, or the user lacks permission (§7 step 3) |
| `Destination table ... does not exist` | Create the table (§7 step 3) |
| `sign-in-required` / `security-challenge` | `python run.py --login` |
| `Sorry, something went wrong` | `AMAZON_ENTITY_ID` is wrong, or this login cannot see the report |
| `still 'pending' after ...s` | Amazon is slow. Raise `REPORT_READY_TIMEOUT_SECONDS` (keep it under 50 min) |
| `selector key '...'` | Amazon changed its page. Run `python run.py --inspect-report --headed` (§11) |
| `Run action is disabled` / `cannot tell which` | Someone ran the same report at the same moment. The next hour retries |
| `missing required column(s)` | The saved report lost a column. Add it back in Amazon Ads |
| `report contains date(s) ... but the selected report date is ...` | Today/Yesterday report IDs are swapped, or `AMAZON_ACCOUNT_TIMEZONE` is wrong |
| Summary says `NO DATA` for the **today** report | Normal in the first hour of Amazon's day (just after midnight US Pacific). Nothing changed; the next run picks the data up |
| `no data rows` (yesterday report) | Amazon returned an empty report for a finished day. Set `ALLOW_EMPTY_REPORT=true` only if that's expected |
| `COMMIT OUTCOME UNKNOWN` | Nothing to do. The next run checks and records what happened |

---

## 7. First-time setup

Do this once, on the machine that will run the job.

### Step 1: install

You need Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked                      # installs exact versions into .venv
uv run playwright install chromium    # the browser (~150 MB)
uv run pytest -q -m "not integration" # all tests should pass
```

On Ubuntu, install the browser's system libraries once:
`sudo "$(which uv)" run playwright install-deps chromium`.

### Step 2: settings

Copy `.env.example` to `.env` and fill it in (see [§9](#9-settings-env)). `.env` holds passwords,
so never share or commit it.

### Step 3: MySQL table and user

The app **never creates or changes tables**. On a fresh database, run this once as a MySQL admin
(change the database name, user and password as needed):

```sql
CREATE DATABASE IF NOT EXISTS search_term;

CREATE TABLE search_term.`search term report` (
  `id`                                         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `Date`                                       DATE           NOT NULL,
  `Portfolio name`                             VARCHAR(255)   NULL,
  `Campaign Name`                              VARCHAR(255)   NOT NULL,
  `Targeting`                                  VARCHAR(1024)  NULL,
  `Match Type`                                 VARCHAR(64)    NULL,
  `Customer Search Term`                       VARCHAR(1024)  NULL,
  `Impressions`                                BIGINT         NULL,
  `Clicks`                                     BIGINT         NULL,
  `Spend`                                      DECIMAL(18,4)  NULL,
  `Sales`                                      DECIMAL(18,4)  NULL,
  `Units`                                      BIGINT         NULL,
  `Cost per 1,000 viewable impressions (VCPM)` DECIMAL(18,4)  NULL,
  PRIMARY KEY (`id`),
  KEY `idx_date` (`Date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'search'@'localhost' IDENTIFIED BY 'choose-a-password';
GRANT SELECT, INSERT, DELETE ON search_term.`search term report` TO 'search'@'localhost';
```

The app only needs `SELECT, INSERT, DELETE`. Then check it:

```bash
python run.py --check-db     # should end with "Schema is compatible ... Nothing was changed."
```

### Step 4: the two saved reports in Amazon Ads

In Amazon Ads, go to **Reporting → Create report → Search term** and create two reports:

| Setting | Report 1 | Report 2 |
|---|---|---|
| Name (example) | `search term_today` | `search term_yesterday` |
| Report period | **Today** | **Yesterday** |
| Time unit | Daily or Hourly (the hour is not imported) | same |
| File format | CSV | CSV |
| Columns (at least) | Date, Portfolio name, Campaign name, Targeting, Targeting match type, Search term, Impressions, Clicks, Total cost, Sales, Purchases | same |

Open each report. The address bar shows
`...reporting/history?entityId=ENTITY...&reportId=xxxxxxxx-...`. Copy:
* `reportId` → `REPORT_ID_TODAY` / `REPORT_ID_YESTERDAY`
* `entityId` → `AMAZON_ENTITY_ID`
* each report's exact name → `REPORT_NAME_TODAY` / `REPORT_NAME_YESTERDAY`. This guards against
  swapped IDs.

### Step 5: sign in to Amazon

```bash
python run.py --login
```

**On a PC with a screen (Windows):** a browser window opens. Sign in yourself, including any OTP,
and tick **"Don't require code on this browser"** if offered. The command finishes on its own once
the Reporting page shows.

**On a server without a screen (Ubuntu):** put `AMAZON_EMAIL` and `AMAZON_PASSWORD` in `.env`
first. The browser stays hidden and fills them in. When Amazon sends a code, the terminal asks:

```
Amazon is asking for a two-step verification code.
A text message with a code has been sent to your phone ending in 42
  Verification code (attempt 1/3): 123456
  Code accepted.
```

The tool ticks "Don't require code on this browser" for you. You get 3 tries, then it stops so the
account is not locked. An image CAPTCHA can't be answered from a terminal. If one appears, sign in
once with a visible browser (`ssh -X` / MobaXterm).

Either way, the login is saved in `data/session/`. Sign in **on the machine that will run the job**,
because a session copied from another computer (especially Windows → Linux) does not work.
Scheduled runs never sign in themselves. When the session expires they stop with exit 3, and you
run `--login` again.

### Step 6: test, then go live

```bash
python run.py --inspect-report     # finds the Run menu and Download link? (changes nothing)
python run.py --dry-run --headed   # full download + checks, MySQL untouched
python run.py                      # first real import
```

Check the result in MySQL:

```sql
SELECT `Date`, COUNT(*), SUM(Spend), SUM(Sales)
FROM `search term report` GROUP BY `Date` ORDER BY `Date`;
```

---

## 8. Running it every hour

### Windows (Task Scheduler)

Run once in PowerShell:

```powershell
$dir = "C:\Users\Dell\Desktop\Amazon_search_term_report_cron"
$action  = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" -Argument "run.py" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 55) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName "Amazon Search Term Report" -Action $action -Trigger $trigger -Settings $settings
```

* It runs at 5 minutes past every hour, **only while the PC is on and you are logged in**.
* To test it: Task Scheduler → right-click the task → **Run**, then watch `logs/app.log`.
* To pause it: right-click → **Disable**.

### Linux (cron)

```bash
crontab -e     # paste deploy/crontab.example and adjust the paths (`which uv`)
sudo cp deploy/logrotate.example /etc/logrotate.d/amazon_search_term_report
```

The cron line uses `flock` and `timeout 55m`, so runs never overlap and a stuck run can't block the
next hour. The server's own timezone doesn't matter.

### Updating the code safely

Pause the schedule, then:

```bash
uv sync --locked
uv run pytest -q -m "not integration"
python run.py --dry-run
```

Then resume the schedule. Keep `.env` and `data/` when you replace files.

---

## 9. Settings (.env)

`.env.example` explains every setting. The ones you are most likely to touch:

| Setting | What it does |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | MySQL connection. The password is never written to the log |
| `REPORT_TABLE` | Table name, written plainly: `search term report` |
| `REPORT_MODE` | `both` (yesterday + today every run) or `cutoff` (one report, by the 12:30 rule) |
| `REPORT_TIMEZONE`, `TODAY_REPORT_CUTOFF` | The cutoff rule: `Asia/Kolkata`, `12:30` |
| `AMAZON_ACCOUNT_TIMEZONE` | The marketplace calendar of the CSV dates (US: `America/Los_Angeles`) |
| `REPORT_PERIOD_TIMEZONE` | The calendar Amazon uses for "Today"/"Yesterday" (your user's timezone; default = `REPORT_TIMEZONE`) |
| `REPORT_ID_TODAY`, `REPORT_ID_YESTERDAY` | IDs of the two saved reports |
| `REPORT_NAME_TODAY`, `REPORT_NAME_YESTERDAY` | Report names. A run stops if the page doesn't show this name |
| `AMAZON_ENTITY_ID` | The `entityId` from the report URL |
| `AMAZON_EMAIL`, `AMAZON_PASSWORD` | Only for `--login` on a server without a screen. Scheduled runs never use them |
| `REPORT_READY_TIMEOUT_SECONDS` | How long to wait for Amazon to finish a report (default 20 min) |
| `ALLOW_EMPTY_REPORT` | `false` = an empty report is rejected instead of wiping the day |
| `DUPLICATE_POLICY` | `warn` = keep repeated rows (normal); `fail` = reject the file |
| `HEADLESS` | `true` = browser runs invisibly (normal for scheduled runs) |
| `LOG_LEVEL` | `INFO` normally; `DEBUG` for more detail |

Settings already set as environment variables take priority over `.env`.

**Moving to the production database:** change the `DB_*` settings, then run
`python run.py --check-db` before the next import.

---

## 10. Security

* **`.env`** holds the database password. Don't share or commit it.
* **`data/session/`** is a logged-in Amazon session. Anyone with a copy can use the account.
* **Logs** automatically hide the database password, and also hide sign-in tokens and download
  links that appear in web addresses.
* **Screenshots** can show account data. Delete old ones from time to time.
* **Scheduled runs never sign in.** Only `--login` does, with a person present. On a server it fills
  `AMAZON_EMAIL` / `AMAZON_PASSWORD`, and the person types the two-step code. Codes are never
  generated or guessed, and CAPTCHAs are never answered. The password is hidden in logs.
* **The MySQL user needs only** `SELECT, INSERT, DELETE` on the one table.
* **`.gitignore`** excludes `.env`, `.venv`, `data/*` and `logs/*`.

---

## 11. Technical reference

### Project layout

```
app/
  auth.py           session checks, interactive --login (never answers CAPTCHA/MFA)
  browser.py        Playwright workflow, run identification, stale-download protection
  config.py         .env loading/validation, paths, timezones, DB settings, REPORT_MODE
  dates.py          12:30 IST rule + choosing the Today/Yesterday saved report
  pipeline.py       orchestration (one report per call), recovery, choices_for_now()
  processor.py      CSV decoding, header mapping, validation, typed records
  database.py       schema inspection, compatibility, transactional date-scoped replace
  verifier.py       post-commit verification
  storage.py        run lock, import journal, safe file moves
  logging_setup.py  compact console + rotating file log, secret redaction
  exceptions.py     error classes → exit codes
config/selectors.json   every UI selector (ordered candidates) + row-text patterns
deploy/                 crontab.example, logrotate.example
sql/                    inspect_schema.sql, verify_date.sql (read-only)
tests/                  pytest suite
run.py                  command line entry point
```

### Amazon browser workflow (`browser.py`)

1. Start Chromium with the saved profile `data/session/`. The browser timezone is fixed to
   `AMAZON_ACCOUNT_TIMEZONE`.
2. Open `reporting/history?entityId=…&reportId=…`. Stop on sign-in, CAPTCHA or MFA (exit 3) or on
   "Sorry, something went wrong".
3. Record every visible run row and its *Date created*, note the trigger time, then click
   **Actions ▸ Run**.
4. Reload every `REPORT_POLL_INTERVAL_SECONDS` and look for **this** run: a row that was not there
   before and was created no earlier than the trigger minute (minus `RUN_CLOCK_SKEW_SECONDS`).
5. When that row shows **Download**, download it as
   `data/downloads/<UTC stamp>_<date>_<period>_<report id>_search_term.csv`.

**Stale-download protection.** In these cases the run fails rather than guess:
* no new row has appeared yet. An older row's Download link is never used;
* a new row's date can't be read;
* two new runs appeared;
* the new run has the same minute as an older run.

Browser errors (exit 4) are retried once (`BROWSER_RETRIES`). Sign-in problems and
"cannot tell which run" are not retried. Each failure saves a screenshot.

### Selectors (`config/selectors.json`)

Each key has an ordered list of candidates, and the first visible match wins. If Amazon changes its
page, put a working selector at the top of the affected key. No code change is needed.
`python run.py --inspect-report --headed` shows, for each key, how many elements every candidate
matches. It also shows the parsed history rows and whether the **Run** menu item was found. It opens
the Actions menu but never clicks Run.

### CSV validation (`processor.py`): all before MySQL is touched

* **File:** not empty; not ZIP/XLSX/PDF/HTML; UTF-8, UTF-16 or Windows-1252; every row has the same
  number of fields.
* **Headers:** case, extra spaces and BOM are ignored. All 11 required columns must exist.
* **Dates:** one format must fit every row (`Oct 21, 2026`, `2026-10-21`, `10/21/2026`…), and every
  row must have the expected date.
* **Numbers:** read exactly (no floating point). `1,234` and `$12.50` are accepted. `abc`, `12%` and
  `1e3` are rejected. Impressions, Clicks and Purchases must be whole and non-negative.
* **Text:** kept exactly as Amazon wrote it. Date and Campaign name must not be empty.
* **Empty report:** rejected unless `ALLOW_EMPTY_REPORT=true`.

### Database import (`database.py`)

1. **Inspect** the table. Refuse (exit 6, nothing changed) if:
   * a mapped column is missing or has an incompatible type;
   * an unmapped `NOT NULL` column has no default;
   * the engine isn't InnoDB.
2. **Check every value fits** before writing: text lengths, decimal places, integer ranges and
   NOT NULL.
3. **One transaction:** `DELETE` the date's rows, `INSERT` in batches of 1,000, then count. It
   **COMMITs** only if the count equals the CSV's row count; otherwise it **ROLLs BACK**.
4. **Retries** happen only after a connection error that was rolled back. If COMMIT itself fails,
   the outcome is recorded as unknown and resolved by the next run.

The date filter adapts to the `Date` column type: `DATE`, `DATETIME`/`TIMESTAMP` (whole-day
range) or text (`DB_TEXT_DATE_FORMAT`).

### Verification (`verifier.py`)

After COMMIT, on a fresh connection, it checks that:
* the required columns still exist;
* the row count equals the CSV's;
* the NULL counts and totals of Impressions, Clicks, Units, Spend and Sales equal the CSV's;
* the count and totals of **all other dates** are unchanged.

A failure exits with code 7. The data is already committed.

### Recovery (`storage.py`, `pipeline.py`)

`data/state/import_journal.json` tracks each import through `started → committed → verified →
archived`. If a run was interrupted, the next run first compares the table with the recorded
expectations. It records the outcome before starting anything new. Filenames are unique, a move
never overwrites, and files are never bulk-deleted.

---

## 12. Tests

```bash
uv run pytest -q -m "not integration"   # fast suite (~5 s)
uv run pytest -v                        # also the real-Chromium tests (~45 s)
```

The tests use a temporary SQLite database with the same table and column names, plus a local fake
of the Amazon history page. They never contact Amazon or MySQL.

| File | Covers |
|---|---|
| `test_dates.py` | 12:30 cutoff edges, timezones, Today/Yesterday choice, not-yet-started skip |
| `test_processor.py` | headers, encodings, numbers, NULLs, dates, blank search terms, duplicates |
| `test_database.py` | schema checks, rollback restores deleted rows, other dates preserved, reruns |
| `test_verifier.py` | count/total mismatches, other dates changed |
| `test_pipeline.py` | full run, failures at each stage, dry run, recovery, `REPORT_MODE=both` |
| `test_browser_logic.py` | run identification, stale Download never used, polling, retries |
| `test_browser_integration.py` | real Chromium against a local mock of the report page |
| `test_storage_and_cli.py` | run lock, safe moves, journal, log redaction, exit codes |

On Windows, one timezone test is skipped because it is POSIX-only.

---

## 13. What has been verified

**Live, on 2026-10-22** (Windows PC, local test database `search_term`):
* MySQL connection and schema check (`--check-db`).
* Amazon login, and the selectors on the real Search Term report pages (`--inspect-report`).
* Full runs of **both** saved reports, all verified:
  * Today report: 15,181 rows, then refreshed to 25,186.
  * Yesterday report: 184,233 rows.
* The account's CSV is **hourly**, includes Sponsored Display rows with blank search terms, and has
  no VCPM column. VCPM is written as NULL.

**Not yet verified:**
* The **production** database (`powerbidb`). Run `--check-db` against it before switching.
* Amazon's wording for a *failed* report run (`pattern_status_failed`) has never been seen live.
* Long-term reuse of the login by scheduled headless runs. If Amazon asks for OTP often, set
  `BROWSER_USER_AGENT` to the same browser used for `--login`.
