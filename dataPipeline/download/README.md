# Manufacturer website → datasheet PDF download pipeline

`python -m dataPipeline.download.run <url>` downloads **every datasheet PDF** from an electronic-component manufacturer's website. You give the product page (or just the homepage); the crawler walks every product category, family, series and part page, the way a person would click through the catalogue, and saves each datasheet once in `datasheets/`. The database only records file hashes and failures.

Nothing in the code knows a particular website. Two real sites were used as examples while building and checking it (see [9](#9-the-two-example-sites)): Good-Ark (`https://en.goodark.com/product.php`, plain HTML) and NOVOSENSE (`https://www.novosns.com/en/`, a JavaScript app with no links at all).

- [1. What it does, in one picture](#1-what-it-does-in-one-picture)
- [2. Files and who calls whom](#2-files-and-who-calls-whom)
- [3. Running it](#3-running-it)
- [4. How the catalogue is walked](#4-how-the-catalogue-is-walked)
- [5. Clicking (JavaScript sites)](#5-clicking-javascript-sites)
- [6. Which PDFs are datasheets](#6-which-pdfs-are-datasheets)
- [7. Saving: files, manifest, database](#7-saving-files-manifest-database)
- [8. `fetcher.py`: all network access](#8-fetcherpy-all-network-access)
- [9. The two example sites](#9-the-two-example-sites)
- [10. Failure handling](#10-failure-handling)
- [11. Every threshold in one place](#11-every-threshold-in-one-place)
- [12. Tests](#12-tests)
- [13. Known limits](#13-known-limits)

---

## 1. What it does, in one picture

```
manufacturer URL (product page or homepage)
     │
     ▼
┌──────────────────────── crawler.py : PartCrawler.crawl ─────────────────────────┐
│ static HTML usable? (≥ 10 links and ≥ 300 characters of text)                    │
│        │ yes                                     │ no (JavaScript app)           │
│        ▼                                         ▼                               │
│  follow <a href> links of catalogue     Chromium: render, close pop-ups, click   │
│  pages (families, pagination, parts)    every clickable element (also inside    │
│  and collect every .pdf link            iframes), page through JS tables with    │
│                                         their "next" button; a click gives a new │
│                                         URL, a new tab, a download or new links  │
│        └──────────────► every PDF found (url, button text, table row, trail) ────┤
└──────────────────────────────────────────────────────────────┬───────────────────┘
                                                               │ on_pdf, at once
                                                               ▼
run.py : is_datasheet(pdf)? ──no──► skipped (application notes, guides, certificates, ...)
     │ yes
     ▼
same URL already saved (file still on disk)? ──► skip
     │ no
     ▼
downloader.py : stream → check "%PDF-" → SHA-256
     same bytes already saved for this manufacturer? ──► no new file
     │
     ▼
datasheets/{manufacturer}/{pdf-name}_{hash8}.pdf
──► line in datasheets/{manufacturer}/manifest.jsonl (page, part row, click trail)
──► row in datasheet_files (URL, file, SHA-256)
any failure ──► row in datasheet_errors (the run goes on)
```

**Why keywords and clicking, and no site-specific selectors or LLM?** Product catalogues are trees with very regular shapes (a "Products" menu, category tiles, family lists, part tables, a "Datasheet" button). Following everything inside the tree and judging links by a few keyword lists is free, works on unknown sites and is easy to debug: every decision is one line in `settings.py`.

---

## 2. Files and who calls whom

| File | Role | Calls |
|---|---|---|
| `run.py` | The script you run. Loops over manufacturers, filters PDFs with `is_datasheet`, downloads, writes the manifest and the database rows. | `crawler`, `downloader`, `fetcher`, `db`, `helpers.run_log` |
| `crawler.py` | `PartCrawler`: walks one catalogue (static links, or clicking in a browser) and reports every PDF through a callback. `is_datasheet`. Knows nothing about disks or databases. | `fetcher`, `settings` |
| `downloader.py` | Streams one PDF to disk, verifies it, removes duplicates, names the file. | `fetcher` |
| `fetcher.py` | All network access: rate limit per host, `robots.txt`, retries, HEAD checks, repair of incomplete TLS chains, the shared Playwright browser. | `settings` |
| `db.py` | Creates and writes `datasheet_files` and `datasheet_errors`. | `settings` |
| `settings.py` | Constants, limits and all keyword lists. | none |

The crawler returns data and writes nothing: each PDF goes to `on_pdf` as soon as it is found (so a long crawl saves its results as it goes) and failures go to a `log_error` callback. That is why `--dry-run` works with the same crawler.

---

## 3. Running it

Run from the repository root (the `-m` form needs the root on the path).

```bash
python -m dataPipeline.download.run https://en.goodark.com/product.php
python -m dataPipeline.download.run https://www.novosns.com/en/ https://en.goodark.com/product.php
python -m dataPipeline.download.run --file manufacturers.txt          # one URL per line, # comments
python -m dataPipeline.download.run https://www.novosns.com/en/ --dry-run -v   # list, download nothing
python -m dataPipeline.download.run https://en.goodark.com/product.php --no-db # no PostgreSQL
```

| Option | Default | Meaning |
|---|---|---|
| `urls` | | Product page or homepage of each manufacturer |
| `--file PATH` | none | More URLs, one per line |
| `--name NAME` | from the domain (`goodark`, `novosns`) | Manufacturer name: folder and database key (one URL only) |
| `--max-pages N` | 20000 | Pages (static or rendered) per manufacturer |
| `--max-depth N` | 10 | Links/clicks from the start URL |
| `--js auto\|always\|never` | `auto` | Clicking browser: decide per site, always, or never |
| `--all-pdfs` | off | Keep every PDF of the catalogue, not only datasheets |
| `--dry-run` | off | Print the datasheets found; download and write nothing (no database needed) |
| `--force` | off | Download again even if already saved |
| `--no-db` | off | No PostgreSQL; hashes only in `manifest.jsonl` |
| `-v` | off | Print every page and every click |

Environment variables: `DATABASE_URL` (default `postgresql+psycopg2://username:postgres@localhost:5432/postgres`, can be in `.env`), `CRAWLER_DELAY` (seconds between requests to one host, default `1.0`), `CRAWLER_USER_AGENT`, `PLAYWRIGHT_CHROMIUM_PATH` (default `/opt/pw-browsers/chromium`).

Requirements: `pip install -r requirements.txt` (the download step uses `httpx`, `beautifulsoup4`, `playwright`, `cryptography`, `SQLAlchemy`, `psycopg2-binary`, `python-dotenv`); a Chromium for Playwright (`python -m playwright install chromium`), needed for JavaScript catalogues; PostgreSQL unless `--no-db`. In a sandbox with a network allowlist, every host of the catalogue must be allowed (for NOVOSENSE also `wwwapi.novosns.com`, which serves its iframes).

A typical log (also saved in `logs/datasheets_<date>_<time>.log`):

```
[1/1] novosns - https://www.novosns.com/en/
    catalogue crawl of https://www.novosns.com/en/ (browser, clicking)
    ! [datasheet_unavailable] https://www.novosns.com/en/abs-speed-sensor dataSheet (NSM4101-Q0TOMR ...): Sorry, the datasheet is not publicly available ...
      + novosns/mt6835-rev-1-3_0c2d5a1f.pdf
    .. 25 pages, 140 queued, 61 PDFs, 410 clicks, 1630s  [https://www.novosns.com/en/product/...]
    ...
    pdfs: 812, datasheets: 790, downloaded: 301, skipped: 0, duplicates: 489, failed: 0, not_datasheet: 22
```

`pdfs`: PDFs found. `datasheets`: passed `is_datasheet`. `downloaded`: new files. `skipped`: URL already saved. `duplicates`: the bytes already existed under another URL (many parts of a series share one datasheet). `failed`: download or database write failed. `not_datasheet`: dropped by the filter. Exit code `0` also when some downloads failed (see `datasheet_errors`).

---

## 4. How the catalogue is walked

Breadth-first from the start URL: categories first, then families, then part pages. Pages are only fetched from the start URL's host (and the hosts of iframes the catalogue embeds), so other language sites on other subdomains (`www.goodark.com` is Chinese) are not crawled; `/zh/`, `/jp/`, `?lang=de` style links are skipped too. `robots.txt` is respected for every page.

**Which links and clickable elements belong to the catalogue** (`PartCrawler._decide`):

- The **start URL** is the catalogue, unless it is the site root (`/`, `/en/`, `/index.php`): then it is only a way in, and only items about products are followed from it.
- An item is **about products** when its own text, or the **section label** it sits under, matches `PRODUCT_KEYWORDS` (`product`, `catalog`, `series`, `family`, `selection guide`, `产品`, ...). The section label is the nearest short text before the item that is not another item of the same kind: for the footer entry "Sensor" it is the column title "Products"; for "Automotive" it is "Applications" (not followed).
- **Inside the catalogue** everything in the page content is followed: family links, part numbers, pagination, "Datasheet" buttons. Header, footer and navigation bars (`<header>`, `<footer>`, `<nav>`, or classes such as `header2`, `foot`, `nav-bg` on the first levels below `<body>`) only when the item's own text is about products, so "Support" or "Careers" in a part page's menu is not followed.
- **Never**: `PART_NEGATIVE_KEYWORDS` in the item's text or in the target URL path (news, careers, about, contact, investors, login, cart, buy, sample requests, cross-reference search, document libraries, ...). These match **whole words** only, so "Pressure Sensor" is not a "press" link and "Sampling ADC" is not a "sample" button. `CLICK_SKIP_RE` skips controls that never navigate (reset, export, columns, language names, "10 / page", ...); form fields, table headers, tabs and checkboxes are never clicked.
- Static pages: a link that looks like pagination (`?page=2`, text `2`, `next`, `›`) in the content of a catalogue page is always followed. Extension-less links such as `/download?id=5` are checked with a HEAD request after the crawl (at most 200).

---

## 5. Clicking (JavaScript sites)

Some catalogues have no links at all: menus, category tiles, table rows and "Datasheet" buttons are `<div>`s with click handlers, often inside an iframe. For each page `_explore_rendered`:

1. Opens it in Chromium, waits until no frame changes any more (rows of JavaScript tables arrive after the page has "loaded"), and closes language choosers, cookie banners and notices (prefers an "English" button, then OK/accept/close).
2. Lists the **clickable elements** of every frame: the outermost element with a pointer cursor, buttons, `role=button`, `onclick`. Icons without text are used only when their `alt`/`title`/`aria-label` says document (`dataSheet`, `pdf`, `download`, ...), so share/ECAD/zoom icons are left alone. Real `<a href>` links are handled as in a static page.
3. Pages through JavaScript tables and lists: presses the enabled "next" button of a pagination bar (`[class*=pagination]`, `[class*=pager]`) until the rows stop changing, and records the elements of every page.
4. Clicks each element: document buttons first, visible before hidden. A click can
   - open a **new tab** or start a **download**: its URL is a PDF if it ends in `.pdf` (or, for a document button, if the server answers `application/pdf`); otherwise a page for the queue;
   - **change the URL** (a router navigation): the new URL goes to the queue, and the page is reloaded (and paged again) before the next click;
   - **change the page in place**: new links are collected; a notice after a document button ("datasheet is not publicly available") is retried once after a wait (it can also mean "not loaded yet"), then logged with stage `datasheet_unavailable`.
5. Remembers what was clicked so that nothing is clicked twice: a named item ("Magnetic Sensor", "NSM4101-Q0TOMR") once per site; a generic one ("Datasheet", an icon) once per table row or page. A hidden menu copy that did nothing does not block the visible item with the same text.

Elements are found again after a reload by frame, text, icon text, table row and position, so a click always hits the same element even though the page was rebuilt.

---

## 6. Which PDFs are datasheets

`is_datasheet(pdf)` in `crawler.py`:

1. The PDF's own URL, link/button text and last click decide first: `DATASHEET_KEYWORDS` (`datasheet`, `data sheet`, `specification`, `规格书`, ...) against `OTHER_DOC_KEYWORDS` (application notes and `AN-12-0029`-style numbers, user guides, manuals, reliability reports, certificates, brochures, catalogues, selection/solution guides, reference designs, package drawings, ...).
2. Otherwise its table row / list item.
3. A PDF with no hint at all is kept if it was found inside the catalogue.

`--all-pdfs` turns the filter off.

---

## 7. Saving: files, manifest, database

`downloader.download(fetcher, pdf, target_dir, known_hashes, base_dir)`:

1. Stream into a hidden `.{sha1 of url}.part` file while hashing (SHA-256). Abort above 300 MB or if the content-type is HTML.
2. The first 1024 bytes must contain `%PDF-` (stage `verify` otherwise).
3. If the manufacturer already has these bytes **and that file still exists**, the temporary file is deleted and the existing path returned (`duplicate_of`): one file for all parts sharing a datasheet.
4. Otherwise the file is named after the document itself: `{name from Content-Disposition, else the URL's last part}_{hash8}.pdf`, e.g. `mt6835-rev-1-3_0c2d5a1f.pdf`, and moved into place atomically.

`run.py` then writes:

- `datasheets/{manufacturer}/manifest.jsonl` (gitignored with `datasheets/`): one JSON line per source URL with `source_url`, `page_url` (where it was found), `link_text`, `context` (the table row, usually the part number), `trail` (links/clicks from the start page, e.g. `Sensor > Magnetic Sensor > ABS Speed Sensor > NSM4101-Q0TOMR > dataSheet`), `file_path`, `file_hash`, `file_size`, `downloaded_at`. Used to skip known URLs when there is no database.
- The database, only what the crawler needs systematically (both tables are created by `run.py`, also in `schema.sql`):

| Table | Columns | Purpose |
|---|---|---|
| `datasheet_files` | `manufacturer`, `source_url` (unique per manufacturer), `file_path` (relative to `datasheets/`), `file_hash` (SHA-256), `file_size`, `downloaded_at` | Skip known URLs, store identical bytes once |
| `datasheet_errors` | `manufacturer`, `url`, `stage`, `error`, `http_status`, `traceback`, `occurred_at` | Append-only failure log |

Files saved by a `--no-db` run get their `datasheet_files` row on the next run with a database.

```sql
SELECT manufacturer, count(*), count(DISTINCT file_hash) FROM datasheet_files GROUP BY 1;  -- URLs vs. files
SELECT stage, count(*) FROM datasheet_errors GROUP BY 1;                                    -- what is failing
```

---

## 8. `fetcher.py`: all network access

One `Fetcher` per run.

- **Politeness.** `_wait(url)` keeps two requests (and two browser loads or clicks) to the same host at least `REQUEST_DELAY` (1 s) apart. `allowed(url)` reads `robots.txt` once per origin; if it cannot be read, everything is allowed. `_request` retries up to 3 times with 2, 4, 8 s backoff on connection errors and HTTP 429/500/502/503/504.
- **Requests.** `get_page(url)`: GET, reads the body only if it is HTML (max 10 MB), `FetchError` for HTTP ≥ 400. `is_pdf(url)`: HEAD (streamed GET if HEAD is rejected); true if the content-type contains `pdf` or `Content-Disposition` names a `.pdf`. `stream(url)`: streamed GET for the downloader.
- **Browser.** `browser_context()` starts Playwright/Chromium on first use (static sites never start it), 1440×900, English locale, downloads observable. If Playwright's own browser is missing it uses `PLAYWRIGHT_CHROMIUM_PATH` with `--no-sandbox`.
- **Incomplete TLS chains.** Some servers leave out their intermediate CA certificate; browsers fetch it, `httpx` fails with `CERTIFICATE_VERIFY_FAILED`. `_repair_chain(host)` follows the certificate's *CA Issuers* link (up to 4 levels), trusts what it gets and retries. Verification stays on.

---

## 9. The two example sites

| | Good-Ark | NOVOSENSE |
|---|---|---|
| Start URL | `https://en.goodark.com/product.php` | `https://www.novosns.com/en/` |
| Mode | static HTML | browser + clicking (Nuxt app, no `<a href>`; content in iframes from `wwwapi.novosns.com`) |
| Path to a datasheet | product page → family `prolist.php?id=551` → pages `&page=2..` → part `proshow.php?id=26552` → `specification/10A05 thru 10A10.pdf` | footer column "Products" › "Sensor" → `/en/sensor` → "Magnetic Sensor" → "ABS Speed Sensor" (part table) → part "MT6835GT-STD-R" or its `dataSheet` icon → new tab `/en/datasheet/<part>/MT6835_Rev.1.3.pdf` |
| Particulars | Chinese/Japanese/Korean sites on other subdomains are ignored; one datasheet covers a series (`10A05 thru 10A10`), so most part pages repeat a known URL; filters (`javascript:` links) are not used | language chooser on every page; some parts answer "datasheet is not publicly available" (stage `datasheet_unavailable`, not an error); all parts of a series share one PDF under different URLs (saved once, `duplicates`) |

---

## 10. Failure handling

| What fails | `stage` | Effect |
|---|---|---|
| Start URL cannot be fetched | `website` | Error row. Next manufacturer. |
| Page blocked by `robots.txt` | `robots` | Page skipped. |
| A page cannot be fetched / rendered | `crawl` / `render` | Page skipped, crawl goes on. |
| A click fails or its element is gone after a reload | `click` | That element is skipped (and may be tried from another page). |
| Datasheet button shows "not available" twice | `datasheet_unavailable` | Logged; nothing to download. |
| Extension-less link check fails | `head_check` | Link skipped. |
| PDF request fails or exceeds 300 MB | `download` | Temporary file removed. Retried on the next run. |
| Response is not a PDF | `verify` | Temporary file removed. |
| Moving the file fails | `write` | Error row. |
| Saving the database row fails | `db` | File and manifest line stay; the row is added on the next run. |
| Anything unexpected for one manufacturer | `manufacturer` | Error row with traceback. Next manufacturer. |
| Database unavailable at start | — | The run stops (use `--no-db`). |

Writing an error row never raises; without a database errors are only printed (and in the log file).

---

## 11. Every threshold in one place

| Name | Value | File | Meaning |
|---|---|---|---|
| `REQUEST_DELAY` | 1.0 s (`CRAWLER_DELAY`) | `settings.py` | Gap between requests/clicks to one host |
| `REQUEST_TIMEOUT` | 30 s | `settings.py` | Per request |
| `MAX_RETRIES` | 3 (backoff 2, 4, 8 s) | `settings.py` | Connection errors and HTTP 429/5xx |
| `MAX_PDF_BYTES` / `MAX_HTML_BYTES` | 300 MB / 10 MB | `settings.py` | Larger files are not saved / parsed |
| `MAX_HEAD_CHECKS` | 200 per site | `settings.py` | Extension-less links tested for `application/pdf` |
| `PART_MAX_PAGES` | 20000 (`--max-pages`) | `settings.py` | Pages per manufacturer |
| `PART_MAX_DEPTH` | 10 (`--max-depth`) | `settings.py` | Links/clicks from the start URL |
| `PART_MAX_CLICKS_PER_PAGE` | 400 | `settings.py` | Elements clicked on one rendered page |
| `PART_MAX_PAGINATION` | 60 | `settings.py` | "next page" clicks in one JavaScript table |
| `_needs_js` | < 10 links or < 300 text characters | `crawler.py` | Start page looks JavaScript-built → browser |
| page stable | 2 × 500 ms unchanged, max 12 s | `crawler.py` | Wait after loading / paging |
| click outcome wait | 3 s (then up to 5 s idle + 6 s stable if it changed in place) | `crawler.py` | Tab, download or route change |
| `CHROME_DEPTH` | 5 levels below `<body>` | `crawler.py` | Where header/footer classes are recognised |
| PDF signature window | first 1024 bytes | `downloader.py` | Must contain `%PDF-` |
| keyword lists | `PRODUCT_`, `PART_NEGATIVE_`, `DATASHEET_`, `OTHER_DOC_KEYWORDS`, `CLICK_SKIP_RE`, `DOC_HINT_RE`, `MODAL_DISMISS_RE` | `settings.py` | What counts as catalogue / datasheet / control |

---

## 12. Tests

`python tests/test_download_crawler.py` (or `pytest tests/test_download_crawler.py`) runs offline unit tests on a synthetic static catalogue served by a fake fetcher: families, pagination and part pages are reached; header/footer, news and other-language subdomains are not; the datasheet filter; the click decisions (footer "Products" vs. "Applications", header items on part pages, icons, controls); click memo keys; whole-word negatives ("Pressure Sensor").

The clicking path needs a real browser and site; it was checked by hand against NOVOSENSE, and the static path against Good-Ark. The quickest check of a change is a dry run with a small budget: `python -m dataPipeline.download.run <url> --dry-run --max-pages 20 -v`, which prints every page, every click and every datasheet.

When a datasheet is missed, `-v` shows why: the page was never queued (an item judged outside the catalogue or negative), an element was never clicked (skipped as a control, or icon without a document hint), or the PDF was dropped by `is_datasheet` (`- not a datasheet:` lines).

---

## 13. Known limits

- **Slow on JavaScript sites**: every click that changes the URL means reloading the page before the next click, about 5–10 s, plus the 1 s per-host pause. A large catalogue takes hours. Static sites run at about one page per 1–2 s.
- Filters and search boxes are not used; the crawl relies on the category tree and pagination reaching every part. Parts reachable only through a search are missed.
- Clicks whose only effect is not a URL, a tab, a download or new links (e.g. content drawn on a canvas) are not explored further.
- `is_datasheet` uses words, not the PDF content: a brochure without any keyword found on a catalogue page is kept; a datasheet named only by a number outside the catalogue is dropped.
- Login-only datasheets are not fetched.
- No resume: a stopped crawl starts at the start URL again (saved files are skipped, not downloaded again).
- `datasheet_errors` grows with every run and is never cleaned.
