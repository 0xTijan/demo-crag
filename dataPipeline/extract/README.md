# Markdown reports → SQL (`dataPipeline/extract`)

`python -m dataPipeline.extract.run` reads the Markdown files written by `dataPipeline/convert`, finds the financial statements in them, and writes one row per report to `reports`, `financials` and `valuation_ratios` (`schema.sql`).

- [1. Running it](#1-running-it)
- [2. What it does, in one picture](#2-what-it-does-in-one-picture)
- [3. Files](#3-files)
- [4. Step by step](#4-step-by-step)
- [5. Where each column comes from](#5-where-each-column-comes-from)
- [6. Checks and `needs_review`](#6-checks-and-needs_review)
- [7. Database](#7-database)
- [8. Reruns, duplicates and `--force`](#8-reruns-duplicates-and---force)
- [9. Decisions and known limits](#9-decisions-and-known-limits)
- [10. Tests](#10-tests)

---

## 1. Running it

From the repository root:

```bash
python -m dataPipeline.extract.run                      # every new or changed report
python -m dataPipeline.extract.run --isin SI0031104332  # only these companies
python -m dataPipeline.extract.run --file markdowns/petrol/reports/letno_2024.md
python -m dataPipeline.extract.run --dry-run            # extract and check, print, write nothing
python -m dataPipeline.extract.run --force              # also files already done; overwrite periods
python -m dataPipeline.extract.run --limit 5            # first 5 files only
```

Needs:
- `DATABASE_URL` (`.env`), the same variable as the other steps.
- Ollama running with the model from `config.json` → `extractionModel` (default `qwen2.5:14b`, `temperature 0`, `num_ctx 16384`). The model is only contacted when a file actually needs extracting.
- Python packages: `sqlalchemy`, `psycopg2`, `python-dotenv`, `ollama` (all in `requirements.txt`).

Everything printed is also saved to `logs/extract_<time>.log`. The run ends with

```
Done: 3 extracted, 1 needs review, 4 skipped, 12 unchanged, 0 failed
```

and exits with status 1 if any file failed.

Without `--file`, only files the download step filed as reports (`downloaded_files.category = 'reports'`) are read. A file named with `--file` is read whatever its category, so a report the downloader mistook for an announcement can still be extracted. A Markdown file with no `report_files` row (put in `markdowns/` by hand) needs its company: `--file path.md --isin SI…`.

---

## 2. What it does, in one picture

```
report_files ⋈ downloaded_files (category = 'reports') ⋈ stocks
        │  markdown_path, pdf_path, stock_isin, fiscal_year hint, nace, quantity
        ▼
for each Markdown file:
   hash ──► same hash + extractor version already done? ──► unchanged
     │
     ▼
   pages.py    split on <!-- page: N -->
   locate.py   income statement / balance sheet / cash flow / key figures pages
               (none of the first two → skipped, no model call)
   period.py   period_end, period_type, unit, currency from the text
   llm.py      one model call per statement: row label + cell text, copied
               every cell looked up on the page ─ not there → dropped
   numbers.py  "1.234,5" "(12)" → Decimal × unit
   derive.py   ebitda, roe, roa, ratios, valuation ratios
   validate.py checks → extracted | needs_review
   db.py       reports + financials + valuation_ratios + report_extractions, one transaction
```

---

## 3. Files

| File | Role |
|---|---|
| `run.py` | CLI, the loop over files, the per-file flow (`process`), summary and exit code |
| `db.py` | tracking tables (DDL), unique keys, input query, load transaction, error log |
| `pages.py` | `split_pages`, `table_rows` (Markdown table cells) |
| `metrics.py` | the canonical line items, statement/scope keywords in SI/HR/DE/EN, industry detection |
| `locate.py` | scores pages and picks the statement pages and the scope |
| `period.py` | dates, period type, unit and currency by regex |
| `llm.py` | prompt, JSON schema, the Ollama call, `find_number` verification |
| `prompts/system.txt`, `prompts/user.txt` | the prompt (placeholders `{{scope}}`, `{{company}}`, `{{statement}}`, `{{industry_hint}}`, `{{items}}`, `{{pages}}`) |
| `numbers.py` | `parse_number`, `decimal_style` |
| `derive.py` | `financials`, `valuations`: every computed column |
| `validate.py` | `validate`: every check |

Reused from elsewhere: `sha256_file`, `log`, `StageError`, `ROOT` (`dataPipeline/convert/common.py`), `start_run_log` (`helpers/run_log.py`), `model_settings` (`helpers/config.py`), `clean_json_string` (`helpers/helpers.py`).

---

## 4. Step by step

Every step that can fail raises `StageError(stage, exc)`. The file is counted as failed, the error goes to `report_extraction_errors` with its traceback, a `failed` row goes to `report_extractions`, and the batch continues. Stages: `read`, `db`, `locate`, `period`, `llm`, `parse`, `validate`.

1. **Skip check.** SHA-256 of the Markdown file. If `report_extractions` already has this `(markdown_path, markdown_hash, extractor_version)` with status `extracted`, `needs_review` or `skipped`, the file is `unchanged`. `failed` rows are retried. Reconverting the PDF (new Markdown) or raising `EXTRACTOR_VERSION` in `run.py` re-extracts.
2. **Pages.** `{page_no: text}` from the `<!-- page: N -->` markers. A file without markers is one page.
3. **Locate** (`locate.py`). A page can hold a statement only if it has a numeric table (≥ 5 table rows with a number of 3+ digits). This rejects tables of contents. It then needs the statement heading (*izkaz poslovnega izida, bilanca stanja, izkaz finančnega položaja, Bilanz, balance sheet …*) or ≥ 3 typical row labels. Headings in Markdown `#` lines and the first 6 lines count most. Each page gets a scope from *skupina / grupa / Konzern / consolidated* against *družba / društvo / separate*. The report's scope is **consolidated** whenever a consolidated income statement or balance sheet exists. The best page of that scope is chosen per statement, plus the next page when the table continues there (numeric table, no other statement heading, not the other scope). No income statement and no balance sheet → `skipped`, no model call (announcements, presentations).
4. **Period** (`period.py`). `period_end` is the latest month-end date on the statement pages (the current column; comparatives are older), else on the cover. `period_type` comes from cover keywords (*polletno, 1–9, devetmesečje, letno poročilo, Q3 …*), else from the month (Mar → Q1, Jun → 1H, Sep → Q3, Dec → FY). Unit: *v tisoč EUR, 000 EUR, TEUR, u tisućama, v mio …*. Currency: EUR / HRK / USD.
5. **Model calls** (`llm.py`, `run.run_statements`). In this order: income statement, balance sheet; the cash flow statement only if neither EBITDA nor D&A was found; the key-figures page only for items still missing (EBITDA, EPS, revenue, net income, and the dividend for FY reports). Each call sends only that statement's pages, with their page markers, and a JSON schema (Ollama `format`). The model returns, per item, the row label, the current and prior cell **as printed**, and the page.
6. **Verify** (`llm.find_number`). Each returned number is compared, by its digits and separators only, with every table cell on the statement's pages. Of several matches, the cell whose row label is most like the model's label wins (the cited page gets a small bonus). The cell's own text is used, so if the model drops the parentheses of `(33.087)` the sign still comes from the page. A number found in running text (a dividend in a sentence) is accepted as printed. A number that is nowhere on those pages is **dropped** and listed in the checks.
7. **Parse** (`numbers.py`). `1.234.567,89`, `1 234 567,89`, `1,234,567.89`, `(1.234)`, `-1.234`, `−`, `–`, `n/a`. The only ambiguous form, one separator followed by exactly 3 digits (`1.234`), uses the table's decimal style from its unambiguous numbers, and is read as thousands when the table does not decide it. Values are multiplied by the unit of the statement's pages (the model's unit if the text has none). Per-share items (EPS, dividend) are not multiplied.
8. **Derive, check, load**: sections 5–8.

---

## 5. Where each column comes from

`reports`:

| Column | Value |
|---|---|
| `stock_isin` | `downloaded_files.stock_isin` (or `--isin`) |
| `period_end`, `period_type` | step 4; the model's answer only where the text gives nothing |
| `currency` | from the text, else the model, else `EUR` (HRK reports stay HRK, nothing is converted) |
| `source_type` | ISIN prefix: `SI` Slovenia, `HR` Croatia, `AT` Austria, else Other |
| `source_file` | the PDF path relative to `reports/` (the Markdown path for `--file` without a PDF) |

`financials` (values in full currency units):

| Column | From |
|---|---|
| `revenue`, `ebit`, `net_income`, `total_assets`, `total_equity` | extracted (`operating_profit` → `ebit`) |
| `ebitda` | printed EBITDA, else `ebit + |depreciation_amortisation|` |
| `earnings_per_share` | printed basic EPS, else `net_income / stocks.quantity` |
| `roe`, `roa` | `net_income / total_equity × 100`, `net_income / total_assets × 100`, for the period, **not annualised** |
| `liabilities_equity_ratio` | `total_liabilities / total_equity` (total liabilities printed, else assets − equity) |
| `net_working_capital` | `current_assets − current_liabilities` |
| `net_liabilities` | net debt: `financial_liabilities (+ current part) − cash` |

`valuation_ratios`: price = `daily_prices.last_price` on the last trading day up to 14 days before `period_end`; market cap = price × `stocks.quantity`; EV = market cap + net debt. `p_e = price / eps`, `p_b = mcap / equity`, `p_s = mcap / revenue`, `ev_s = EV / revenue`, `net_debt_ebitda`, `dividend_per_share` (extracted, FY reports), `dividend_yield = dps / price × 100`. Without a price, the price-based columns are NULL and the row is still written.

Quarterly values are stored cumulative, as reported (Q3 = 1–9 months). A value that does not fit its `NUMERIC(p, s)` column is left NULL and listed as an `overflow` check.

Every number's origin is kept in `report_extractions.items`, for example:

```json
"net_income": {"statement": "income", "page": 4, "row_label": "Čisti poslovni izid obračunskega obdobja",
               "label_found": "Čisti poslovni izid obračunskega obdobja", "unit_scale": 1000,
               "current_raw": "45.678", "current": "45678000", "prior_raw": "40.100", "prior": "40100000"}
```

`items._derived` lists the values that were computed instead of read (e.g. `ebitda = ebit + depreciation_amortisation`).

---

## 6. Checks and `needs_review`

Any failed check makes the report `needs_review`. Its rows are still loaded, and the reasons are in `report_extractions.checks`. Find them with:

```sql
SELECT markdown_path, report_id, jsonb_pretty(checks) FROM report_extractions WHERE status = 'needs_review';
```

| Check | Fails when |
|---|---|
| `period` | no period found; text and model disagree on date or type; year more than 1 away from `downloaded_files.fiscal_year` |
| `coverage` | fewer than 4 of revenue, EBIT, net income, total assets, total equity |
| `not_on_page` | the model returned a number that is not printed on the pages (dropped), a cell that is not a number, or no unit was found |
| `balance` | total assets ≠ equity + liabilities (0.5% or one unit of rounding) |
| `sign`, `plausibility` | negative revenue or assets, EBIT > revenue, \|net income\| > 2 × revenue (not for banks/insurers) |
| `comparative` | the prior column ≠ the stored value: income items against the same period last year, balance items against last year end |
| `yoy` | revenue, total assets or equity changed more than 10× |
| `cumulative` | revenue breaks Q1 ≤ 1H ≤ Q3 ≤ FY against stored reports of the same year |
| `conflict` | the period is already stored from another file with different values (section 8) |
| `overflow` | a computed value does not fit its column |

Banks and insurers are detected from `stocks.nace` (64.1 / 65) or the sector/company name. The prompt then tells the model what counts as revenue for them, and the revenue checks are skipped.

---

## 7. Database

Created on start (`db.ensure_tables`) and in `schema.sql` section 7:

- `report_extractions`: one row per `(markdown_path, markdown_hash, extractor_version)` with `status` (`extracted | needs_review | skipped | failed`), `report_id`, `scope`, `unit_scale`, `model`, `items`, `checks`, `error`.
- `report_extraction_errors`: append-only, one row per failure, with stage and traceback.
- Unique keys `reports (stock_isin, period_end, period_type)`, `financials (report_id)`, `valuation_ratios (report_id)`. If existing rows violate one, the run stops and lists them instead of failing halfway.
- The `reports` / `financials` / `valuation_ratios` id sequences are moved past the highest existing id, in case rows were inserted with explicit ids.

All rows of one file are written in one transaction.

---

## 8. Reruns, duplicates and `--force`

- Running twice changes nothing: the second run reports every file as `unchanged`.
- A period that is already stored **from the same PDF** is updated in place.
- A period already stored **from another file** (an SI and an EN version, a separate financial-statements PDF, or `sample_data.sql`) is not overwritten. With the same values (within 0.5%) the file is only recorded. With different values the stored row is kept, and the file becomes `needs_review` with a `conflict` check.
- `--force` re-extracts files already done and overwrites such periods.
- `--dry-run` reads the database but writes nothing (not even the tracking tables) and prints what it would write.

---

## 9. Decisions and known limits

- **Group figures win** when a report has both group and company statements. The schema has no scope column, so the scope is only recorded in `report_extractions.scope`.
- **The prior-year column is not loaded** as rows (the schema has no `is_comparative`). It is used for the `comparative` check and kept in `items`.
- **ROE/ROA and P/E are not annualised** for interim periods.
- Financial years are assumed to end in December when the period type comes from the month, and for the balance-sheet comparison of interim reports.
- Accuracy depends on the model: check `needs_review` rows after the first runs on real reports, and adjust the keywords in `metrics.py` or the prompt.
- Scanned reports work only as well as their Markdown (vision-OCR fallback in `convert/`).

---

## 10. Tests

`tests/test_extract_*.py`: synthetic inputs and a fake model, no database, no Ollama. See `tests/README.md`.

```bash
python -m pytest tests/test_extract_*.py -q
```
