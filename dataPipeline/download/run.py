"""Download financial reports and announcements (PDF) for every company in the database.

    python -m dataPipeline.download.run                   # all companies
    python -m dataPipeline.download.run --company-id 3 7  # selected companies
    python -m dataPipeline.download.run --dry-run         # list PDFs, download nothing

Website URLs come from `stocks.website_url` (joined via `companies.isin`).
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
from .downloader import DownloadError, download, slugify  # noqa: E402
from .fetcher import FetchError, Fetcher  # noqa: E402
from .settings import REPORTS_DIR  # noqa: E402


def process_company(engine, fetcher, company, args) -> dict:
    stats = {"found": 0, "downloaded": 0, "skipped": 0, "duplicates": 0, "failed": 0}
    log_error = db.ErrorLog(engine, company["company_id"])
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
    known_urls, known_hashes = db.known_downloads(engine, company["company_id"])
    company_dir = REPORTS_DIR / slugify(company["name"])

    for pdf in pdfs:
        category = categorise(pdf)
        if args.dry_run:
            print(f"      [{category}] {pdf.text[:70]!r} {pdf.url}")
            continue
        existing = known_urls.get(pdf.url)
        if existing and (REPORTS_DIR / existing).exists() and not args.force:
            stats["skipped"] += 1
            continue
        try:
            info = download(fetcher, pdf, company_dir / category, known_hashes, REPORTS_DIR)
        except DownloadError as exc:
            stats["failed"] += 1
            log_error(exc.stage, str(exc), url=pdf.url, http_status=exc.status)
            continue
        try:
            db.record_download(engine, {
                "company_id": company["company_id"], "category": category, "source_url": pdf.url,
                "page_url": pdf.page_url, "link_text": pdf.text[:500], **{
                    k: info[k] for k in ("file_path", "file_hash", "file_size", "fiscal_year")},
            })
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            log_error("db", exc, url=pdf.url)
            continue
        if info["duplicate_of"]:
            stats["duplicates"] += 1
        else:
            stats["downloaded"] += 1
            print(f"      + {info['file_path']}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company-id", type=int, nargs="+", help="only these company_id values")
    parser.add_argument("--limit", type=int, help="process at most N companies")
    parser.add_argument("--max-pages", type=int, default=150, help="pages crawled per site (default 150)")
    parser.add_argument("--max-depth", type=int, default=3, help="link depth from the homepage (default 3)")
    parser.add_argument("--js", choices=("auto", "always", "never"), default="auto",
                        help="render pages with Playwright: auto-detect, always or never (default auto)")
    parser.add_argument("--dry-run", action="store_true", help="list PDFs without downloading")
    parser.add_argument("--force", action="store_true", help="download again even if already recorded")
    args = parser.parse_args()

    engine = db.get_engine()
    db.ensure_tables(engine)
    companies = db.load_companies(engine, args.company_id, args.limit)
    if not companies:
        print("No companies found.")
        return 1

    fetcher = Fetcher()
    totals = {"found": 0, "downloaded": 0, "skipped": 0, "duplicates": 0, "failed": 0}
    started = time.monotonic()
    try:
        for i, company in enumerate(companies, 1):
            print(f"[{i}/{len(companies)}] {company['name']} - {company.get('website_url') or 'no website'}")
            try:
                stats = process_company(engine, fetcher, company, args)
            except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
                db.ErrorLog(engine, company["company_id"])("company", exc, url=company.get("website_url"))
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
