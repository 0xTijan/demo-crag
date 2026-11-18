"""Download every datasheet (PDF) from component manufacturers' websites.

    python -m dataPipeline.download.run https://en.goodark.com/product.php
    python -m dataPipeline.download.run https://www.novosns.com/en/ https://en.goodark.com/product.php
    python -m dataPipeline.download.run --file manufacturers.txt     # one URL per line
    python -m dataPipeline.download.run URL --dry-run                # list datasheets only
    python -m dataPipeline.download.run URL --no-db                  # no PostgreSQL needed
    python -m dataPipeline.download.run URL -v --show-browser        # watch it click, in a browser window
    python -m dataPipeline.download.run --retry-failed               # retry failed datasheets (all makers)
    python -m dataPipeline.download.run URL --retry-failed           # ... of this manufacturer only

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
from urllib.parse import unquote, urlsplit  # noqa: E402

from . import db  # noqa: E402
from .crawler import DatasheetLink, PartCrawler, is_datasheet, site_key  # noqa: E402
from .downloader import DownloadError, download, slugify  # noqa: E402
from .fetcher import FetchError, Fetcher  # noqa: E402
from .settings import DATASHEETS_DIR, PART_MAX_DEPTH, PART_MAX_PAGES  # noqa: E402
from helpers.run_log import start_run_log  # noqa: E402

MANIFEST = "manifest.jsonl"
MISSING = "missing.jsonl"  # datasheets that could not be obtained, with all context


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
        # (file name, size) -> saved file: the same datasheet under another URL is recognised by a
        # HEAD request instead of being downloaded again (parts of a series share one PDF)
        self.by_name_size = {}
        for r in self.manifest.values():
            self._index(r["source_url"], r.get("file_size"), r["file_path"], r["file_hash"])
        self.db_urls = set()
        if engine is not None:
            urls, hashes = db.known_datasheets(engine, name)
            self.db_urls = set(urls)
            self.known_urls.update(urls)
            self.known_hashes.update(hashes)
        self.stats = {"pdfs": 0, "datasheets": 0, "downloaded": 0, "skipped": 0, "duplicates": 0,
                      "failed": 0, "not_datasheet": 0, "missing": 0}

    def _index(self, url: str, size, file_path: str, file_hash: str) -> None:
        name = Path(unquote(urlsplit(url).path)).name.lower()
        if name and size:
            self.by_name_size[(name, int(size))] = (file_path, file_hash)

    def _known_by_head(self, pdf):
        """(file_path, file_hash) of an already saved PDF with the same file name and size, or None."""
        name = Path(unquote(urlsplit(pdf.url).path)).name.lower()
        if self.args.force or not any(k[0] == name for k in self.by_name_size):
            return None  # no saved file with this name: nothing to compare, download it
        head = self.fetcher.head_info(pdf.url)
        if not head or "html" in head["content_type"]:
            return None
        hit = self.by_name_size.get((name, head["size"]))
        return hit if hit and (DATASHEETS_DIR / hit[0]).exists() else None

    def missing(self, stage: str, message: str, url: str | None, details: dict, http_status=None) -> None:
        """A datasheet the site shows but that could not be obtained: keep everything known about it
        in missing.jsonl and in datasheet_errors.context."""
        self.stats["missing"] += 1
        record = {"manufacturer": self.name, "stage": stage, "error": message, "url": url,
                  "http_status": http_status, **(details or {}),
                  "recorded_at": datetime.now(timezone.utc).isoformat()}
        if not self.args.dry_run:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / MISSING).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.log_error(stage, message, url=url, http_status=http_status, context=record)

    def __call__(self, pdf) -> str:
        """Save one PDF. Returns downloaded | duplicate | skipped | failed | not_datasheet | dry_run."""
        self.stats["pdfs"] += 1
        if not self.args.all_pdfs and not is_datasheet(pdf):
            self.stats["not_datasheet"] += 1
            if self.args.verbose:
                print(f"      - not a datasheet: {pdf.url}")
            return "not_datasheet"
        self.stats["datasheets"] += 1
        if self.args.dry_run:
            print(f"      [datasheet] {' > '.join(pdf.trail[-3:])!r} {pdf.url}")
            return "dry_run"
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
                    return "failed"
            return "skipped"
        same = self._known_by_head(pdf)
        try:
            if same:
                info = {"file_path": same[0], "file_hash": same[1], "duplicate_of": same[0],
                        "file_size": int((DATASHEETS_DIR / same[0]).stat().st_size)}
            else:
                info = download(self.fetcher, pdf, self.folder, self.known_hashes, DATASHEETS_DIR)
        except DownloadError as exc:
            self.stats["failed"] += 1
            details = dict(pdf.details, link_text=pdf.text, context=pdf.context, page_url=pdf.page_url,
                           trail=list(pdf.trail))
            self.missing(exc.stage, str(exc), pdf.url, details, http_status=exc.status)
            return "failed"
        row = {"manufacturer": self.name, "source_url": pdf.url, "page_url": pdf.page_url,
               "link_text": pdf.text[:500], "context": pdf.context[:1000], "trail": " > ".join(pdf.trail)[:2000],
               "file_path": info["file_path"], "file_hash": info["file_hash"], "file_size": info["file_size"]}
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / MANIFEST).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "downloaded_at": datetime.now(timezone.utc).isoformat()},
                                ensure_ascii=False) + "\n")
        self.known_urls[pdf.url] = info["file_path"]
        self.manifest[pdf.url] = row
        self._index(pdf.url, info["file_size"], info["file_path"], info["file_hash"])
        if self.engine is not None:
            try:
                db.record_datasheet(self.engine, row)
                self.db_urls.add(pdf.url)
            except Exception as exc:  # noqa: BLE001
                self.stats["failed"] += 1
                self.log_error("db", exc, url=pdf.url, context={**row, "trail": list(pdf.trail)})
                return "failed"
        if info["duplicate_of"]:
            self.stats["duplicates"] += 1
            return "duplicate"
        else:
            self.stats["downloaded"] += 1
            print(f"      + {info['file_path']}")
            return "downloaded"


def process_manufacturer(engine, fetcher, url: str, name: str, args) -> dict:
    log_error = db.ErrorLog(engine, name)
    saver = Saver(fetcher, engine, name, args, log_error)
    crawler = PartCrawler(fetcher, log_error, on_pdf=saver, max_pages=args.max_pages,
                          max_depth=args.max_depth, js=args.js, verbose=args.verbose, fast=args.fast,
                          on_missing=lambda stage, message, url, details: saver.missing(stage, message, url, details))
    try:
        result = crawler.crawl(url)
    except FetchError as exc:
        log_error("website", exc, url=url, http_status=exc.status)
        return saver.stats
    print(f"    {result.pages_visited} pages{' (browser)' if result.used_js else ''}, "
          f"{result.clicks} clicks, {len(result.pdfs)} PDF links, "
          f"{len(result.unavailable)} datasheets not public")
    return saver.stats


# ------------------------------------------------------------------ --retry-failed
RETRY_OK = ("downloaded", "duplicate", "skipped")


def failure_key(row: dict) -> tuple:
    """Rows/lines about the same failed datasheet share this key (reruns log them again)."""
    ctx = row.get("context") or {}
    if row["stage"] == "datasheet_unavailable":
        return ("click", row["manufacturer"], ctx.get("page_url") or row.get("url"),
                ctx.get("element", ""), ctx.get("row_text", ""))
    return ("download", row["manufacturer"], row.get("url"))


def drop_missing_lines(folder: Path, keys: set, before: str) -> None:
    """Remove lines of missing.jsonl for these failures that were written before `before` (ISO time)."""
    path = folder / MISSING
    if not path.exists():
        return
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        old = rec.get("recorded_at", "") < before
        if not (old and failure_key({**rec, "context": rec}) in keys):
            kept.append(line)
    path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")


def retry_failed(engine, fetcher, names, args) -> dict:
    """Try every failed datasheet in datasheet_errors once more. A success is saved like any download
    and its error rows (and missing.jsonl lines) are removed; a failure leaves one fresh row."""
    groups: dict = {}
    for row in db.failed_datasheets(engine, names):
        groups.setdefault(failure_key(row), []).append(row)
    print(f"{len(groups)} failed datasheet(s) to retry ({sum(map(len, groups.values()))} error rows)")
    totals = {"retried": 0, "recovered": 0, "still_failing": 0, "cannot_retry": 0}
    savers, crawlers = {}, {}
    started = datetime.now(timezone.utc).isoformat()
    for i, (key, rows) in enumerate(groups.items(), 1):
        last = rows[-1]
        name, ctx = last["manufacturer"], last["context"] or {}
        if name not in savers:
            log_error = db.ErrorLog(engine, name)
            savers[name] = Saver(fetcher, engine, name, args, log_error)
            crawlers[name] = PartCrawler(fetcher, log_error, verbose=args.verbose,
                                         on_missing=lambda st, m, u, d, s=savers[name]: s.missing(st, m, u, d))
        saver = savers[name]
        label = (ctx.get("row_text") or ctx.get("element") or "")[:70]
        print(f"[{i}/{len(groups)}] {name}: {last['stage']} {label!r} {last['url']}")
        if args.dry_run:
            continue
        missing_before = saver.stats["missing"]
        totals["retried"] += 1
        if key[0] == "click":
            if not ctx.get("element"):
                totals["cannot_retry"] += 1
                print("      cannot retry: no element recorded (error from an older version)")
                continue
            pdfs = crawlers[name].retry_click(key[2], ctx["element"], ctx.get("row_text", ""), ctx.get("trail", ()))
            outcomes = [saver(pdf) for pdf in pdfs]
        else:
            pdf = DatasheetLink(last["url"], ctx.get("link_text") or ctx.get("element", ""),
                                ctx.get("context") or ctx.get("row_text", ""), ctx.get("page_url", ""),
                                ctx.get("page_title", ""), tuple(ctx.get("trail", ())), True,
                                details={k: v for k, v in ctx.items()
                                         if k not in ("stage", "error", "http_status", "recorded_at", "manufacturer")})
            outcomes = [saver(pdf)]
        ids = [r["error_id"] for r in rows]
        if outcomes and all(o in RETRY_OK for o in outcomes):
            totals["recovered"] += 1
            db.delete_errors(engine, ids)
            drop_missing_lines(saver.folder, {key}, started)
            print(f"      recovered ({', '.join(outcomes)}); {len(ids)} error row(s) removed")
        else:
            totals["still_failing"] += 1
            if saver.stats["missing"] > missing_before:  # a fresh row with the new error replaces the old ones
                db.delete_errors(engine, ids)
                drop_missing_lines(saver.folder, {key}, started)
            print(f"      still failing ({', '.join(outcomes) or 'no PDF'})")
    print("Retry: " + ", ".join(f"{k}: {v}" for k, v in totals.items()))
    return totals


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
    parser.add_argument("--fast", action="store_true",
                        help="JavaScript tables: when a row's datasheet button gives the PDF, do not also open "
                             "that row's part page (much faster; part pages are not all visited)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="only retry the datasheets in datasheet_errors (all manufacturers, or those of the "
                             "given URLs / --name); a success is saved and its error rows removed")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every page and click")
    parser.add_argument("--show-browser", action="store_true",
                        help="open a visible browser window to watch the clicking (JavaScript sites; needs a display)")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    if args.retry_failed and args.no_db:
        parser.error("--retry-failed reads datasheet_errors: it needs the database")
    if not urls and not args.retry_failed:
        parser.error("give at least one URL (or --file)")
    if args.name and len(urls) > 1:
        parser.error("--name works with one URL only")
    start_run_log("datasheets")

    engine = None
    if not args.no_db and (not args.dry_run or args.retry_failed):
        engine = db.get_engine()
        db.ensure_tables(engine)

    fetcher = Fetcher(headless=not args.show_browser)
    totals: dict = {}
    started = time.monotonic()
    if args.retry_failed:
        args.all_pdfs = True  # these were datasheets already; do not filter them again
        names = [args.name] if args.name else [manufacturer_name(u) for u in urls] or None
        try:
            totals = retry_failed(engine, fetcher, names, args)
        finally:
            fetcher.close()
        print(f"\nDone in {time.monotonic() - started:.0f}s")
        return 0
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
