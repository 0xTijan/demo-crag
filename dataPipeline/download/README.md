# Company website → PDF download pipeline

`python -m dataPipeline.download.run` visits the English website of every company in the `stocks` table, finds the PDFs in its financial-report and announcement sections, and saves them in `reports/`. Every file and every failure is recorded in PostgreSQL.

**Datasheets of component manufacturers** are downloaded by a second crawler in the same folder, `python -m dataPipeline.download.parts_run <url>`: see [14. Part crawler](#14-part-crawler-every-datasheet-of-a-manufacturer).

This document explains the pipeline from the first line to the last, so that an engineer who has never seen it can understand what each part does and how the parts connect.

- [1. What it does, in one picture](#1-what-it-does-in-one-picture)
- [2. Files and who calls whom](#2-files-and-who-calls-whom)
- [3. Running it](#3-running-it)
- [4. Database](#4-database)
- [5. `fetcher.py`: all network access](#5-fetcherpy-all-network-access)
- [6. `crawler.py`: from homepage to PDF links](#6-crawlerpy-from-homepage-to-pdf-links)
- [7. `downloader.py`: one PDF to disk](#7-downloaderpy-one-pdf-to-disk)
- [8. `run.py`: the command-line loop](#8-runpy-the-command-line-loop)
- [9. Worked example](#9-worked-example)
- [10. Failure handling](#10-failure-handling)
- [11. Every threshold in one place](#11-every-threshold-in-one-place)
- [12. Tests](#12-tests)
- [13. Extending and known limits](#13-extending-and-known-limits)
- [14. Part crawler: every datasheet of a manufacturer](#14-part-crawler-every-datasheet-of-a-manufacturer)

---

## 1. What it does, in one picture

```
stocks(isin, name, website_url)
     │
     ▼
┌──────────────────────────── run.py ────────────────────────────────┐
│  for each company:                                                  │
│                                                                     │
│   ┌──────────────── crawler.py : SiteCrawler.crawl ─────────────┐   │
│   │ 1. find_english_base: hreflang → <html lang> → language      │   │
│   │      switcher → /en/ probes (else: default language)         │   │
│   │ 2. focused crawl of the company's own domain, best-scoring   │   │
│   │      links first (investor / reports / announcements)        │   │
│   │      static HTML first, Playwright browser if the page       │   │
│   │      needs JavaScript                                        │   │
│   │ 3. collect every PDF link with its text and surrounding row  │   │
│   └──────────────────────────────────────────────────────────────┘   │
│          │ {url: PdfLink(text, context, page, ...)}                  │
│          ▼                                                          │
│   wanted(pdf)? ──► categorise(pdf): reports | announcements | other │
│          │                                                          │
│          ▼                                                          │
│   already downloaded (same URL, file still on disk)? ──► skip       │
│          │ no                                                       │
│          ▼                                                          │
│   downloader.py : stream → check "%PDF-" → SHA-256                  │
│       same bytes already saved for this company? ──► no new file    │
│          │                                                          │
│          ▼                                                          │
│   reports/{company}/{category}/{year}_{title}_{hash8}.pdf           │
│   ──► row in downloaded_files                                       │
│   any failure ──► row in download_errors (the run goes on)          │
└─────────────────────────────────────────────────────────────────────┘
```

**Why a keyword crawl and no LLM?** Investor pages follow a few very regular patterns (a page called "Investors", a list called "Financial reports", links with a year in the text). Scoring links against keyword lists in English, Slovenian and Croatian is fast, free and easy to debug: every decision is one line in `settings.py`. The price is that categories and years are guesses from link text (see [13](#13-extending-and-known-limits)).

---

## 2. Files and who calls whom

| File | Role | Calls |
|---|---|---|
| `run.py` | The script you run. Loops over companies, applies `wanted` and `categorise`, downloads, records results. | `db`, `crawler`, `downloader`, `fetcher`, `helpers.run_log` |
| `db.py` | Reads companies from `stocks`. Creates and writes `downloaded_files` and `download_errors`. | `settings` |
| `fetcher.py` | All network access: rate limit per host, `robots.txt`, retries, HEAD checks, the Playwright browser, repair of incomplete TLS chains. | `settings` |
| `crawler.py` | Finds the English site, runs the focused crawl, decides which PDFs to keep and their category. Knows nothing about disks or databases. | `fetcher`, `settings` |
| `downloader.py` | Streams one PDF to disk, verifies it, removes duplicates, names the file. | `fetcher`, `crawler.PdfLink` |
| `parts_run.py` | Part crawler: the script you run for manufacturers' datasheets ([14](#14-part-crawler-every-datasheet-of-a-manufacturer)). | `parts_crawler`, `downloader`, `fetcher`, `db` |
| `parts_crawler.py` | Walks a product catalogue (static links, or clicking in a browser) and reports every PDF. | `fetcher`, `crawler` (URL helpers), `settings` |
| `settings.py` | Constants, limits and all keyword lists. | none |

```
run.py ──► crawler.SiteCrawler.crawl ──► fetcher.get_page / render / is_pdf
   │              │
   │              └── wanted / categorise (pure functions on PdfLink)
   │
   ├──► downloader.download ──► fetcher.stream
   └──► db.record_download / db.ErrorLog
```

The key design point: **the crawler returns data and writes nothing.** `crawl()` returns a `CrawlResult` (a dict of `PdfLink`), and failures are reported through a `log_error` callback. That is why the same crawler works with `--dry-run`: `run.py` simply skips the download and database steps.

---

## 3. Running it

```bash
python -m dataPipeline.download.run                          # every company in stocks
python -m dataPipeline.download.run --isin SI0031890831      # one company
python -m dataPipeline.download.run --isin SI0031890831 SI0031104332   # several
python -m dataPipeline.download.run --dry-run                # list PDFs, download nothing
python -m dataPipeline.download.run --force                  # download again even if recorded
```

Run it from the repository root (the `-m` form needs the root on the path).

| Option | Default | Meaning |
|---|---|---|
| `--isin ISIN [ISIN ...]` | all | Only these companies |
| `--limit N` | none | At most N companies |
| `--max-pages N` | 150 | Pages crawled per site |
| `--max-depth N` | 3 | Link depth from the English homepage |
| `--js auto\|always\|never` | `auto` | Render pages with Playwright: decide per site, always, or never |
| `--dry-run` | off | List what would be downloaded; write nothing |
| `--force` | off | Download again even if the URL is recorded and the file exists |

Environment variables: `DATABASE_URL` (default `postgresql+psycopg2://username:postgres@localhost:5432/postgres`, can be in `.env`), `CRAWLER_DELAY` (seconds between requests to one host, default `1.0`), `CRAWLER_USER_AGENT`, `PLAYWRIGHT_CHROMIUM_PATH` (default `/opt/pw-browsers/chromium`).

Requirements:

- PostgreSQL reachable through `DATABASE_URL`, with the `stocks` table filled and `stocks.website_url` set for the companies you want.
- Python packages: `pip install -r requirements.txt` (repo root; the download step uses `httpx`, `beautifulsoup4`, `playwright`, `cryptography`, `SQLAlchemy`, `psycopg2-binary`, `python-dotenv`).
- A Chromium for Playwright (`python -m playwright install chromium`), only needed for sites that require JavaScript.

Output:

- `reports/{company-slug}/{reports|announcements|other}/*.pdf`. `reports/` is gitignored.
- Rows in `downloaded_files` and `download_errors`.
- **A copy of everything printed** in `logs/download_<date>_<time>.log` (one file per run, gitignored). The first line of the file is the exact command; the last is the finish time.
- Exit code `0`, also when some downloads failed (look in `download_errors`); `1` only if no company matched.

A typical log, with what each line means:

```
[log] saving output to .../logs/download_2026-11-07_11-30-00.log    # helpers/run_log.py
[1/1] Triglavska Zavarovalnica d.d. - https://www.triglav.eu        # run.py: one line per company
    ! [crawl] http://ar2013.triglav.eu/ FetchError: ConnectError ... # a failure, also saved in download_errors
    English site: https://www.triglav.eu/en (language_switcher); 118 pages; 591 PDF links
      + triglavska-zavarovalnica-d-d/reports/2024_..._8ead0c46.pdf   # a new file was saved
    found: 590, downloaded: 103, skipped: 455, duplicates: 31, failed: 1
Done in 1002s - found: 590, downloaded: 103, skipped: 455, duplicates: 31, failed: 1
Failures are in the download_errors table.
```

- `English site: <url> (<how>)`: `how` is `hreflang`, `html_lang`, `language_switcher`, `path_probe` or `not_found`. `118 pages` is how many pages were fetched; `591 PDF links` how many `.pdf` links were seen before filtering.
- `found`: PDFs that passed `wanted()`. `downloaded`: new files. `skipped`: the URL was recorded and the file exists. `duplicates`: the bytes already existed under another URL (nothing new saved). `failed`: the download or the database write failed.

---

## 4. Database

Both tables are created every time `run.py` starts (`CREATE TABLE IF NOT EXISTS`, in `db.DDL` and in `schema.sql`). Companies are the rows of `stocks`; there is no separate company table.

### `downloaded_files`: one row per (company, source URL)

| Column | Meaning |
|---|---|
| `download_id` | Serial primary key |
| `stock_isin` | The company, `REFERENCES stocks(isin)` |
| `category` | `reports`, `announcements` or `other` |
| `source_url` | The PDF URL. `UNIQUE (stock_isin, source_url)`: a rerun updates the row instead of adding one |
| `page_url`, `link_text` | The page the link was found on, and the link's text |
| `file_path` | Relative to `reports/`. Two URLs with identical bytes share one `file_path` |
| `file_hash`, `file_size` | SHA-256 of the bytes, and size in bytes |
| `fiscal_year` | Guessed from link text, URL, or surrounding row (see [7](#7-downloaderpy-one-pdf-to-disk)) |
| `downloaded_at` | When the row was written |

### `download_errors`: append-only failure log

One row per failure, never updated: `stock_isin` (may be `NULL`), `url`, `stage`, `error`, `http_status`, `traceback`, `occurred_at`.

`stage` says where it failed:

| `stage` | Meaning |
|---|---|
| `website` | The company has no `website_url`, or its homepage could not be fetched |
| `english_version` | No English version found; the default language was crawled instead |
| `robots` | A page was skipped because `robots.txt` disallows it |
| `crawl` | A page could not be fetched (HTTP error, DNS, timeout) |
| `render` | The Playwright browser failed on a page |
| `head_check` | The content-type check of an extension-less link failed |
| `download` | The PDF request failed (HTTP error, connection dropped, file too large) |
| `verify` | The response was not a PDF |
| `write` | Moving the file into place failed |
| `db` | Saving the `downloaded_files` row failed |
| `company` | An unexpected error ended this company's run |

```sql
SELECT stage, count(*) FROM download_errors GROUP BY stage;                       -- what is failing
SELECT url, error FROM download_errors WHERE stock_isin = 'SI0031890831'
ORDER BY occurred_at DESC;                                                        -- one company
```

Errors accumulate: every run adds rows for the same dead links. Filter by `occurred_at` to see one run.

---

## 5. `fetcher.py`: all network access

One `Fetcher` object per run. Everything that touches the network goes through it.

### 5.1 Politeness

- `_wait(url)` sleeps so that two requests to the same host are at least `REQUEST_DELAY` (1 s) apart. The timer is per host, so different websites do not slow each other down.
- `allowed(url)` reads `robots.txt` once per origin and answers `can_fetch`. If `robots.txt` cannot be read, everything is allowed.
- `_request` retries up to 3 times with 2, 4, 8 s backoff on connection errors and on HTTP 429/500/502/503/504. Other statuses (404, 403) are returned at once.

### 5.2 Requests

| Method | Does |
|---|---|
| `get_page(url)` | GET; reads the body only if it is HTML (max 10 MB). Raises `FetchError` for HTTP ≥ 400. |
| `is_pdf(url)` | HEAD; if the server rejects HEAD (403/404/405/501) or sends no content type, a streamed GET. True if the content-type contains `pdf` or `Content-Disposition` names a `.pdf`. |
| `stream(url)` | Streamed GET for the downloader. |
| `render(url)` | Playwright, see 5.3. |

### 5.3 Browser rendering (`render`)

Used for sites whose links are built by JavaScript. In order: open the page (`domcontentloaded`), wait up to 8 s for the network to go quiet, click a cookie-accept button if one exists, scroll down 6 times, then click any "load more / show more / view all" button up to 8 times (stopping if the URL changes). Returns the final HTML. The browser starts on first use, so static sites never launch it. If Playwright's own browser is missing, it falls back to `PLAYWRIGHT_CHROMIUM_PATH` with `--no-sandbox`.

### 5.4 Incomplete TLS certificate chains

Some servers send only their own certificate and leave out the intermediate CA. Browsers fetch the missing piece themselves; `httpx` does not and fails with `CERTIFICATE_VERIFY_FAILED`. `triglav.eu` is such a site.

`_repair_chain(host)` runs once per host after that failure: it reads the server's certificate (through `HTTPS_PROXY` if set), follows its *CA Issuers* link (up to 4 levels, stopping at the root), adds the certificates it gets to the trusted set, rebuilds the HTTP client, and the request is retried. **Verification stays on**; the fetcher only learns an intermediate it was missing. If the repair fails, the original TLS error is reported.

---

## 6. `crawler.py`: from homepage to PDF links

### 6.1 `find_english_base(fetcher, website_url)`

Returns `(url, how, page)`. Tries, in this order, and stops at the first that works:

1. **`hreflang`**: `<link rel="alternate" hreflang="en" href=...>` on the same site.
2. **`html_lang`**: the page itself says `<html lang="en...">` and is not under a non-English path prefix.
3. **`language_switcher`**: a link whose text is `EN`/`English`/..., whose `hreflang` is English, or whose first path segment is `en`/`eng`/....
4. **`path_probe`**: tries `/en/`, `/en`, `/eng/`, `/en-gb/`, `/english/`.
5. **`not_found`**: crawl the default language and log an `english_version` error.

A bare domain in the database (`example.com`) is tried with `https://`, then `http://`.

### 6.2 `SiteCrawler.crawl` and `_crawl`

The crawl is a priority queue (`heapq`) on the company's own domain. For every fetched page:

1. `page_relevance`: keyword score of its URL, title and first headings.
2. Every link is looked at:
   - ends in `.pdf` → collected as a `PdfLink` (text, the table row or list item around it, the nearest heading, the page, the page's relevance);
   - other file types (images, Office files, ...) → ignored;
   - a link to another language (`/sl/`, `/hr/`, `?lang=de`, ...) → ignored;
   - a link on the same site is **followed** if its URL/text scores above 0, or if the current page is relevant and the link looks like pagination or a year (`?page=2`, `2019`, `next`). It must also stay under `/en/` when the English site lives there, and within `--max-depth`;
   - an `<iframe>` is always followed and its domain trusted (hosted investor-relations widgets);
   - a link without extension whose path contains `download`, `file`, `document`, ... is remembered and checked by content type after the crawl (`is_pdf`, at most 200 per site).
3. Priority = link score + 2 if the page is relevant + 5 for iframes. Best first, so the most promising pages are fetched before the page limit is reached.

Scores (`keyword_score` in `settings.py`): a keyword with a space matches as a phrase (`annual report`); a keyword of 3 characters or less must match a whole word (`ir`, `q1`); a longer keyword matches any word starting with it, so `report` matches `reports` and `porocil` matches `porocila`. Negative keywords (careers, products, privacy, cookies, catalogue, ...) subtract.

**Static or browser?** `crawl` first looks at the homepage: fewer than 10 links or less than 300 characters of text means the page is built by JavaScript, and the whole crawl uses Playwright. If a static crawl finds no PDFs at all, it is repeated once with the browser. `--js always` / `--js never` override this.

### 6.3 Choosing and sorting PDFs

- `wanted(pdf)`: keep a PDF if its own URL/text/row matches report, announcement or navigation keywords more than it matches negative ones; or if it was found on a relevant page and has no negative match.
- `categorise(pdf)`: a keyword vote on the URL path, link text and surrounding row; a tie is decided by the page it was found on. More report words → `reports`; more announcement words → `announcements`; no clear winner and no report words → `other`.

---

## 7. `downloader.py`: one PDF to disk

`download(fetcher, pdf, target_dir, known_hashes, reports_dir)`:

1. Stream the response into a hidden `.{sha1 of url}.part` file while hashing it (SHA-256). Abort above 300 MB or if the content-type is HTML.
2. Check the first 1024 bytes contain `%PDF-`. A server that says "PDF" but sends an error page fails here (`verify`).
3. If `known_hashes` already holds this SHA-256 for the company **and that file still exists**, delete the temporary file and return the existing path (`duplicate_of` is set). The same report linked twice is stored once.
4. Otherwise name it `{year or undated}_{title-slug}_{hash8}.pdf` and move it into place (atomic rename, so a half-written file never has the final name).

**Title** = the link text, unless it is generic ("Download", "PDF", "here", ...); then the filename in `Content-Disposition`; then the last part of the URL.
**Year** = the first 4-digit year (1980–2099) in the link text, then the URL path, then the surrounding row. It is a guess; see the limits in [13](#13-extending-and-known-limits).

The temporary file is always removed, also on errors. Failures raise `DownloadError(stage, message, status)` and `run.py` logs them.

---

## 8. `run.py`: the command-line loop

1. Parse arguments and start the run log (`helpers/run_log.py` → `logs/download_<time>.log`).
2. Connect, create the two tables, load the companies (`db.load_companies`, filtered by `--isin`/`--limit`).
3. For each company (`process_company`): crawl; keep `wanted` PDFs; for each, compute its category; skip if recorded with the file present (unless `--force`); otherwise download and write the `downloaded_files` row.
4. One bad company never stops the run: an unexpected exception is stored as stage `company` and the next company starts.
5. Close the browser and HTTP client, print the totals.

With `--dry-run` step 3 only prints `[category] 'link text' url` for each PDF.

---

## 9. Worked example

**A real site: `triglav.eu`** (the `stocks.website_url` of Triglavska Zavarovalnica was set to `https://www.triglav.eu`).

| Step | What happened |
|---|---|
| Homepage | `https://www.triglav.eu/` redirects to the Slovenian `/sl/`. The first request failed with `CERTIFICATE_VERIFY_FAILED`: the server omits its intermediate certificate. `_repair_chain` fetched the DigiCert intermediate and the retry worked. |
| English site | No `hreflang`; `<html lang>` is Slovenian; the language switcher has an "EN" link → `https://www.triglav.eu/en` (`language_switcher`). |
| Crawl | 118 pages, 591 PDF links, about 4 minutes. Raising `--max-pages` to 500 and `--max-depth` to 5 found the identical set, so the crawl had run out of pages, not hit a limit. |
| Links | Most PDFs are at `/wps/wcm/connect/<uuid>/<name>.pdf?...` and their link text is just "DOWNLOAD"; the real title is in the file name and the table row. |
| Selection | 590 PDFs passed `wanted()`: 239 `reports`, 224 `announcements`, 126 `other` (corporate governance, fact sheets); some PDFs appear under several URLs. |
| Download | 526 distinct files, 908 MB. 31 URLs delivered bytes that already existed (counted as `duplicates`, nothing new saved). One URL answered 404 (`download_errors`, stage `download`). Four `*.triglav.eu` annual-report microsites no longer exist (stage `crawl`). About 45 minutes in total, mostly the 1 s per-request pause and file transfer. |

**A small static site (the local test).** Company pages: `/` (Slovenian) with `<link hreflang="en" href="/en/">`, `/en/investors/financial-reports/`, `/en/investors/announcements/`, `/en/careers/`, `/en/products/`.

1. `find_english_base` follows the `hreflang` link to `/en/`.
2. `/en/investors/` scores high ("investor"), so it is crawled first; `/en/careers/` and `/en/products/` score negative and are never opened; `/sl/` is another language.
3. On the reports page the row `Half-year report 2024 | PDF` links to `/download?id=5` (no extension). A HEAD request returns `application/pdf`, so it is kept; the file name `HY_Report_2024.pdf` comes from `Content-Disposition`.
4. The same report under `/files/mirror/AR_2023_EN.pdf` has identical bytes: one file on disk, two rows in `downloaded_files`.
5. A link the server answers with 404 and one that serves HTML with a `.pdf` name end up in `download_errors` (stages `download` and `verify`).

A second run skips every file already on disk and logs only the same two failures again.

---

## 10. Failure handling

| What fails | Where | Effect |
|---|---|---|
| Company has no `website_url` | `website` | Error row. Next company. |
| Homepage cannot be fetched | `website` | Error row. Next company. |
| No English version found | `english_version` | Error row. The default language is crawled anyway. |
| Page blocked by `robots.txt` | `robots` | Error row. The page is skipped, the crawl continues. |
| One page fails (404, DNS, timeout) | `crawl` / `render` | Error row. That page is skipped. |
| Browser cannot render a direct file download | `render` | Falls back to a plain HTTP content-type check. |
| Incomplete TLS chain | `fetcher` | One repair attempt per host; if it fails the TLS error is reported. |
| PDF request fails or exceeds 300 MB | `download` | Error row. Temporary file removed. |
| Response is not a PDF | `verify` | Error row. Temporary file removed. |
| Moving the file fails | `write` | Error row. |
| Saving the database row fails | `db` | Error row. The file stays on disk and is recorded on the next run. |
| Anything unexpected inside one company | `company` | Error row with traceback. The run continues. |
| Database unavailable at start | `run.py` | The run stops with the connection error. |
| Writing the error row itself fails | `ErrorLog` | Printed to stderr; never raises. |

Failed downloads are retried automatically on the next run, because only recorded URLs whose file exists are skipped.

---

## 11. Every threshold in one place

| Name | Value | File | Meaning |
|---|---|---|---|
| `REQUEST_DELAY` | 1.0 s (`CRAWLER_DELAY`) | `settings.py` | Minimum gap between requests to one host |
| `REQUEST_TIMEOUT` | 30 s | `settings.py` | Per request |
| `MAX_RETRIES` | 3 (backoff 2, 4, 8 s) | `settings.py` | Connection errors and HTTP 429/5xx |
| `MAX_PDF_BYTES` | 300 MB | `settings.py` | Larger files are not saved |
| `MAX_HTML_BYTES` | 10 MB | `settings.py` | Larger pages are not parsed |
| `MAX_HEAD_CHECKS` | 200 per site | `settings.py` | Extension-less links tested for `application/pdf` |
| `--max-pages` | 150 | `run.py` | Pages crawled per site |
| `--max-depth` | 3 | `run.py` | Link depth from the English homepage |
| `_needs_js` | < 10 links or < 300 text characters | `crawler.py` | Homepage looks JavaScript-built → use the browser |
| scrolls / "load more" clicks | 6 / 8 | `fetcher.py` | Browser rendering per page |
| network-idle wait | 8 s | `fetcher.py` | Browser rendering per page |
| TLS chain depth | 4 levels | `fetcher.py` | Intermediates fetched while repairing a chain |
| PDF signature window | first 1024 bytes | `downloader.py` | Must contain `%PDF-` |
| download chunk | 64 KB | `downloader.py` | Streaming read size |
| title slug | 80 characters | `downloader.py` | File name length |
| link context | 400 characters | `crawler.py` | Row/list text kept around a link |
| link priority | score + 2 (relevant page) + 5 (iframe) | `crawler.py` | Order of the crawl queue |
| keyword lists | `NAV_`, `REPORT_`, `ANNOUNCEMENT_`, `NEGATIVE_KEYWORDS` | `settings.py` | What counts as relevant |

---

## 12. Tests

There are no automated tests for this pipeline yet. It was checked by hand against:

- two local fake sites (a static one with an `hreflang` English section, a `robots.txt` rule, a 404 link, a fake PDF, an extension-less download link, a duplicate file; and one whose links are built by JavaScript behind a "Load more" button), with a second run to confirm that nothing is downloaded twice;
- the real `triglav.eu` (see [9](#9-worked-example)).

The quickest manual check of any change is a dry run on one company: `python -m dataPipeline.download.run --isin <ISIN> --dry-run` prints every PDF with its category and does not write anything except the log file.

When a PDF is missed or wrongly kept, find out which rule decided: the page may never have been queued (`link_score` was 0, or it was outside `/en/`), the link may have been dropped by `wanted()`, or the category vote went the other way. All three use the lists in `settings.py`.

---

## 13. Extending and known limits

**Adding a keyword or a language.** Add the word (or stem) to `NAV_KEYWORDS`, `REPORT_KEYWORDS`, `ANNOUNCEMENT_KEYWORDS` or `NEGATIVE_KEYWORDS`. To skip another language, add its path prefix to `OTHER_LANG_SEGMENTS`.

**Known limits**

- Everything on the investor pages is saved, not only financial statements. Investor presentations, governance statements and fact sheets arrive as `reports` or `other`.
- `fiscal_year` and the category are guessed from link text and can be wrong. For example, a number in a URL can produce an impossible year such as 2062. Confirm both from the PDF itself in the conversion step.
- Pages that need a dropdown, tab or year filter to be clicked are not handled; only scrolling and "load more" buttons are automated.
- Sites on other domains are followed only through iframes. Direct `.pdf` links to other domains are downloaded.
- `robots.txt` is checked for crawled pages, not for PDF downloads.
- Documents that exist only on old sites that no longer resolve cannot be fetched.
- Slow by design: one request per second per host and one company at a time. A big site takes tens of minutes.
- Every run crawls again before downloading, even when all files are already on disk.
- `download_errors` grows with every run and is never cleaned.
- Companies without a `website_url` are only logged as errors.
- No locking: two simultaneous runs could both download the same file. The unique key keeps `downloaded_files` consistent, and the file name contains the hash, so the result is the same file.

---

## 14. Part crawler: every datasheet of a manufacturer

`parts_run.py` + `parts_crawler.py` download **every datasheet PDF** from a component manufacturer's website. You give the product page (or just the homepage); the crawler walks every product category, family, series and part page, the way a person would click through the catalogue, and saves each datasheet once. Nothing in the code knows a particular website: the two sites below are only the examples it was built and checked against.

```bash
python -m dataPipeline.download.parts_run https://en.goodark.com/product.php
python -m dataPipeline.download.parts_run https://www.novosns.com/en/ https://en.goodark.com/product.php
python -m dataPipeline.download.parts_run --file manufacturers.txt        # one URL per line, # comments
python -m dataPipeline.download.parts_run https://www.novosns.com/en/ --dry-run -v   # list, download nothing
python -m dataPipeline.download.parts_run https://en.goodark.com/product.php --no-db   # no PostgreSQL
```

| Option | Default | Meaning |
|---|---|---|
| `--file PATH` | none | More URLs, one per line |
| `--name NAME` | from the domain (`goodark`, `novosns`) | Manufacturer name: folder and database key (one URL only) |
| `--max-pages N` | 20000 | Pages (static or rendered) per manufacturer |
| `--max-depth N` | 10 | Links/clicks from the start URL |
| `--js auto\|always\|never` | `auto` | Clicking browser: decide per site, always, or never |
| `--all-pdfs` | off | Keep every PDF of the catalogue, not only datasheets |
| `--dry-run` | off | Print the datasheets found; download and write nothing (no database needed) |
| `--force` | off | Download again even if already saved |
| `--no-db` | off | Only `manifest.jsonl`, no PostgreSQL |
| `-v` | off | Print every page and every click |

Output:

- `datasheets/{manufacturer}/{name-of-the-pdf}_{hash8}.pdf` (`datasheets/` is gitignored). The name is the server's file name (`Content-Disposition`) or the URL's last part, so `MT6835_Rev.1.3.pdf` stays recognisable.
- `datasheets/{manufacturer}/manifest.jsonl`: one JSON line per source URL: `source_url`, `page_url` (where it was found), `link_text`, `context` (the table row, usually the part number), `trail` (links/clicks from the start page, e.g. `Sensor > Magnetic Sensor > ABS Speed Sensor > NSM4101-Q0TOMR > Datasheet`), `file_path`, `file_hash`, `file_size`.
- With a database: the same rows in `datasheet_files`, failures in `datasheet_errors` (both created by `parts_run.py`, also in `schema.sql`). Files saved by a `--no-db` run get their row on the next run with a database.
- Logs: `logs/datasheets_<date>_<time>.log`.

Every file is saved as soon as it is found, so a crawl stopped half-way keeps what it has. A rerun skips URLs whose file exists; identical bytes under another URL (many parts share one datasheet) are stored once and recorded for every URL (`duplicates`).

### 14.1 How the catalogue is walked

```
start URL ──► static HTML usable? (≥ 10 links, ≥ 300 characters of text)
                 │ yes                                   │ no (JavaScript app)
                 ▼                                       ▼
        follow <a href> links                  Chromium: render, close pop-ups,
        (families, pagination,                 click every clickable element in
         part pages) and collect               the page and its iframes, page through
         every .pdf link                       JavaScript tables with their "next" button
                 │                                       │
                 └──────────► every PDF ──► is_datasheet? ──► download (downloader.py)
```

Breadth-first: categories first, then families, then part pages. Pages are only on the start URL's host (and hosts of iframes the catalogue embeds), so other language subdomains (`www.goodark.com` is Chinese) are not crawled.

**Which links/elements are the catalogue** (`PartCrawler._decide`):

- The **start URL** is the catalogue, unless it is the site root (`/`, `/en/`, `/index.php`): then it is only a way in, and only items about products are followed from it.
- An item is **about products** when its own text, or the **section label** it sits under, matches `PRODUCT_KEYWORDS` (`product`, `catalog`, `series`, `family`, `selection guide`, `产品`, ...). The section label is the nearest short text before the item that is not another item of the same kind: for the footer entry "Sensor" it is the column title "Products"; for "Automotive" it is "Applications" (not followed).
- **Inside the catalogue**, everything in the page content is followed (family links, part numbers, pagination, "Datasheet" buttons). Header, footer and navigation bars (`<header>`, `<footer>`, `<nav>`, or classes such as `header2`, `foot`, `nav-bg` near `<body>`) only when the item's own text is about products, so a "Support" or "Careers" menu entry on a part page is not followed.
- **Never**: `PART_NEGATIVE_KEYWORDS` in the item text or in the target URL path (news, careers, about, contact, investors, login, cart, buy, sample requests, cross-reference search, document libraries, ...). They match **whole words** only, so "Pressure Sensor" is not a "press" link and "Sampling ADC" is not a "sample" button. Controls that never navigate are skipped by `CLICK_SKIP_RE` (reset, export, columns, language names, "10 / page", ...), as are form fields, table headers, tabs and checkboxes.

### 14.2 Clicking (JavaScript sites)

Some catalogues have no links at all: menus, category tiles, table rows and "Datasheet" buttons are `<div>`s with click handlers, often inside an iframe. For each page the crawler:

1. Opens it in Chromium, waits until the page stops changing (rows of JavaScript tables arrive after the page has "loaded"), closes language choosers / cookie banners / notices (prefers an "English" button).
2. Lists **clickable elements** in every frame: elements whose cursor is a pointer, buttons, `role=button`, `onclick`. Icons without text are clicked only when their `alt`/`title`/`aria-label` says document (`dataSheet`, `pdf`, `download`, ...).
3. Pages through JavaScript tables/lists: presses the enabled "next" button of a pagination bar until the rows stop changing, recording the elements of every page.
4. Clicks each element (document buttons first, visible before hidden). A click may open a **new tab** or start a **download** (its URL: a PDF if it ends in `.pdf` or the server says `application/pdf`), **change the URL** (a new page for the queue; the page is reloaded, and re-paged, before the next click), or **change the page in place** (new links are collected; a notice such as "datasheet not publicly available" is retried once, then logged as stage `datasheet_unavailable`).
5. Remembers what was clicked: a named item ("Magnetic Sensor", "NSM4101-Q0TOMR") is clicked once per site; a generic one ("Datasheet", an icon) once per table row or page.

### 14.3 Which PDFs are datasheets (`is_datasheet`)

1. The PDF's own URL, link/button text and last click decide first: `DATASHEET_KEYWORDS` (`datasheet`, `specification`, `规格书`, ...) against `OTHER_DOC_KEYWORDS` (application notes and `AN-12-0029`-style numbers, user guides, manuals, reliability reports, certificates, brochures, catalogues, selection/solution guides, package drawings, ...).
2. Otherwise its table row / list item.
3. A PDF with no hint at all is kept if it was found inside the catalogue.

`--all-pdfs` turns the filter off.

### 14.4 The two example sites

| | Good-Ark (`https://en.goodark.com/product.php`) | NOVOSENSE (`https://www.novosns.com/en/`) |
|---|---|---|
| Mode | static HTML | browser + clicking (Nuxt app, no `<a href>`) |
| Path to a datasheet | product page → family `prolist.php?id=551` → pages `&page=2..` → part `proshow.php?id=26552` → `specification/10A05 thru 10A10.pdf` | homepage footer "Products › Sensor" → `/en/sensor` → "Magnetic Sensor" → "ABS Speed Sensor" (table in an iframe from `wwwapi.novosns.com`) → row "NSM4101-Q0TOMR" / its `dataSheet` icon → new tab `/en/datasheet/<part>/<file>.pdf` |
| Particulars | Chinese/Japanese/Korean sites on other subdomains are ignored; one datasheet covers a whole series (`10A05 thru 10A10`), so most part pages repeat a known URL | language chooser pop-up on every page; some parts answer "datasheet is not publicly available" (logged, not an error); all parts of a series share one PDF under different URLs (saved once) |

Network: in a sandboxed environment both sites' hosts must be allowed, for NOVOSENSE also `wwwapi.novosns.com` (the iframe and its data).

### 14.5 Limits

- **Slow on JavaScript sites**: every click that changes the URL means reloading the page before the next click, about 5–10 s each, plus the 1 s per-host pause. A large catalogue takes hours. Static sites are about one page per 1–2 s.
- Filters and search forms are not used; the crawl relies on the category tree and pagination reaching every part. Parts reachable only through a search box are missed.
- Clicks on elements that do nothing (or whose effect is not a URL, tab, download or new link) are not explored further.
- `is_datasheet` uses words, not the PDF content: a datasheet named only by a number on a page outside the catalogue is dropped; a brochure without any keyword found on a catalogue page is kept.
- Login-only datasheets are not fetched (no credentials are used).
- No resume: a stopped crawl starts from the start URL again (already saved files are skipped, not downloaded again).

Tests: `python tests/test_parts_crawler.py` (or pytest) checks the static walk, scope rules, click decisions and datasheet filter on a synthetic site, without network or browser. The clicking path was checked by hand against NOVOSENSE.
