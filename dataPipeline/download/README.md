# PDF download step

Crawls each company's **English** website and downloads every PDF found in its
financial-report and announcement sections to `reports/` (git-ignored).

```bash
pip install httpx beautifulsoup4 playwright sqlalchemy psycopg2-binary python-dotenv
python -m playwright install chromium   # optional: falls back to PLAYWRIGHT_CHROMIUM_PATH

python -m dataPipeline.download.run                    # all companies
python -m dataPipeline.download.run --company-id 3 7   # selected companies
python -m dataPipeline.download.run --dry-run          # list what would be downloaded
```

Options: `--max-pages 150`, `--max-depth 3`, `--js auto|always|never`, `--force`, `--limit N`.
Env: `DATABASE_URL`, `CRAWLER_DELAY` (seconds per host, default 1), `CRAWLER_USER_AGENT`,
`PLAYWRIGHT_CHROMIUM_PATH`.

## How it works

1. **Input:** `companies JOIN stocks ON isin` → `stocks.website_url`.
2. **English version:** `<link hreflang="en">` → `<html lang="en">` → language-switcher link
   → probing `/en/`, `/eng/`, `/en-gb/`. If none exists, the default language is crawled and an
   `english_version` row is logged. Pages under other language prefixes (`/sl/`, `/hr/`, `?lang=de`, …) are skipped.
3. **Focused crawl** (`crawler.py`): a priority queue on the company's domain. Links are scored by keywords
   (investor, reports, announcements, AGM, dividend, … plus SL/HR stems), with negatives such as careers,
   products and privacy. Pagination and year links are followed only from relevant pages. Iframes (hosted IR
   widgets) are followed too. `robots.txt` is respected, with about 1 request/second per host.
4. **JavaScript sites:** Playwright is used when the static HTML is nearly empty, or when the static crawl
   finds no PDFs. It scrolls, accepts cookie banners and clicks "load more" buttons.
5. **PDF links:** `.pdf` URLs, plus extension-less download links (`/download?id=…`) checked by content-type.
   A PDF is kept if its own text/URL looks financial, or if it was found on an investor/report page and does
   not look like a catalogue or privacy policy. No LLM is involved.
6. **Category** (keyword vote: link text, row context, then page): `reports`, `announcements`, or `other`.
7. **Download** (`downloader.py`): streamed, checked for `%PDF-`, SHA-256 deduplicated per company.
   Saved as `reports/{company-slug}/{category}/{year|undated}_{title-slug}_{hash8}.pdf`.
8. **Database:** successes go to `downloaded_files` (reruns skip known URLs whose file still exists),
   failures to `download_errors` with stage, HTTP status and traceback.

```sql
SELECT stage, count(*) FROM download_errors GROUP BY stage;          -- what is failing
SELECT * FROM download_errors WHERE company_id = 3 ORDER BY occurred_at DESC;
```

## Known limits

- Year/period filters that need a dropdown or tab click are not automated (only scrolling and "load more").
- Documents hosted on another domain are downloaded when linked directly as `.pdf`, but their sites are
  not crawled, except through iframes.
- The category and year are heuristics from link text. Confirm them later from the PDF itself (Step 3).
