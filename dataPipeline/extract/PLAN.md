# Plan: `dataPipeline/extract`, Markdown reports → SQL rows

Status: plan only, nothing is implemented yet. Covers README Step 4 (Markdown → structured SQL rows) for the tables already in `schema.sql`: `reports`, `financials` and `valuation_ratios`.

```
python -m dataPipeline.extract.run                      # every new or changed Markdown report
python -m dataPipeline.extract.run --isin SI0031104332  # only these companies
python -m dataPipeline.extract.run --file markdowns/petrol/reports/letno_2024.md
python -m dataPipeline.extract.run --dry-run            # extract and validate, print, write nothing
python -m dataPipeline.extract.run --force              # re-extract files already done
```

---

## 1. Where it sits

```
download/  ──►  reports/<company>/reports/*.pdf          downloaded_files (stock_isin, fiscal_year, link_text)
convert/   ──►  markdowns/<company>/reports/*.md          report_files (pdf_path, markdown_path, status)
extract/   ──►  reports + financials + valuation_ratios   report_extractions (tracking, this step)
```

Inputs the step already has, with nothing new to collect:

| Needed | Where it comes from |
|---|---|
| Markdown text, split per page | `report_files.markdown_path`, with `<!-- page: N -->` markers from `convert/` |
| Company (`stock_isin`) | `report_files.pdf_path = downloaded_files.file_path` → `downloaded_files.stock_isin` |
| Only real reports, not announcements | `downloaded_files.category = 'reports'` |
| Year hint | `downloaded_files.fiscal_year`, `link_text` (a hint only; the document decides) |
| `source_type` | ISIN prefix / `stocks.mic`: `SI…` → Slovenia, `HR…` → Croatia, `AT…` → Austria, else Other |
| Share count, market price (for `valuation_ratios`) | `stocks.quantity`, `daily_prices.last_price` on the last trading day ≤ `period_end` |

A Markdown file without a `report_files` row (put there by hand) needs `--isin`; it is skipped with a clear message otherwise.

---

## 2. Files

```
dataPipeline/extract/
  README.md          how it works (same style as convert/README.md), written with the code
  run.py             CLI: argument parsing, the loop over files, counts, exit code 1 if anything failed
  db.py              DDL for the tracking tables, input query, the load transaction
  pages.py           split Markdown on <!-- page: N --> into {page_no: text}
  locate.py          find the statement pages (income statement, balance sheet, cash flow) by keywords
  period.py          period_end / period_type / scope / currency / unit from the cover and statement headers
  metrics.py         THE canonical list of raw line items + SI/HR/DE/EN synonyms
  llm.py             builds the prompt, calls Ollama with a JSON schema, returns raw strings
  prompts/system.txt
  prompts/user.txt
  numbers.py         "1.234.567,89" / "(1.234)" / "-" / "v tisoč EUR" → Decimal, deterministic
  derive.py          ebitda, roe, roa, ratios, valuation_ratios from the raw items
  validate.py        consistency checks → status 'extracted' or 'needs_review'
tests/
  test_extract_numbers.py
  test_extract_locate.py
  test_extract_derive.py
  test_extract_validate.py
  gold/extract/*.json        hand-labelled expected values for 8–10 real reports
  score_extract.py           per-metric accuracy against the gold set
```

`run.py` is started with `python -m` like `download/run.py`, so the relative imports and `helpers.*` resolve without the `sys.path` workaround `convert/docling.py` needs.

---

## 3. Per file, step by step

Every step either succeeds or raises a stage error (`locate`, `period`, `llm`, `parse`, `validate`, `db`). The error is logged in `report_extraction_errors` and the batch continues, the same pattern as `convert/common.py`.

1. **Skip check.** SHA-256 of the Markdown file. Skip if `report_extractions` already has `(markdown_path, markdown_hash, extractor_version)` with status `extracted` or `needs_review`. A reconverted PDF (new Markdown hash) or a new extractor version is extracted again.
2. **Split pages** (`pages.py`). `{page_no: text}`. Page numbers are kept all the way to the tracking row so every number can be cited.
3. **Locate statements** (`locate.py`). Score each page by multilingual heading keywords from `metrics.py`:
   - income statement: *izkaz poslovnega izida, račun dobiti i gubitka, Gewinn- und Verlustrechnung, income statement, statement of profit or loss*
   - balance sheet: *izkaz finančnega položaja, bilanca stanja, bilanca, Bilanz, statement of financial position, balance sheet*
   - cash flow: *izkaz denarnih tokov, izvještaj o novčanim tokovima, Kapitalflussrechnung, cash flow statement*
   - scope: *skupina / grupa / Konzern / consolidated* vs *družba / društvo / separate / parent*

   A page needs a heading hit **and** a table (`|` rows) with numbers. Keep the best page (plus the next page if the table continues) per statement × scope. If no income statement and no balance sheet is found, the file is marked `skipped` (`not a financial statement report`) without calling the LLM. Most announcements and presentations end here, cheaply.
4. **Choose scope.** If both are present, use **consolidated** (that is what analysts and the RAG questions mean by "the company's revenue"). Otherwise use what is there. Stored in `report_extractions.scope`, since `financials` has no scope column.
5. **Period and units** (`period.py`). Regex on the cover page and the statement headers first: dates (`31.12.2024`, `30. 6. 2025`), *letno / annual / godišnje* → FY, *polletno / 1H / polugodišnje / half-year* → 1H, *1–3 / Q1 / prvo trimesečje / prvo tromjesečje* → Q1, *1–9 / Q3 / devetmesečje* → Q3, *Q4 / 1–12 unaudited* → Q4. Unit: *v EUR*, *v tisoč EUR / 000 EUR / TEUR / u tisućama*, *v mio EUR*. The LLM (next step) returns the same fields from the table header. If the two disagree, or neither matches `downloaded_files.fiscal_year`, the result is kept but the status becomes `needs_review`.
6. **LLM mapping** (`llm.py`). One call per located statement, only that statement's pages in the prompt (small context, fast on CPU). Ollama with `format=<JSON schema>`, `temperature 0`, model from a new `config.json` key `extractionModel` (bigger model, batch job; e.g. `qwen2.5:14b` or `qwen3:8b`, `num_ctx 16384`). The model **maps rows, it never writes numbers**:

   ```json
   {"period_end": "2024-12-31", "period_type": "FY", "currency": "EUR", "unit": "thousand",
    "scope": "consolidated",
    "items": {
      "revenue":   {"row_label": "Čisti prihodki od prodaje", "current": "1.234.567", "prior": "1.101.220", "page": 57},
      "net_income":{"row_label": "Čisti poslovni izid obdobja", "current": "(12.345)", "prior": "8.101", "page": 57},
      "total_assets": null
    }}
   ```
7. **Verify and parse** (`numbers.py`). Each `current` / `prior` string must appear in the text of the page it cites, after normalising whitespace. If not, it is a hallucination: the item is dropped and noted. Then parse in Python: `.` thousands / `,` decimals (SI, HR, AT) or the reverse (EN) detected per table, `(x)` and `-x` negative, `–`, `—`, `-` empty → null, multiplied by the unit scale. Results are in full currency units, matching `NUMERIC(20,2)`.
8. **Derive** (`derive.py`). Everything in `financials` that is a ratio or a sum is computed here, never asked from the LLM (see §4).
9. **Validate** (`validate.py`). See §5. Sets `extracted` or `needs_review`; never silently drops a report.
10. **Load** (`db.py`), one transaction per file, see §6.

---

## 4. Mapping to `schema.sql`

Raw line items extracted (`metrics.py`); one list, imported by the prompt, the parser and the tests:

| Raw item | Statement | Used for |
|---|---|---|
| `revenue` | IS | `financials.revenue`, `p_s`, `ev_s` |
| `operating_profit` (EBIT) | IS | `financials.ebit`, `ebitda` |
| `depreciation_amortisation` | IS or CF | `ebitda` |
| `net_income` (attributable to owners of the parent when shown) | IS | `net_income`, `roe`, `roa`, `p_e` |
| `eps` | IS | `earnings_per_share` |
| `total_assets` | BS | `total_assets`, `roa` |
| `total_equity` (incl. non-controlling interests) | BS | `total_equity`, `roe`, `p_b` |
| `total_liabilities` (or derived: assets − equity) | BS | `liabilities_equity_ratio` |
| `current_assets`, `current_liabilities` | BS | `net_working_capital` |
| `financial_liabilities` (non-current + current borrowings, leases) | BS | `net_liabilities`, `net_debt_ebitda`, `ev_s` |
| `cash` (cash and cash equivalents) | BS | `net_liabilities` |
| `dividend_per_share` | notes / text, FY only | `valuation_ratios.dividend_per_share` |

Derived columns:

| Column | Formula | Note |
|---|---|---|
| `ebitda` | reported EBITDA if the table has it, else `ebit + depreciation_amortisation` | |
| `roe` | `net_income / total_equity × 100` | period value, **not annualised** (see §8) |
| `roa` | `net_income / total_assets × 100` | same |
| `liabilities_equity_ratio` | `total_liabilities / total_equity` | |
| `net_working_capital` | `current_assets − current_liabilities` | |
| `net_liabilities` | `financial_liabilities − cash` | net debt |
| `earnings_per_share` | reported; else `net_income / stocks.quantity` | flagged in the tracking row when computed |
| `valuation_ratios.*` | `market_cap = price × stocks.quantity`; `p_e = price / eps`, `p_b = market_cap / equity`, `p_s = market_cap / revenue`, `ev_s = (market_cap + net_liabilities) / revenue`, `net_debt_ebitda`, `dividend_yield = dps / price × 100` | price = `daily_prices.last_price` on the last trading day ≤ `period_end`; no price → the row is still written with nulls for the price-based columns |

Values are stored as reported for the period: Slovenian and Croatian quarterly reports are cumulative (Q3 = 1–9 months), which is also how `sample_data.sql` behaves (revenue rises Q1 → 1H → Q3 → FY).

`reports` row:

| Column | Value |
|---|---|
| `stock_isin` | from `downloaded_files` |
| `period_end`, `period_type` | from step 5 |
| `currency` | from the statement header (`EUR`; `HRK` for pre-2023 Croatian reports, stored as reported, not converted) |
| `source_type` | from the ISIN prefix |
| `source_file` | `pdf_path` relative to `reports/` (same meaning as the file names in `sample_data.sql`) |

The **prior-year column** is extracted but not loaded as its own `reports` row (the schema has no `is_comparative`). It is used by validation (§5) and kept in the tracking row's JSON, so it can be loaded later if the schema gets that column.

---

## 5. Validation

Each failed check is written to `report_extractions.checks` (JSON) and sets `needs_review`. The rows are still loaded, so nothing is lost, and the review page can show them.

- `total_assets ≈ total_liabilities + total_equity`, within 0.5% or the rounding of the unit.
- `ebit ≤ revenue`, `|net_income| ≤ revenue × 2`, non-banks only.
- `total_assets > 0`, `revenue ≥ 0`.
- Period: regex and LLM agree; the year matches `downloaded_files.fiscal_year` when that is set.
- Prior-year column ≈ the own-period value already stored for that period (from last year's report), within 0.5%. A mismatch means a restatement or an extraction error, flagged either way.
- Year-over-year change of more than 10× on revenue, total assets or equity.
- Cumulative quarters: `revenue(Q1) ≤ revenue(1H) ≤ revenue(Q3) ≤ revenue(FY)` for the same year, when the others exist.
- Fewer than 4 of the core items (`revenue`, `ebit`, `net_income`, `total_assets`, `total_equity`) found.
- Any item dropped because its raw string was not on the page.

Banks and insurers (from `stocks.nace` / `sector_name`) have no revenue line and a different balance sheet. `metrics.py` gets a `bank` / `insurer` subset (e.g. revenue = net interest income + net fee income for banks), and the revenue checks are skipped for them.

---

## 6. Database

### 6.1 New tables (owned by this step, created idempotently on start, like `convert/common.py`)

```sql
CREATE TABLE IF NOT EXISTS report_extractions (
    extraction_id     SERIAL PRIMARY KEY,
    markdown_path     TEXT     NOT NULL,             -- relative to repo root
    markdown_hash     CHAR(64) NOT NULL,
    extractor_version TEXT     NOT NULL,             -- bump to re-extract everything
    report_id         INT REFERENCES reports(report_id) ON DELETE SET NULL,
    status            TEXT NOT NULL CHECK (status IN ('extracted','needs_review','skipped','failed')),
    scope             TEXT CHECK (scope IN ('consolidated','separate')),
    unit_scale        INT,                           -- 1, 1000, 1000000
    model             TEXT,                          -- e.g. qwen2.5:14b
    items             JSONB,                         -- per item: row_label, raw current/prior, page, parsed value
    checks            JSONB,                         -- failed validation checks
    error             TEXT,                          -- "<stage>: <message>" when failed / reason when skipped
    extracted_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (markdown_path, markdown_hash, extractor_version)
);

CREATE TABLE IF NOT EXISTS report_extraction_errors (
    error_id      SERIAL PRIMARY KEY,
    markdown_path TEXT NOT NULL,
    markdown_hash CHAR(64),
    stage         TEXT NOT NULL,   -- locate | period | llm | parse | validate | db
    error         TEXT NOT NULL,
    traceback     TEXT,
    occurred_at   TIMESTAMPTZ DEFAULT now()
);
```

`items` keeps the page reference and the row label for every number, so the RAG side can cite it (`page_reference`) without changing the `financials` table.

### 6.2 Small additions to existing tables (needed for safe reruns)

```sql
CREATE UNIQUE INDEX IF NOT EXISTS reports_period_key      ON reports (stock_isin, period_end, period_type);
CREATE UNIQUE INDEX IF NOT EXISTS financials_report_key    ON financials (report_id);
CREATE UNIQUE INDEX IF NOT EXISTS valuation_ratios_report_key ON valuation_ratios (report_id);
```

Without them a rerun cannot upsert, and one period could end up with two `financials` rows. The step checks for existing duplicates first and stops with a list of them instead of failing on the index.

### 6.3 Load transaction

1. `INSERT INTO reports … ON CONFLICT (stock_isin, period_end, period_type) DO UPDATE SET currency, source_type, source_file` → `report_id`.
2. `INSERT INTO financials … ON CONFLICT (report_id) DO UPDATE` for every column.
3. Same for `valuation_ratios`.
4. Upsert the `report_extractions` row.

**Two files for one period** (an SI and an EN version, or an annual report and a separate financial-statements PDF): the second one does not overwrite the first blindly. If its values match within 0.5%, it is only recorded in `report_extractions`. If they differ, the existing row is kept and the new file is `needs_review`. `--force` overwrites.

**Rows not created by this step** (e.g. `sample_data.sql`): updated like any other, since the period key is the same. `--dry-run` shows which ones would change.

---

## 7. Implementation order

1. **Groundwork (½ day).** `metrics.py`, `extractionModel` in `config.json`, the DDL in `db.py`, the duplicate check for §6.2.
2. **Gold set (½–1 day, by hand).** 8–10 converted reports: FY and 1H/Q, SI and HR, one bank, one insurer, one scanned (vision-OCR) report. Write the expected values to `tests/gold/extract/<file>.json`. Every later change is scored against it.
3. **Deterministic parts with unit tests (1 day).** `pages.py`, `numbers.py`, `locate.py`, `period.py`, `derive.py`, `validate.py`. No database, no Ollama, synthetic input, like the existing tests.
4. **LLM mapping (1–2 days).** `llm.py` + prompts, raw-string verification. Tune on the gold set with `tests/score_extract.py` (per-metric accuracy, plus how many wrong values validation caught).
5. **Load + CLI (1 day).** `db.py` load transaction, `run.py` with `--isin`, `--file`, `--dry-run`, `--force`, `start_run_log("extract")`, the `Done: N extracted, N needs_review, N skipped, N failed` summary, exit code 1 on failures.
6. **Docs (½ day).** `dataPipeline/extract/README.md`; a line under README Step 4; update `rag/prompts/sql/system.txt`, which currently mentions `is_comparative` and `page_reference` that the schema does not have.

**Done when:** ≥ 95% of the gold-set values are correct, every wrong value is caught as `needs_review`, and a second run with no changes extracts nothing.

---

## 8. Decisions to confirm (default chosen in brackets)

1. **Scope** when both statements exist: [consolidated], or store the separate one too (needs a `scope` column in `reports`).
2. **ROE / ROA for quarters:** [period value, not annualised], or annualised (×4 for Q1, ×2 for 1H, ×4/3 for Q3).
3. **Comparative column:** [validation only], or add `is_comparative` to `reports` and load prior-year values as extra rows (the SQL prompt already expects that column).
4. **Indexes in §6.2** on existing tables: [add them], or keep `schema.sql` untouched and do look-up-then-update in code.
5. **`valuation_ratios`:** [computed in the same run from `daily_prices`], or a separate later step.
6. **Model:** [`qwen2.5:14b` overnight], or the same `gemma3:4b` as the RAG side (faster, expect more `needs_review`).
