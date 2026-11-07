# Company website → PDF download pipeline

`python -m dataPipeline.download.run` visits the English website of every company in the `stocks` table, finds the PDFs in its financial-report and announcement sections, and saves them in `reports/`. Every file and every failure is recorded in PostgreSQL.

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
python -m dataPipeline.download.run --skip-by-name           # only download file names not saved before
python -m dataPipeline.download.run --recrawl                # ignore the saved crawl, visit the sites again
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
| `--recrawl` | off | Crawl again even if a recent crawl is saved (see 8) |
| `--max-age-days N` | 7 | Reuse a saved crawl younger than N days; `0` always crawls |
| `--skip-by-name` | off | Also skip a PDF whose file name was already saved for this company, even under a different URL (see 8) |
| `--dry-run` | off | List what would be downloaded; write nothing |
| `--force` | off | Download again even if the URL is recorded and the file exists |

Environment variables: `DATABASE_URL` (default `postgresql+psycopg2://username:postgres@localhost:5432/postgres`, can be in `.env`), `CRAWLER_DELAY` (seconds between requests to one host, default `1.0`), `CRAWLER_USER_AGENT`, `PLAYWRIGHT_CHROMIUM_PATH` (default `/opt/pw-browsers/chromium`).

Requirements:

- PostgreSQL reachable through `DATABASE_URL`, with the `stocks` table filled and `stocks.website_url` set for the companies you want.
- Python packages: `pip install -r requirements.txt` (repo root; the download step uses `httpx`, `beautifulsoup4`, `playwright`, `cryptography`, `SQLAlchemy`, `psycopg2-binary`, `python-dotenv`).
- A Chromium for Playwright (`python -m playwright install chromium`), only needed for sites that require JavaScript.

Output:

- `reports/{company-slug}/{reports|announcements|other}/*.pdf`. `reports/` is gitignored.
- Rows in `downloaded_files` and `download_errors`, and the result of each crawl in `crawl_runs` and `discovered_pdfs`.
- **A copy of everything printed** in `logs/download_<date>_<time>.log` (one file per run, gitignored). The first line of the file is the exact command; the last is the finish time.
- Exit code `0`, also when some downloads failed (look in `download_errors`); `1` only if no company matched.

A typical log, with what each line means:

```
[log] saving output to .../logs/download_2026-11-07_11-30-00.log    # helpers/run_log.py
[1/1] Triglavska Zavarovalnica d.d. - https://www.triglav.eu        # run.py: one line per company
    ! [crawl] http://ar2013.triglav.eu/ FetchError: ConnectError ... # a failure, also saved in download_errors
    English site: https://www.triglav.eu/en (language_switcher); 118 pages; 591 PDF links
    # a rerun within 7 days prints instead:  English site: ... 591 PDF links from the saved crawl of 2026-11-07 11:30 (0.2 days old; --recrawl to refresh)
      + triglavska-zavarovalnica-d-d/reports/2024_..._8ead0c46.pdf   # a new file was saved
    found: 590, downloaded: 103, skipped: 455, same_name: 0, duplicates: 31, failed: 1
Done in 1002s - found: 590, downloaded: 103, skipped: 455, same_name: 0, duplicates: 31, failed: 1
Failures are in the download_errors table.
```

- `English site: <url> (<how>)`: `how` is `hreflang`, `html_lang`, `language_switcher`, `path_probe` or `not_found`. `118 pages` is how many pages were fetched; `591 PDF links` how many `.pdf` links were seen before filtering.
- `found`: PDFs that passed `wanted()`. `downloaded`: new files. `skipped`: the URL was recorded and the file exists. `same_name`: skipped by `--skip-by-name` (0 without the flag). `duplicates`: the bytes already existed under another URL (nothing new saved). `failed`: the download or the database write failed.

---

## 4. Database

The tables are created every time `run.py` starts (`CREATE TABLE IF NOT EXISTS`, in `db.DDL` and in `schema.sql`). Companies are the rows of `stocks`; there is no separate company table.

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

### `crawl_runs` and `discovered_pdfs`: the saved crawl

`crawl_runs` has one row per company, the latest crawl: `crawled_at`, the `website_url` it was made for, the English `base_url` and how it was found (`english`), `pages_visited`, `used_js`, `pdf_links`.
`discovered_pdfs` has one row per PDF link that crawl found, **before** any filtering: `source_url`, `link_text`, `context`, `page_url`, `page_title`, `page_relevance`, `first_seen`, `last_seen`. `last_seen` equals the crawl's `crawled_at` for every link the latest crawl saw; a link a newer crawl no longer finds keeps its older `last_seen` and is not used.

Because the links are stored unfiltered, a change to the keyword lists (`wanted`, `categorise`) takes effect on the next run without crawling again.

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
3. For each company (`process_company`): crawl; keep `wanted` PDFs; for each, compute its category; skip if recorded with the file present (unless `--force`); with `--skip-by-name` also skip if the file name is already known (below); otherwise download and write the `downloaded_files` row.
**Saved crawl.** Crawling is the slow part (one request per second per page). After a successful crawl `run.py` saves its result (`db.save_crawl`). A later run uses it instead of visiting the site (`db.load_crawl`) if **all** of these hold:

- `--recrawl` is not given and `--max-age-days` is above 0,
- the crawl is younger than `--max-age-days` (default 7),
- `stocks.website_url` is unchanged since that crawl,
- that crawl found at least one PDF (an empty crawl is probably a failure and is repeated).

The run then prints `from the saved crawl of <date> (<n> days old)` and sends no page requests at all; only new PDFs are downloaded. A `--dry-run` reads a saved crawl but never writes one. The trade-off is freshness: a report published after the saved crawl is not seen until the crawl is older than `--max-age-days` or you pass `--recrawl`. The errors of the original crawl (dead pages, `robots.txt`) are not repeated on later runs.

**What counts as "already downloaded".** The crawl always looks at the whole website. Then, per PDF:

| Check | When | Compares | Cost when it matches |
|---|---|---|---|
| Same URL | always | the full `source_url` (query included) in `downloaded_files`, file still on disk | no request |
| Same file name (`--skip-by-name`) | only with the flag | the last part of the URL path, query ignored, lower case, `+`/`%20` read as spaces (`name_key` in `downloader.py`) | no request |
| Same content | always, after downloading | SHA-256 | the file is downloaded, then discarded (`duplicates`) |

The flag is for sites whose links change while the documents do not (a cache id or session in the query, a new folder). Without it each such link is downloaded again and only then recognised as a duplicate. It only applies to URLs that end in `.pdf`; a link like `/download?id=5` has no usable name and is always matched by URL. Names are compared with the URLs recorded in `downloaded_files`, so a name counts only if its file is still on disk, and a name seen earlier in the same run counts too.

Trade-off: two different documents with the same file name (`report.pdf` in a 2023 and a 2024 folder) are treated as one, and the second is skipped. Use the flag when you check for new reports on companies you already downloaded; leave it off for a first full download.

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

A second run within a week does not visit the site at all (it uses the saved crawl) and skips every file already on disk; `--recrawl` visits the site again and logs the same two failures again.

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
- `--skip-by-name` trusts the file name only: different documents with the same name are skipped, and a document re-published under the same name with new content is not downloaded again (use `--force`).
- Slow by design: one request per second per host and one company at a time. A big site takes tens of minutes.
- A saved crawl can be up to `--max-age-days` old, so a newly published report may be missed until `--recrawl` or the age limit. The saved crawl is per company and ignores `--max-pages`, `--max-depth` and `--js`: after changing those, add `--recrawl`.
- `download_errors` grows with every run and is never cleaned.
- Companies without a `website_url` are only logged as errors.
- No locking: two simultaneous runs could both download the same file. The unique key keeps `downloaded_files` consistent, and the file name contains the hash, so the result is the same file.
