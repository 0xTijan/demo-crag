# Data Pipeline Roadmap — from company list to SQL rows

Covers README Steps 0–4 (discovery → download → PDF conversion → numeric extraction),
with a short note on Step 5 (vector index), since it reuses the same converted documents.
Written 2026-10-31. `dataPipeline/` and `reports/` are currently empty, so this starts from scratch.

---

## 1. Review of the README design

The overall shape is right: it is staged, every number is traceable to a source, the steps are idempotent and dedup is hash-based.
The items below are what I would change before writing code.

### 1.1 Things that conflict and need a decision first

| Topic | README says | Code / `schema.sql` says | Recommendation |
|---|---|---|---|
| `reports` columns | `source_url, file_hash, fiscal_year, report_type, language, status` | `period_end, period_type, source_type, source_file` | Merge both sets of columns (see §3). Keep `period_end` as the truth and derive `fiscal_year` from it, because some fiscal years don't end on 31 Dec. |
| `financials` shape | EAV, one row per metric | Wide, one column per metric | **Store facts as EAV, and expose a wide `VIEW` named `financials`** for the RAG side. You get EAV's flexibility for loading, and text-to-SQL stays easy (a small model writes far better SQL against wide tables). The existing SQL prompt keeps working. |
| Metric list | 12 metrics (cogs, gross_profit, cash_*…) | revenue, ebit, eps, roe, roa, net_liabilities… | One canonical list in **one file** (`dataPipeline/metrics.py`), imported by extraction, the classifier prompt and the SQL prompt. Derived ratios (roe, roa, margins, L/E) belong in the view, not in extraction. |
| Step 3.1 vs 3.2 | 3.1: "image-only → Docling, else PyMuPDF". 3.2: "digital → Docling, scanned → OCR". | — | These contradict each other. See §1.3. |

### 1.2 Discovery (Step 1): a generic company-website crawler

Company websites are the source, because the crawler will later collect other kinds of data too
(press releases, dividend announcements, AGM documents, management info…). The README's flow
(search → crawl → classify PDFs) is built only for reports. The main change: **split crawling from
classifying.** The crawler maps each company site and stores *everything it sees* (pages and document links)
without knowing what it's looking for. Classification runs afterwards as pluggable **targets**:
`financial_report` now, others later. A new data type then needs only a new target, not a new crawler.

```
seed companies ─► site crawler ─► web_pages + web_links (raw, type-agnostic)
                                         │
                       targets (plugins) ┼─► financial_report ─► report_candidates ─► download (Step 2)
                                         ├─► press_release    (later)
                                         └─► dividends / AGM  (later)
```

**Crawler design:**
- **Entry points, cheapest first:** `robots.txt` → `sitemap.xml` (often lists every PDF with `lastmod`, so no rendering is needed) → `homepage_url`. Use a search engine (README 1.1) only when the homepage is missing or the crawl finds no IR section.
- **Focused crawl, not a full crawl:** a priority queue on the same domain with a depth limit of 3 and a page limit per site of about 300. Score links by keywords that *each target registers* (for reports: *investitorji, za vlagatelje, finančna poročila, letno poročilo, financijska izvješća, investor relations, reports*). Follow high-scoring links first. Other targets add their own keywords later, and the crawler code doesn't change.
- **httpx first, Playwright per site when needed.** If the static HTML has almost no links, or the IR page looks like a JS app, set `sites.needs_js = true` and render that site with Playwright from then on (scrolling, "load more", year filters and tabs). Record this per site so it's detected only once.
- **Learn entry points.** Once the IR or reports pages of a site are found, store them in `site_entry_points`. Later recrawls start there and finish in seconds, and the full crawl reruns only monthly or when the entry points stop yielding results.
- **Incremental recrawl:** keep `ETag` / `Last-Modified` / content hash per URL and send conditional requests, so unchanged pages cost almost nothing. A new link on a known IR page is the trigger for "new report published".
- **Real-world quirks to handle:** language versions (`/en/`, `/sl/`, `/hr/`): crawl one, record the other. Documents served from another domain (a CDN, a hosted IR provider, or links to the exchange's announcement) go on an allow-list per site. Links without `.pdf`: check them with a HEAD request for `content-type`. Pagination and filters on document libraries.
- **Last resort:** `browser-use` (agentic browsing, as in the README) only for sites where the scripted crawl finds nothing. Log these sites, since they're usually worth a small custom rule.
- **Politeness:** respect `robots.txt`, about 1 request/second per domain, an honest user agent, and cache everything.

**Classification for the `financial_report` target (README 1.3):**
- **Run regex heuristics before the LLM:** years (`20\d\d`), *letno/annual/godišnje*, *polletno/half-year/polugodišnje*, *Q1–Q4, trimesečje, tromjesečje*, *konsolidirano/consolidated*, *revidirano/audited*. Most links classify confidently from these plus the surrounding context (table row, heading). Send only the leftovers to the LLM.
- **Confirm the classification after download** from the PDF itself: the cover page and the first statement header give the period and type. The URL and anchor text are only hints.
- **Collect ESEF files when the site has them.** Many listed companies post the annual report as an ESEF package (`.zip` / `.xhtml`) next to the PDF. The statements in it are machine-tagged with the IFRS taxonomy, so the numbers can be read **without an LLM**. Have the crawler accept these links as a second document format; extraction then gets a free, exact path (Phase 4).

### 1.3 Conversion (Step 3)

- Annual reports run 150–300 pages, and Docling on a CPU takes seconds per page. **Don't run Docling on the whole document for numeric extraction.**
  1. Extract text per page with PyMuPDF for every page (fast). This also detects scanned PDFs (pages with almost no text).
  2. Locate the statement pages using keywords on that per-page text (README 3.4, moved earlier).
  3. Run **Docling only on those ~5–15 pages** for good tables. Docling has built-in OCR, so scanned statement pages take the same path.
  4. Full-document Docling, which Step 5 needs for narrative text, runs as a separate, lower-priority overnight job.
- **Keep page numbers.** Plain Markdown loses them, but `page_reference` and citations depend on them. Insert page markers (`<!-- page: 57 -->`) or store per-page Markdown.
- Store a `converter` and `converter_version` per document so you can reprocess when Docling improves.

### 1.4 Extraction (Step 4): what decides accuracy

- **Consolidated vs separate statements.** Most annual reports contain both the group (*skupina / grupa*) and the parent company (*družba / društvo*). If you don't track which one a number came from, you will silently mix them. Add `scope` = `consolidated | separate` to every fact.
- **Units and number formats.** Handle "v EUR", "v tisoč EUR", "000 EUR", "v mio EUR"; Slovenian/Croatian formatting `1.234.567,89`; negatives as `(1.234)` or `-1.234`; and HRK before 2023 (Croatia switched to EUR). Store `unit_scale` and `currency` explicitly.
- **Don't let the LLM write numbers.** Ask it to *map* rows to metrics: return the row label and the raw cell string for each metric and column. Then:
  1. Parse the string to a number **in Python** (deterministic handling of format and scale).
  2. **Check that the raw string actually occurs** in the source table on that page. If it doesn't, the value was hallucinated and gets rejected.
- **Banks and insurers** (Savinja Banka, Triglavska Zavarovalnica in your sample) have no revenue or COGS and use a different statement structure. Store `industry_template` per company (`general | bank | insurer`) and use a matching metric subset and prompt. Nulls for the rest are fine.
- **Comparative columns are restated values.** The 2023 column in the 2024 report may differ from the 2023 report. Store both with `is_comparative`, and define a policy for the view: own-period value first, otherwise the latest restatement.
- **Validation (4.2), extended:**
  - assets = liabilities + equity (±0.5% or ± rounding of `unit_scale`)
  - gross_profit = revenue − cogs, when all three are present
  - this year's comparative ≈ last year's own-period value; a mismatch suggests a restatement or an extraction error, so flag it either way
  - year-over-year change above 10× → flag
  - quarterly values that look cumulative (Q3 YTD) vs discrete → record `period_start` explicitly
- **Measure, don't guess.** Build a small **gold set** by hand (§4, Phase 0) and score every extraction change against it.

### 1.5 Operations

- **Status machine per document**, with every step idempotent and safe to rerun:
  `discovered → downloaded → converted → statements_located → extracted → validated | needs_review → loaded`
  Store `last_error` and `attempts`, so failures don't block the batch and can be retried.
- **Dedup by meaning, not only by hash.** The same report under two URLs, re-saved with different bytes, or in both SI and EN versions has different hashes. Add a uniqueness check on `(company_id, period_end, period_type, scope, language)`. Pick one preferred language per company, the original, and keep the other only for narrative use.
- **Orchestration:** start with a plain CLI (`python -m dataPipeline run download --company 4`). Add Prefect only once scheduling is actually needed, because it's a lot of setup for a single-machine batch job.
- **Review queue:** for now, a `needs_review` status plus a page in the existing Streamlit `db_gui.py` is enough. It should show the PDF page image next to the extracted values.

---

## 2. Target folder layout

```
dataPipeline/
  __init__.py
  __main__.py            # CLI: python -m dataPipeline <step> [--company ID] [--report ID]
  config.py              # paths, model names, thresholds
  db.py                  # one connection pool; small repository functions
  metrics.py             # THE canonical metric list + multilingual synonyms
  crawl/                 # type-agnostic: knows nothing about reports
    frontier.py          # priority queue, depth/page limits, per-domain rate limit
    fetch.py             # httpx (conditional GET) → Playwright fallback, robots.txt
    sitemap.py           # sitemap.xml discovery
    links.py             # link + context extraction (anchor, row, heading), HEAD mime check
    store.py             # web_pages / web_links / site_entry_points
  targets/               # pluggable "what are we looking for"
    base.py              # Target interface: keywords(), classify(link), handle(candidate)
    financial_report.py  # regex → LLM classification → report_candidates
    # press_release.py, dividends.py ... later
  ingest/download.py     # fetch, verify PDF, hash, store, insert row
  convert/
    pages.py             # PyMuPDF per-page text, scanned detection
    locate.py            # find statement pages (keywords → embedding fallback)
    docling_tables.py    # Docling on selected pages → Markdown with page markers
  extract/
    esef.py              # iXBRL → facts (no LLM)
    llm_map.py           # LLM row→metric mapping (structured output)
    numbers.py           # parse "1.234,5" / "(12)" / unit scale
    validate.py
    load.py
  prompts/
reports/                 # (gitignored) data
  raw/{company_id}/{fiscal_year}/{period_type}_{hash8}.pdf
  processed/{company_id}/{report_id}/pages.jsonl, statements.md, extraction.json
tests/
  gold/                  # hand-labelled expected values per report
```

---

## 3. Schema changes (first migration)

```sql
-- seed list (README Step 0); replaces / extends companies
ALTER TABLE companies
  ADD COLUMN homepage_url TEXT,
  ADD COLUMN ajpes_id TEXT,
  ADD COLUMN sector TEXT,
  ADD COLUMN industry_template TEXT DEFAULT 'general'
      CHECK (industry_template IN ('general','bank','insurer')),
  ADD COLUMN name_aliases TEXT[] DEFAULT '{}';

-- crawler (type-agnostic)
CREATE TABLE sites (
  site_id SERIAL PRIMARY KEY,
  company_id INT REFERENCES companies(company_id),
  base_url TEXT NOT NULL,
  needs_js BOOLEAN DEFAULT FALSE,
  extra_allowed_domains TEXT[] DEFAULT '{}',   -- CDNs, hosted IR providers
  last_full_crawl TIMESTAMPTZ
);
CREATE TABLE web_pages (
  page_id SERIAL PRIMARY KEY,
  site_id INT REFERENCES sites(site_id),
  url TEXT NOT NULL UNIQUE,
  title TEXT, language TEXT, depth INT,
  content_hash CHAR(64), etag TEXT, last_modified TEXT,
  http_status INT, fetched_at TIMESTAMPTZ
);
CREATE TABLE web_links (
  link_id SERIAL PRIMARY KEY,
  page_id INT REFERENCES web_pages(page_id),
  url TEXT NOT NULL,
  anchor_text TEXT, context TEXT,          -- table row / nearby heading
  mime_type TEXT,                          -- from extension or HEAD
  first_seen TIMESTAMPTZ DEFAULT now(),
  UNIQUE (page_id, url)
);
CREATE TABLE site_entry_points (            -- learned per target
  site_id INT REFERENCES sites(site_id),
  target TEXT,                              -- financial_report | press_release ...
  url TEXT,
  last_yield TIMESTAMPTZ,                   -- last time it produced a new candidate
  PRIMARY KEY (site_id, target, url)
);

-- financial_report target output: every classified link, incl. rejected (avoid re-classifying)
CREATE TABLE report_candidates (
  candidate_id SERIAL PRIMARY KEY,
  company_id INT REFERENCES companies(company_id),
  link_id INT REFERENCES web_links(link_id),
  url TEXT NOT NULL UNIQUE,
  file_format TEXT,            -- pdf | esef
  anchor_text TEXT, context TEXT,
  guessed_period_type TEXT, guessed_fiscal_year INT,
  confidence REAL, classified_by TEXT,   -- regex | llm | manual
  status TEXT DEFAULT 'new',   -- new | accepted | rejected | review | downloaded
  discovered_at TIMESTAMPTZ DEFAULT now()
);

-- extend reports
ALTER TABLE reports
  ADD COLUMN source_url TEXT,
  ADD COLUMN file_hash CHAR(64) UNIQUE,
  ADD COLUMN file_format TEXT DEFAULT 'pdf',   -- pdf | esef
  ADD COLUMN language TEXT,
  ADD COLUMN status TEXT DEFAULT 'downloaded',
  ADD COLUMN last_error TEXT,
  ADD COLUMN attempts INT DEFAULT 0,
  ADD COLUMN downloaded_at TIMESTAMPTZ DEFAULT now();

-- EAV facts
CREATE TABLE financial_facts (
  fact_id SERIAL PRIMARY KEY,
  report_id INT REFERENCES reports(report_id) ON DELETE CASCADE,
  metric TEXT NOT NULL,              -- from dataPipeline/metrics.py
  value NUMERIC(24,4),
  currency TEXT, unit_scale INT,     -- 1, 1000, 1000000 (value is already scaled)
  period_start DATE, period_end DATE NOT NULL,
  scope TEXT CHECK (scope IN ('consolidated','separate')),
  is_comparative BOOLEAN NOT NULL,
  page_number INT, source_label TEXT, raw_value TEXT,
  method TEXT,                       -- esef | llm | manual
  status TEXT DEFAULT 'ok',          -- ok | needs_review | rejected
  UNIQUE (report_id, metric, period_end, scope, is_comparative)
);

-- wide view for the RAG/SQL side: replaces the current financials table
-- CREATE VIEW financials AS SELECT ... pivot of financial_facts, preferring
-- own-period over comparative, consolidated over separate, + derived roe/roa/margins
```

---

## 4. Roadmap

The key ordering decision: **build the pipeline from the middle outward.** Start with a
hand-written list of PDF URLs and get download → convert → extract → load working and
measured. The crawler comes after that. It's the biggest piece and the one you'll keep
extending, but you can only judge it properly once the downstream steps exist. Phase 0's
site survey gives it a ready-made test set, so no time is lost.

### Phase 0 — Decisions & groundwork (1–1½ days)
- [ ] Decide the §1.1 conflicts (EAV + view, canonical metric list, `reports` columns).
- [ ] **Site survey (2–3 hours, done by hand) for 10 companies of different kinds.** Per site, write `tests/gold/sites/<company>.json` with:
  the IR / reports page URLs and how many clicks from the homepage they are; static HTML or JS-rendered;
  whether `sitemap.xml` exists and lists the PDFs; which domains host the documents; whether there are pagination, filters or tabs;
  language versions; whether an ESEF package is offered; and **the full list of report URLs on the site**.
  This becomes the crawler's test set (Phase 5), shows which crawler features matter most, and supplies the URLs for Phase 1.
- [ ] Write `dataPipeline/metrics.py` (canonical names, SI/HR/EN synonyms, per-industry subsets).
- [ ] Apply the §3 migration to a dev database.
- [ ] **Gold set:** from the surveyed sites, pick 8–10 real reports (mix of annual/half-year, SI/HR, one bank, one insurer, one scanned if you have one). Fill in the expected metric values by hand in `tests/gold/reports/*.json`. This becomes your accuracy benchmark for extraction.

### Phase 1 — Manual URLs → stored PDFs (Step 2) (1 day)
- [ ] `seeds/manual_urls.csv`: `company_id, url`, containing the gold-set reports.
- [ ] `ingest/download.py`: httpx with retry/backoff, PDF magic-byte check (`%PDF`), SHA-256, dedup against `reports.file_hash`, save to `reports/raw/...`, insert a `reports` row with `status='downloaded'`.
- [ ] CLI: `python -m dataPipeline download --from seeds/manual_urls.csv`.
- **Done when:** running it twice inserts nothing new the second time.

### Phase 2 — PDF → located statement pages (Step 3) (2–3 days)
- [ ] `convert/pages.py`: PyMuPDF per-page text into `pages.jsonl`; flag scanned pages.
- [ ] `convert/locate.py`: keyword scoring per page for balance sheet / income statement / cash flow × consolidated/separate, in SI/HR/EN. Return a page range per statement.
- [ ] Detect the period from the PDF (cover page, statement headers) and correct the `reports` row.
- [ ] `convert/docling_tables.py`: Docling on the located pages only → Markdown with page markers.
- **Done when:** the right pages are found for ≥ 90% of gold reports. Log failures for the embedding fallback (deferred until Step 5's embedding setup exists).

### Phase 3 — Extraction → `financial_facts` (Step 4) (3–5 days)
- [ ] `extract/numbers.py` + unit tests: `1.234.567,89`, `(1.234)`, `-`, `—`, `v tisoč EUR`, HRK.
- [ ] `extract/llm_map.py`: per statement table, structured output (Ollama `format=<schema>`, `temperature 0`) returning `{metric: {row_label, current_raw, prior_raw}}`, plus `unit_scale`, `currency`, `period_end`, `scope`. Use the bigger model here (overnight batch, as the README already plans).
- [ ] Verify each raw string exists in the source table, then parse in Python.
- [ ] `extract/validate.py`: the checks in §1.4; set `status` on the fact and on the report.
- [ ] `extract/load.py`: upsert into `financial_facts`; create the `financials` view; point the RAG SQL prompt at it.
- [ ] `tests/score_gold.py`: per-metric precision/recall against the gold set. **Run after every prompt or model change.**
- **Done when:** ≥ 95% of the gold metrics are correct, and every wrong value is caught by validation (`needs_review`) rather than loaded silently.

### Phase 4 — ESEF fast path (2 days, if the survey found ESEF packages on the sites)
- [ ] `extract/esef.py`: parse iXBRL (Arelle, or a lighter iXBRL parser). Map IFRS concepts → canonical metrics (`ifrs-full:Revenue`, `ifrs-full:ProfitLoss`, `ifrs-full:Assets`, `ifrs-full:Equity`, `ifrs-full:Liabilities`, cash-flow totals…). Scale comes from the `decimals`/`scale` attributes, and context gives period and consolidation.
- [ ] When an ESEF version of a report exists, it wins. Compare it against the LLM extraction of the same report's PDF: **free extra ground truth to tune the LLM path for quarterly reports, where ESEF doesn't exist.**

### Phase 5 — Website crawler + `financial_report` target (Step 1) (5–8 days)
Build it in this order. Each step is usable on its own and is scored against `tests/gold/sites/`.
- [ ] **5a. Static crawler core:** `crawl/fetch.py` (httpx, robots.txt, rate limit, conditional GET), `crawl/frontier.py` (priority queue, depth/page limits), `crawl/links.py` (link + context extraction, HEAD mime check), `crawl/store.py` → `web_pages`, `web_links`. CLI: `python -m dataPipeline crawl --company 4`.
- [ ] **5b. Sitemap entry:** `crawl/sitemap.py`. Many sites are done after this step alone.
- [ ] **5c. Target interface + `financial_report`:** `targets/base.py` (`keywords()` used by the frontier for scoring, `classify(link)`, `handle(candidate)`); `targets/financial_report.py` with regex heuristics → LLM only below the confidence threshold → `review` below the second threshold → `report_candidates`. Accepted candidates feed Phase 1's downloader, replacing the manual CSV.
- [ ] **5d. Playwright fallback:** detect `needs_js` automatically, render with scrolling and "load more" clicks, and handle tabs or year filters on document libraries.
- [ ] **5e. Incremental recrawl:** `site_entry_points` learning, conditional requests, monthly full crawl. A new link on an entry page = new report.
- [ ] **5f. Fallbacks:** search-engine lookup when the homepage fails; `browser-use` for sites that are still empty; log those sites for custom rules.
- **Done when:** for the surveyed sites, the crawler finds ≥ 95% of the report URLs in `tests/gold/sites/` with no wrong accepts, and a recrawl of an unchanged site takes under 30 seconds.
- **Later targets** (press releases, dividends, AGM…) = a new file in `targets/` with its own keywords, classifier and handler. The crawler and the stored pages are reused as they are, and existing crawls can be re-classified without fetching again.

### Phase 6 — Narrative indexing (Step 5) (separate roadmap)
- Full-document Docling overnight → header-aware chunks with `page_number` → multilingual embeddings → pgvector.
- Reuse it for the statement-locating fallback from Phase 2.

### Phase 7 — Scheduling & review (1–2 days)
- [ ] `python -m dataPipeline run-all`: crawl → classify (all targets) → download → convert → extract, each step picking up documents by `status`.
- [ ] Scheduled run (Windows Task Scheduler or Prefect). Never run Playwright and the LLM at the same time (16 GB budget).
- [ ] Review page in `db_gui.py`: the `needs_review` facts with the PDF page rendered alongside; approve or correct, saved as `method='manual'`.

---

## 5. Where to start this week

1. Do the site survey for 10 companies (Phase 0). It gives you the crawler's test set, the manual URL list and the ESEF answer in one pass.
2. Write `metrics.py` and the migration.
3. Hand-label 8–10 gold reports.
4. Build the Phase 1 downloader from the surveyed URLs.

After that, every later phase can be measured from its first day: extraction against the gold reports, the crawler against the gold sites.
