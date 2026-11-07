"""Extract the financial statements from converted Markdown reports into SQL.

    python -m dataPipeline.extract.run                      # every new or changed report
    python -m dataPipeline.extract.run --isin SI0031104332  # only these companies
    python -m dataPipeline.extract.run --file markdowns/petrol/reports/letno_2024.md
    python -m dataPipeline.extract.run --dry-run            # extract and check, print, write nothing
    python -m dataPipeline.extract.run --force              # also files already extracted; overwrite periods

Input: Markdown written by dataPipeline/convert (report_files), for PDFs the download
step filed as reports (downloaded_files.category = 'reports'). Output: one row in
`reports`, `financials` and `valuation_ratios` per report, and a `report_extractions`
row with every number's page and row label. See dataPipeline/extract/README.md.
"""
from dotenv import load_dotenv

load_dotenv()

import argparse  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

from dataPipeline.convert.common import ROOT, StageError, log, sha256_file  # noqa: E402
from helpers.run_log import start_run_log  # noqa: E402

from . import db, derive, llm, period  # noqa: E402
from .locate import locate  # noqa: E402
from .metrics import ITEMS, STATEMENT_ITEMS, industry  # noqa: E402
from .numbers import UNIT_SCALE, parse_number  # noqa: E402
from .pages import split_pages  # noqa: E402
from .validate import PRIOR_TOLERANCE, validate  # noqa: E402

EXTRACTOR_VERSION = "1"  # bump when a change should re-extract every report
DONE = ("extracted", "needs_review", "skipped")
SOURCE_TYPES = {"SI": "Slovenia", "HR": "Croatia", "AT": "Austria"}
COMPARED = ["revenue", "ebit", "net_income", "total_assets", "total_equity"]


class Model:
    """Creates the Ollama client on first use, so runs that skip every file never need Ollama."""

    def __init__(self):
        self.chat, self.name = None, None

    def get(self):
        if self.chat is None:
            self.chat, self.name = llm.ollama_chat()
            log(f"  model: {self.name}")
        return self.chat


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _prior_year_end(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - 1, day=28)


def run_statements(chat, found: dict, pages: dict, item: dict, scope, ind: str, period_type_hint) -> list:
    """Model calls in order: income, balance, then cash flow / key figures only for what is still missing."""
    results, have = [], set()

    def call(statement, items):
        res = llm.extract_statement(chat, statement, items, pages, found[statement].pages,
                                    item.get("name") or "", scope, ind)
        log(f"  {statement} (pages {found[statement].pages}): {len(res.items)} item(s)"
            + (f", {len(res.dropped)} dropped (not on the page)" if res.dropped else ""))
        results.append(res)
        have.update(res.items)

    for statement in ("income", "balance"):
        if statement in found:
            call(statement, STATEMENT_ITEMS[statement])
    if "cashflow" in found and not have & {"ebitda", "depreciation_amortisation"}:
        call("cashflow", STATEMENT_ITEMS["cashflow"])
    if "highlights" in found:
        used = {p for r in results for p in r.pages}
        missing = [i for i in STATEMENT_ITEMS["highlights"] if i not in have
                   and (i != "dividend_per_share" or period_type_hint in (None, "FY"))]
        if missing and not set(found["highlights"].pages) <= used:
            call("highlights", missing)
    return results


def parse_items(results: list, pages: dict, fallback_scale: int | None):
    """Verified strings -> Decimals in full units. Returns (current, prior, items_json, dropped, scales)."""
    current, prior, items_json, dropped, scales = {}, {}, {}, [], set()
    for res in results:
        dropped.extend(res.dropped)
        llm_scale = UNIT_SCALE.get(res.header.get("unit") or "")
        scale = period.unit_in("\n".join(pages[p] for p in res.pages)) or llm_scale or fallback_scale
        if scale is None:
            dropped.append(f"{res.statement}: unit (EUR / thousand / million) not found, assumed units")
            scale = 1
        scales.add(scale)
        for name, entry in res.items.items():
            if name in items_json:  # first statement that found it wins
                continue
            mult = 1 if ITEMS[name].get("per_share") else scale
            record = {"statement": res.statement, "row_label": entry["row_label"], "unit_scale": mult}
            for column, target in (("current", current), ("prior", prior)):
                f = entry[column]
                if f is None:
                    continue
                try:
                    value = parse_number(f.raw, f.decimal)
                except ValueError as exc:
                    dropped.append(f"{name}.{column}: {exc}")
                    continue
                if value is None:
                    continue
                target[name] = value * mult
                record.update({f"{column}_raw": f.raw, f"{column}": str(target[name]),
                               "page": record.get("page", f.page), "label_found": record.get("label_found", f.label)})
            if name in current or name in prior:
                items_json[name] = record
    return current, prior, items_json, dropped, scales


def same_values(fin: dict, stored: dict) -> bool:
    for col in COMPARED:
        a, b = fin.get(col), stored.get(col)
        if a is not None and b is not None and abs(Decimal(a) - Decimal(b)) > PRIOR_TOLERANCE * max(abs(Decimal(a)), abs(Decimal(b)), 1):
            return False
    return True


def process(engine, item: dict, args, model: Model, tracked: bool) -> str:
    """Extract one Markdown file. Returns extracted | needs_review | skipped | unchanged."""
    md_rel = item["markdown_path"]
    md_file = ROOT / md_rel
    try:
        md_hash = sha256_file(md_file)
        markdown = md_file.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise StageError("read", exc) from exc
    base = {"markdown_path": md_rel, "markdown_hash": md_hash, "extractor_version": EXTRACTOR_VERSION,
            "stock_isin": item["stock_isin"], "model": None, "scope": None, "unit_scale": None,
            "items": None, "checks": None, "error": None, "report_id": None}

    try:
        if tracked and not args.force:
            prev = db.previous_status(engine, md_rel, md_hash, EXTRACTOR_VERSION)
            if prev in DONE:
                return "unchanged"
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc

    pages = split_pages(markdown)
    try:
        found, scope, _ = locate(pages)
    except Exception as exc:  # noqa: BLE001
        raise StageError("locate", exc) from exc
    log(f"  {len(pages)} page(s); statements: "
        + (", ".join(f"{s} p{v.pages}" for s, v in found.items()) or "none") + f"; scope: {scope or 'not stated'}")
    if "income" not in found and "balance" not in found:
        if not args.dry_run:
            db.record(engine, {**base, "status": "skipped", "error": "no income statement or balance sheet found"})
        return "skipped"

    try:
        stmt_pages = [p for s in ("income", "balance") if s in found for p in found[s].pages]
        guess = period.detect(pages, stmt_pages)
    except Exception as exc:  # noqa: BLE001
        raise StageError("period", exc) from exc

    ind = industry(item.get("nace"), item.get("sector_name"), item.get("name"))
    try:
        results = run_statements(model.get(), found, pages, item, scope, ind, guess.period_type)
    except Exception as exc:  # noqa: BLE001
        raise StageError("llm", exc) from exc
    base["model"] = model.name

    try:
        current, prior, items_json, dropped, scales = parse_items(results, pages, guess.unit_scale)
    except Exception as exc:  # noqa: BLE001
        raise StageError("parse", exc) from exc

    llm_end = next((_date(r.header.get("period_end")) for r in results if _date(r.header.get("period_end"))), None)
    llm_type = next((r.header.get("period_type") for r in results if r.header.get("period_type")), None)
    llm_currency = next((r.header.get("currency") for r in results if r.header.get("currency")), None)
    period_end = guess.period_end or llm_end
    period_type = guess.period_type or llm_type
    if period_end and not period_type:
        period_type = period.MONTH_TO_TYPE.get(period_end.month)
    currency = (guess.currency or llm_currency or "EUR").upper()[:10]
    unit_scale = max(scales) if scales else None
    log(f"  period: {period_end} {period_type} (text: {guess.period_end} {guess.period_type}; "
        f"model: {llm_end} {llm_type}); unit x{unit_scale}; {currency}; industry: {ind}")

    try:
        stored_prior = stored_prior_balance = siblings = None
        if period_end and period_type:
            stored_prior = db.stored_report(engine, item["stock_isin"], _prior_year_end(period_end), period_type)
            # interim balance sheets compare with the previous year end (Dec FY assumed)
            year_end = (_prior_year_end(period_end) if period_type == "FY"
                        else date(period_end.year - 1, 12, 31))
            stored_prior_balance = db.stored_report(engine, item["stock_isin"], year_end, "FY")
            siblings = db.siblings(engine, item["stock_isin"], period_end.year, period_type)
        checks = validate(current, prior, industry=ind, unit_scale=unit_scale or 1, period_end=period_end,
                          period_type=period_type,
                          period_votes={"text": (guess.period_end, guess.period_type), "model": (llm_end, llm_type)},
                          fiscal_year_hint=item.get("fiscal_year"), stored_prior=stored_prior,
                          stored_prior_balance=stored_prior_balance, siblings=siblings or {}, dropped=dropped)
        fin, notes = derive.financials(current, item.get("quantity"))
        price = db.price_at(engine, item["stock_isin"], period_end) if period_end else None
        val, vnotes = derive.valuations(fin, Decimal(price) if price is not None else None,
                                        item.get("quantity"), current.get("dividend_per_share"))
        for note in notes + vnotes:
            if "does not fit" in note:
                checks.append({"check": "overflow", "detail": note})
        items_json["_derived"] = notes + vnotes
    except Exception as exc:  # noqa: BLE001
        raise StageError("validate", exc) from exc

    status = "needs_review" if checks else "extracted"
    report = None
    if period_end and period_type:
        report = {"stock_isin": item["stock_isin"], "period_end": period_end, "period_type": period_type,
                  "currency": currency, "source_type": SOURCE_TYPES.get(item["stock_isin"][:2], "Other"),
                  "source_file": item.get("pdf_path") or md_rel}
        existing = db.stored_report(engine, item["stock_isin"], period_end, period_type)
        if existing and existing["source_file"] != report["source_file"] and not args.force:
            base["report_id"] = existing["report_id"]
            if same_values(fin, existing):
                log(f"  period already stored from {existing['source_file']} with the same values; not overwritten")
            else:
                checks.append({"check": "conflict", "detail": f"report_id {existing['report_id']} "
                               f"({existing['source_file']}) has different values; kept it (use --force to overwrite)"})
                status = "needs_review"
            report = None

    for col in ("revenue", "ebitda", "ebit", "net_income", "total_assets", "total_equity", "roe"):
        if fin.get(col) is not None:
            log(f"    {col:<14} {fin[col]:>22,}")
    for c in checks:
        log(f"    ! {c['check']}: {c['detail']}")

    extraction = {**base, "status": status, "scope": scope, "unit_scale": unit_scale,
                  "items": items_json, "checks": checks or None}
    if args.dry_run:
        log(f"  dry run: would write {'reports/financials/valuation_ratios + ' if report else ''}"
            f"report_extractions ({status})")
        return status
    try:
        report_id = db.load(engine, report, fin if report else None, val if report else None, extraction)
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc
    log(f"  saved: {status}" + (f", report_id {report_id}" if report_id else ", no report row (see checks)"))
    return status


def resolve_inputs(engine, args) -> list[dict]:
    if not args.file:
        if not db.table_exists(engine, "report_files"):
            sys.exit("Table report_files does not exist: run dataPipeline/convert/docling.py first.")
        return db.list_inputs(engine, isins=args.isin)
    rels = []
    for f in args.file:
        p = Path(f).resolve()
        try:
            rels.append(p.relative_to(ROOT).as_posix())
        except ValueError:
            sys.exit(f"{f} is not inside the repository ({ROOT})")
    known = {r["markdown_path"]: r for r in db.list_inputs(engine, markdown_paths=rels, any_category=True)} \
        if db.table_exists(engine, "report_files") else {}
    out = []
    for rel in rels:
        if rel in known:
            out.append(known[rel])
            continue
        if not args.isin or len(args.isin) != 1:
            sys.exit(f"{rel} is not a converted file with a known company (no matching report_files + "
                     f"downloaded_files rows): give its company with --isin ISIN")
        comp = db.company(engine, args.isin[0])
        if comp is None:
            sys.exit(f"ISIN {args.isin[0]} is not in stocks")
        out.append({**comp, "markdown_path": rel, "pdf_path": None, "fiscal_year": None})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract financial statements from Markdown reports into SQL.")
    parser.add_argument("--isin", nargs="+", help="only these companies")
    parser.add_argument("--file", nargs="+", help="only these Markdown files (paths in the repo)")
    parser.add_argument("--dry-run", action="store_true", help="extract and check, write nothing to the database")
    parser.add_argument("--force", action="store_true",
                        help="re-extract files already done and overwrite periods stored from other files")
    parser.add_argument("--limit", type=int, help="stop after this many files")
    args = parser.parse_args()
    start_run_log("extract")

    log(f"[extract] connecting to db ({db.safe_url()})")
    engine = db.get_engine()
    try:
        if args.dry_run:
            tracked = db.table_exists(engine, "report_extractions")
        else:
            db.ensure_tables(engine)
            tracked = True
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Database unavailable ({db.safe_url()}): {exc}")

    inputs = resolve_inputs(engine, args)[: args.limit or None]
    log(f"Found {len(inputs)} Markdown report(s){' (--dry-run)' if args.dry_run else ''}"
        f"{' (--force)' if args.force else ''}")
    model = Model()
    counts = {"extracted": 0, "needs_review": 0, "skipped": 0, "unchanged": 0, "failed": 0}
    for idx, item in enumerate(inputs, 1):
        log(f"[{idx}/{len(inputs)}] {item['markdown_path']} ({item.get('name') or item['stock_isin']})")
        started = time.time()
        try:
            outcome = process(engine, item, args, model, tracked)
        except StageError as err:
            counts["failed"] += 1
            log(f"  FAILED at stage '{err.stage}': {err}")
            if not args.dry_run:
                md_hash = None
                try:
                    md_hash = sha256_file(ROOT / item["markdown_path"])
                except Exception:  # noqa: BLE001
                    pass
                db.log_error(engine, item["markdown_path"], md_hash, err.stage, err.original)
                if md_hash:
                    db.record(engine, {"markdown_path": item["markdown_path"], "markdown_hash": md_hash,
                                       "extractor_version": EXTRACTOR_VERSION, "stock_isin": item["stock_isin"],
                                       "status": "failed", "error": f"{err.stage}: {err}", "model": model.name})
            continue
        counts[outcome] += 1
        if outcome == "unchanged":
            log("  already extracted (same file content and extractor version), skipped")
        else:
            log(f"  {outcome} in {time.time() - started:.1f}s")

    log("Done: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()))
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
