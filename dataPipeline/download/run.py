"""Download every datasheet (PDF) from component manufacturers' websites.

    python -m dataPipeline.download.run https://en.goodark.com/product.php
    python -m dataPipeline.download.run https://www.novosns.com/en/ https://en.goodark.com/product.php
    python -m dataPipeline.download.run --file manufacturers.txt     # one URL per line
    python -m dataPipeline.download.run URL --dry-run                # list datasheets only
    python -m dataPipeline.download.run URL --no-db                  # no PostgreSQL needed

Give the product page or the homepage of a manufacturer. The crawler walks every
product category, family and part page (clicking through JavaScript menus and
tables when the site needs it) and downloads every datasheet it finds to
datasheets/{manufacturer}/. Each manufacturer folder has a manifest.jsonl with one
line per source URL (where it was found, part row, click trail). The database only
gets the file hashes (`datasheet_files`) and the failures (`datasheet_errors`).
"""
from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

from . import db  # noqa: E402
from .crawler import PartCrawler, is_datasheet, site_key  # noqa: E402
from .downloader import DownloadError, download, slugify  # noqa: E402
from .fetcher import FetchError, Fetcher  # noqa: E402
from .settings import DATASHEETS_DIR, PART_MAX_DEPTH, PART_MAX_PAGES  # noqa: E402
from helpers.run_log import start_run_log  # noqa: E402

MANIFEST = "manifest.jsonl"


def manufacturer_name(url: str) -> str:
    """goodark for https://en.goodark.com/product.php"""
    host = urlsplit(url if "://" in url else "https://" + url).netloc
    return site_key(host).split(".")[0]


def read_manifest(folder: Path) -> dict:
    """{source_url: row} from earlier runs (file paths relative to datasheets/)."""
    rows = {}
    path = folder / MANIFEST
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows[row["source_url"]] = row
    return rows


class Saver:
    """Downloads each datasheet as soon as the crawler finds it."""

    def __init__(self, fetcher, engine, name: str, args, log_error):
        self.fetcher, self.engine, self.name, self.args, self.log_error = fetcher, engine, name, args, log_error
        self.folder = DATASHEETS_DIR / slugify(name)
        self.manifest = read_manifest(self.folder)
        self.known_urls = {u: r["file_path"] for u, r in self.manifest.items()}
        self.known_hashes = {r["file_hash"]: r["file_path"] for r in self.manifest.values()}
        self.db_urls = set()
        if engine is not None:
            urls, hashes = db.known_datasheets(engine, name)
            self.db_urls = set(urls)
            self.known_urls.update(urls)
            self.known_hashes.update(hashes)
        self.stats = {"pdfs": 0, "datasheets": 0, "downloaded": 0, "skipped": 0, "duplicates": 0,
                      "failed": 0, "not_datasheet": 0}

    def __call__(self, pdf) -> None:
        self.stats["pdfs"] += 1
        if not self.args.all_pdfs and not is_datasheet(pdf):
            self.stats["not_datasheet"] += 1
            if self.args.verbose:
                print(f"      - not a datasheet: {pdf.url}")
            return
        self.stats["datasheets"] += 1
        if self.args.dry_run:
            print(f"      [datasheet] {' > '.join(pdf.trail[-3:])!r} {pdf.url}")
            return
        existing = self.known_urls.get(pdf.url)
        if existing and (DATASHEETS_DIR / existing).exists() and not self.args.force:
            self.stats["skipped"] += 1
            if self.engine is not None and pdf.url not in self.db_urls and pdf.url in self.manifest:
                # saved by a run without the database: add its row now
                try:
                    db.record_datasheet(self.engine, self.manifest[pdf.url])
                    self.db_urls.add(pdf.url)
                except Exception as exc:  # noqa: BLE001
                    self.log_error("db", exc, url=pdf.url)
            return
        try:
            info = download(self.fetcher, pdf, self.folder, self.known_hashes, DATASHEETS_DIR)
        except DownloadError as exc:
            self.stats["failed"] += 1
            self.log_error(exc.stage, str(exc), url=pdf.url, http_status=exc.status)
            return
        row = {"manufacturer": self.name, "source_url": pdf.url, "page_url": pdf.page_url,
               "link_text": pdf.text[:500], "context": pdf.context[:1000], "trail": " > ".join(pdf.trail)[:2000],
               "file_path": info["file_path"], "file_hash": info["file_hash"], "file_size": info["file_size"]}
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / MANIFEST).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "downloaded_at": datetime.now(timezone.utc).isoformat()},
                                ensure_ascii=False) + "\n")
        self.known_urls[pdf.url] = info["file_path"]
        self.manifest[pdf.url] = row
        if self.engine is not None:
            try:
                db.record_datasheet(self.engine, row)
                self.db_urls.add(pdf.url)
            except Exception as exc:  # noqa: BLE001
                self.stats["failed"] += 1
                self.log_error("db", exc, url=pdf.url)
        if info["duplicate_of"]:
            self.stats["duplicates"] += 1
        else:
            self.stats["downloaded"] += 1
            print(f"      + {info['file_path']}")


def process_manufacturer(engine, fetcher, url: str, name: str, args) -> dict:
    log_error = db.ErrorLog(engine, name)
    saver = Saver(fetcher, engine, name, args, log_error)
    crawler = PartCrawler(fetcher, log_error, on_pdf=saver, max_pages=args.max_pages,
                          max_depth=args.max_depth, js=args.js, verbose=args.verbose)
    try:
        result = crawler.crawl(url)
    except FetchError as exc:
        log_error("website", exc, url=url, http_status=exc.status)
        return saver.stats
    print(f"    {result.pages_visited} pages{' (browser)' if result.used_js else ''}, "
          f"{result.clicks} clicks, {len(result.pdfs)} PDF links, "
          f"{len(result.unavailable)} datasheets not public")
    return saver.stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="manufacturer product page or homepage")
    parser.add_argument("--file", type=Path, help="text file with one URL per line (# comments allowed)")
    parser.add_argument("--name", help="manufacturer name (default: from the domain, e.g. goodark)")
    parser.add_argument("--max-pages", type=int, default=PART_MAX_PAGES,
                        help=f"pages crawled per manufacturer (default {PART_MAX_PAGES})")
    parser.add_argument("--max-depth", type=int, default=PART_MAX_DEPTH,
                        help=f"link/click depth from the start URL (default {PART_MAX_DEPTH})")
    parser.add_argument("--js", choices=("auto", "always", "never"), default="auto",
                        help="browser with clicking: auto-detect, always or never (default auto)")
    parser.add_argument("--all-pdfs", action="store_true", help="keep every PDF of the catalogue, not only datasheets")
    parser.add_argument("--dry-run", action="store_true", help="list datasheets without downloading")
    parser.add_argument("--force", action="store_true", help="download again even if already saved")
    parser.add_argument("--no-db", action="store_true", help="do not use PostgreSQL (hashes only in manifest.jsonl)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every page and click")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    if not urls:
        parser.error("give at least one URL (or --file)")
    if args.name and len(urls) > 1:
        parser.error("--name works with one URL only")
    start_run_log("datasheets")

    engine = None
    if not args.no_db and not args.dry_run:
        engine = db.get_engine()
        db.ensure_tables(engine)

    fetcher = Fetcher()
    totals: dict = {}
    started = time.monotonic()
    try:
        for i, url in enumerate(urls, 1):
            name = args.name or manufacturer_name(url)
            print(f"[{i}/{len(urls)}] {name} - {url}")
            try:
                stats = process_manufacturer(engine, fetcher, url, name, args)
            except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
                db.ErrorLog(engine, name)("manufacturer", exc, url=url)
                continue
            print("    " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
    finally:
        fetcher.close()

    print(f"\nDone in {time.monotonic() - started:.0f}s - " + ", ".join(f"{k}: {v}" for k, v in totals.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
