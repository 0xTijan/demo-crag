"""Download financial reports and announcements (PDF) for every company in the database.

    python -m dataPipeline.download.run                   # all companies
    python -m dataPipeline.download.run --isin SI0031104332  # selected companies
    python -m dataPipeline.download.run --dry-run         # list PDFs, download nothing
    python -m dataPipeline.download.run --skip-by-name    # skip file names already downloaded

Companies are the rows of `stocks`; the website is `stocks.website_url`.
Files go to reports/{company}/{reports|announcements|other}/. Every download is
recorded in `downloaded_files` and every failure in `download_errors`.
"""
from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from . import db  # noqa: E402
from .crawler import SiteCrawler, categorise, wanted  # noqa: E402
from .downloader import DownloadError, download, name_key, slugify  # noqa: E402
from .fetcher import FetchError, Fetcher  # noqa: E402
from .settings import REPORTS_DIR  # noqa: E402
from helpers.run_log import start_run_log  # noqa: E402


STAT_KEYS = ("found", "downloaded", "skipped", "same_name", "duplicates", "failed")


def process_company(engine, fetcher, company, args) -> dict:
    stats = dict.fromkeys(STAT_KEYS, 0)
    log_error = db.ErrorLog(engine, company["isin"])
    url = (company.get("website_url") or "").strip()
    if not url:
        log_error("website", "company has no website_url in stocks")
        return stats

    crawler = SiteCrawler(fetcher, log_error, max_pages=args.max_pages, max_depth=args.max_depth, js=args.js)
    try:
        result = crawler.crawl(url)
    except FetchError as exc:
        log_error("website", exc, url=url, http_status=exc.status)
        return stats
    print(f"    English site: {result.base_url} ({result.english}); "
          f"{result.pages_visited} pages{' with browser' if result.used_js else ''}; "
          f"{len(result.pdfs)} PDF links")

    pdfs = [p for p in result.pdfs.values() if wanted(p)]
    stats["found"] = len(pdfs)
    known_urls, known_hashes = db.known_downloads(engine, company["isin"])
    company_dir = REPORTS_DIR / slugify(company["name"])
    # --skip-by-name: file names of PDFs already saved for this company, whatever their URL was
    known_names = {}
    if args.skip_by_name:
        for known_url, path in known_urls.items():
            key = name_key(known_url)
            if key:
                known_names[key] = path

    for pdf in pdfs:
        category = categorise(pdf)
        if args.dry_run:
            print(f"      [{category}] {pdf.text[:70]!r} {pdf.url}")
            continue
        existing = known_urls.get(pdf.url)
        if existing and (REPORTS_DIR / existing).exists() and not args.force:
            stats["skipped"] += 1
            continue
        key = name_key(pdf.url) if args.skip_by_name and not args.force else None
        if key and key in known_names and (REPORTS_DIR / known_names[key]).exists():
            stats["same_name"] += 1  # a PDF with this file name is already saved, even under another URL
            continue
        try:
            info = download(fetcher, pdf, company_dir / category, known_hashes, REPORTS_DIR)
        except DownloadError as exc:
            stats["failed"] += 1
            log_error(exc.stage, str(exc), url=pdf.url, http_status=exc.status)
            continue
        try:
            db.record_download(engine, {
                "stock_isin": company["isin"], "category": category, "source_url": pdf.url,
                "page_url": pdf.page_url, "link_text": pdf.text[:500], **{
                    k: info[k] for k in ("file_path", "file_hash", "file_size", "fiscal_year")},
            })
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            log_error("db", exc, url=pdf.url)
            continue
        if name_key(pdf.url):
            known_names[name_key(pdf.url)] = info["file_path"]  # same name again later in this run
        if info["duplicate_of"]:
            stats["duplicates"] += 1
        else:
            stats["downloaded"] += 1
            print(f"      + {info['file_path']}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--isin", nargs="+", help="only these ISINs")
    parser.add_argument("--limit", type=int, help="process at most N companies")
    parser.add_argument("--max-pages", type=int, default=150, help="pages crawled per site (default 150)")
    parser.add_argument("--max-depth", type=int, default=3, help="link depth from the homepage (default 3)")
    parser.add_argument("--js", choices=("auto", "always", "never"), default="auto",
                        help="render pages with Playwright: auto-detect, always or never (default auto)")
    parser.add_argument("--dry-run", action="store_true", help="list PDFs without downloading")
    parser.add_argument("--skip-by-name", action="store_true",
                        help="also skip a PDF whose file name (end of the URL, query ignored) was already "
                             "downloaded for this company, even if its URL differs")
    parser.add_argument("--force", action="store_true", help="download again even if already recorded")
    args = parser.parse_args()
    start_run_log("download")  # copy everything printed below to logs/download_<time>.log

    engine = db.get_engine()
    db.ensure_tables(engine)
    companies = db.load_companies(engine, args.isin, args.limit)
    if not companies:
        print("No companies found.")
        return 1

    fetcher = Fetcher()
    totals = dict.fromkeys(STAT_KEYS, 0)
    started = time.monotonic()
    try:
        for i, company in enumerate(companies, 1):
            print(f"[{i}/{len(companies)}] {company['name']} - {company.get('website_url') or 'no website'}")
            try:
                stats = process_company(engine, fetcher, company, args)
            except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
                db.ErrorLog(engine, company["isin"])("company", exc, url=company.get("website_url"))
                continue
            print("    " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
            for key in totals:
                totals[key] += stats[key]
    finally:
        fetcher.close()

    print(f"\nDone in {time.monotonic() - started:.0f}s - " + ", ".join(f"{k}: {v}" for k, v in totals.items()))
    print("Failures are in the download_errors table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
