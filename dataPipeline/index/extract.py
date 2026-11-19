"""Step 1 of plan v1 (parts_alternatives_plan.md, section 2): extract the specs of every datasheet.

    python -m dataPipeline.index.extract run                  # every datasheet whose type has a reviewed spec list
    python -m dataPipeline.index.extract run mosfet_n --limit 20 --drafts   # pilot on one type, draft list allowed
    python -m dataPipeline.index.extract show goodark/2n7000.md   # what was extracted from one datasheet
    python -m dataPipeline.index.extract report               # per type: extracted, parts, values, unverified

One LLM call per datasheet. Input: the datasheet's spec pages (tape-and-reel, soldering and curve pages
dropped) and the spec list of its type (from Step 0's `type`). The model answers compact lines
(`key | value | unit | page | raw | conditions`); code turns them into the full value format
(values.py), converts units to the spec list's unit and runs the two checks: the raw text is on the cited
page, and min <= max. A value that fails is kept with `verified: false` and its issues.

The result goes into the datasheet's index-cache file (index_cache/<supplier>/<file_hash>.json) under
`extraction`. A datasheet is skipped when its cache already has an extraction of the same EXTRACTOR_VERSION,
type and spec-list version, so a run can be stopped and resumed, and changing a spec list (raise its
`version`) re-extracts only that type. `load_cache` then loads the cache into Postgres (catalog_parts).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python dataPipeline/index/extract.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataPipeline.index.common import (  # noqa: E402
    CACHE_DIR, MARKDOWN_DIR, ROOT, LLMError, OllamaJSON, file_sha256, find_datasheets, read_cache,
    split_pages, write_cache,
)
from dataPipeline.index.spec_lists import (  # noqa: E402
    SPEC_LIST_DIR, load_spec_list, spec_list_path, spec_pages, validate,
)
from dataPipeline.index.values import check_value, parse_line, show  # noqa: E402

EXTRACTOR_VERSION = "1"   # bump when the prompt or the code changes in a way that needs a re-run
EXTRACT_PROMPT = (Path(__file__).parent / "prompts" / "extract.txt").read_text(encoding="utf-8")
DEFAULT_MODEL = "qwen3:30b-a3b"
MAX_PARTS = 1000          # a family datasheet with more rows than this is cut (and reported)


# ------------------------------------------------------------------ extract one datasheet
def spec_list_lines(spec_list: dict) -> str:
    """The spec list as the prompt shows it: key | label | unit | stat | conditions | also printed as."""
    lines = ["key | label | unit | stat | conditions | also printed as"]
    for key, s in (spec_list.get("specs") or {}).items():
        aliases = ", ".join((s.get("aliases") or [])[:6])
        lines.append(f"{key} | {s.get('label') or key} | {s.get('unit') or ''} | {s.get('stat') or 'value'} | "
                     f"{s.get('conditions') or ''} | {aliases}")
    return "\n".join(lines)


def _clean_pages(markdown: str) -> dict[int, str]:
    return {n: re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", t) for n, t in split_pages(markdown).items()}


def _parse_lines(lines, specs: dict, pages: dict, dropped: Counter) -> dict:
    out = {}
    for line in lines if isinstance(lines, list) else []:
        if isinstance(line, dict):  # tolerate {"key":..., "value":...} objects instead of a line
            line = " | ".join(str(line.get(k, "")) for k in ("key", "value", "unit", "page", "raw", "conditions"))
        parsed = parse_line(str(line), {})
        if not parsed:
            continue
        key = parsed[0]
        if key not in specs:
            dropped[key] += 1
            continue
        key, value = parse_line(str(line), specs[key])
        out[key] = check_value(value, pages)
    return out


def extract(markdown: str, spec_list: dict, llm, max_chars: int = 48000) -> dict:
    """extract(markdown, spec_list) -> {"parts": [{part_number, family_row, ordering_page, specs}], "stats": ...}
    (plan section 13, rule 3). `llm(system, user)` returns an LLMAnswer; Ollama in production, a fake in tests."""
    specs = spec_list.get("specs") or {}
    user = (f"Component type: {spec_list.get('name') or spec_list.get('type')}\n\nSpec list:\n"
            f"{spec_list_lines(spec_list)}\n\nDatasheet:\n{spec_pages(markdown, max_chars)}")
    answer = llm(EXTRACT_PROMPT, user)
    data = answer.data if isinstance(answer.data, dict) else {}
    pages = _clean_pages(markdown)
    dropped: Counter = Counter()
    common = _parse_lines(data.get("common"), specs, pages, dropped)

    merged: dict = {}
    raw_parts = [p for p in (data.get("parts") or []) if isinstance(p, dict)]
    for p in raw_parts[:MAX_PARTS]:
        pn = " ".join(str(p.get("part_number") or "").split())[:80]
        row = " ".join(str(p.get("family_row") or "").split())[:120]
        try:
            ordering_page = int(p.get("ordering_page") or 0) or None
        except (TypeError, ValueError):
            ordering_page = None
        values = _parse_lines(p.get("values"), specs, pages, dropped)
        ident = (pn.upper(), row.lower())
        part = merged.setdefault(ident, {"part_number": pn, "family_row": row, "ordering_page": ordering_page,
                                         "specs": {}})
        part["specs"].update(values)
    if not merged and common:
        merged[("", "")] = {"part_number": "", "family_row": "", "ordering_page": None, "specs": {}}
    parts = []
    for part in merged.values():
        part["specs"] = {**common, **part["specs"]}
        if part["specs"]:
            parts.append(part)

    n_values = sum(len(p["specs"]) for p in parts)
    unverified = sum(1 for p in parts for v in p["specs"].values() if not v.get("verified"))
    coverage = (sum(len(p["specs"]) for p in parts) / (len(parts) * len(specs))) if parts and specs else 0
    stats = {"parts": len(parts), "values": n_values, "unverified": unverified, "coverage": round(coverage, 2),
             "cut": max(0, len(raw_parts) - MAX_PARTS), "unknown_keys": dict(dropped.most_common(10)),
             "seconds": round(answer.seconds, 2), "tokens_in": answer.tokens_in, "tokens_out": answer.tokens_out}
    return {"parts": parts, "stats": stats}


# ------------------------------------------------------------------ which datasheets
def load_spec_lists(types=None, drafts: bool = False) -> dict:
    """{type: spec_list} for the named types, or every spec_lists/<type>.yaml. Only `status: reviewed` lists,
    unless drafts=True (pilot). Lists with errors are left out (printed)."""
    paths = [spec_list_path(t) for t in types] if types else sorted(
        p for p in SPEC_LIST_DIR.glob("*.yaml") if not p.name.endswith(".draft.yaml"))
    out = {}
    for path in paths:
        if not path.exists():
            print(f"  ! {path.name}: no spec list (draft one with `spec_lists draft {path.stem}`)", file=sys.stderr)
            continue
        sl = load_spec_list(path)
        errors, _ = validate(sl)
        if errors:
            print(f"  ! {path.name}: {len(errors)} error(s), skipped (run `spec_lists check {path.stem}`)",
                  file=sys.stderr)
            continue
        if sl.get("status") != "reviewed" and not drafts:
            print(f"  {path.name}: status {sl.get('status')!r}, skipped (review it, or use --drafts for a pilot)")
            continue
        out[sl["type"]] = sl
    return out


def is_current(extraction: dict, spec_list: dict) -> bool:
    return (bool(extraction) and extraction.get("version") == EXTRACTOR_VERSION
            and extraction.get("type") == spec_list.get("type")
            and extraction.get("spec_list_version") == spec_list.get("version"))


# ------------------------------------------------------------------ run
def cmd_run(args, llm=None) -> int:
    spec_lists = load_spec_lists(args.types, args.drafts)
    if not spec_lists:
        print("No spec list to extract with. Draft and review them first: python -m dataPipeline.index.spec_lists")
        return 1
    datasheets = find_datasheets(Path(args.input), args.supplier)
    per_type, todo = Counter(), []
    for ds in datasheets:
        cache = read_cache(ds.cache_path)
        sl = spec_lists.get(cache.get("type"))
        if not sl:
            continue
        per_type[sl["type"]] += 1
        if args.force or not is_current(cache.get("extraction") or {}, sl):
            todo.append((ds, sl))
    total = sum(per_type.values())
    already = total - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(spec_lists)} spec list(s): " + ", ".join(f"{k} v{s['version']}" for k, s in spec_lists.items()))
    print(f"{total} datasheets of these types, {already} already extracted, "
          f"{len(todo)} to extract with {args.workers} parallel request(s)")
    if not todo:
        if not total:
            print("No datasheet has one of these types. Run Step 0 first (classify run / group / report).")
        return 0
    if llm is None:
        llm = OllamaJSON("specExtractionModel", DEFAULT_MODEL)
        llm.check_model()
        print(f"model: {llm.model} {llm.options}")

    lock = threading.Lock()
    st = Counter()
    started = last_print = time.monotonic()

    def work(ds, sl):
        markdown = ds.path.read_text(encoding="utf-8", errors="replace")
        try:
            result = extract(markdown, sl, llm, args.max_chars)
        except LLMError as exc:
            cache = read_cache(ds.cache_path)
            cache["extraction_error"] = {"at": _now(), "type": sl["type"], "spec_list_version": sl["version"],
                                         "error": str(exc)[:500]}
            write_cache(ds.cache_path, cache)
            raise
        cache = read_cache(ds.cache_path)  # re-read: another step may have written meanwhile
        cache.update(file=ds.rel, supplier=ds.supplier, file_hash=ds.file_hash)
        cache.pop("extraction_error", None)
        cache["extraction"] = {"version": EXTRACTOR_VERSION, "type": sl["type"], "spec_list_version": sl["version"],
                               "spec_list_status": sl.get("status"), "model": getattr(llm, "model", "?"),
                               "at": _now(), **result}
        write_cache(ds.cache_path, cache)
        return ds, result["stats"]

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, ds, sl) for ds, sl in todo]
        try:
            for fut in cf.as_completed(futures):
                try:
                    ds, s = fut.result()
                except (LLMError, OSError) as exc:
                    with lock:
                        st["failed"] += 1
                    print(f"  ! {exc}", file=sys.stderr)
                    continue
                with lock:
                    st["done"] += 1
                    for k in ("parts", "values", "unverified", "seconds", "tokens_in", "tokens_out"):
                        st[k] += s[k]
                if args.verbose:
                    print(f"  {ds.rel}: {s['parts']} part(s), {s['values']} values, {s['unverified']} unverified, "
                          f"coverage {s['coverage']:.0%}, {s['seconds']}s, {s['tokens_out']} tokens out"
                          + (f", unknown keys {list(s['unknown_keys'])}" if s["unknown_keys"] else "")
                          + (f", {s['cut']} rows cut" if s["cut"] else ""))
                now = time.monotonic()
                n = st["done"] + st["failed"]
                if now - last_print >= 30 or n == len(todo):
                    last_print = now
                    rate = n / (now - started)
                    eta = (len(todo) - n) / rate if rate else 0
                    print(f"  .. {n}/{len(todo)} ({st['failed']} failed), {rate * 3600:.0f}/h, ETA {eta / 60:.0f} min",
                          flush=True)
        except KeyboardInterrupt:
            print("Stopped. Extracted datasheets are kept; run the same command again to continue.")
            for f in futures:
                f.cancel()
            return 1

    done = st["done"] or 1
    wall = time.monotonic() - started
    print(f"\nExtracted {st['done']}, failed {st['failed']} in {wall / 60:.1f} min: {st['parts']} parts, "
          f"{st['values']} values, {st['unverified']} unverified ({st['unverified'] / max(st['values'], 1):.0%})")
    print(f"{wall / done:.1f} s per datasheet with {args.workers} worker(s); one request: {st['seconds'] / done:.1f} s, "
          f"{st['tokens_in'] / done:.0f} tokens in, {st['tokens_out'] / done:.0f} out")
    if total > done:
        print(f"Estimate for all {total} datasheets of these types at this speed: {total * wall / done / 3600:.1f} h")
    print("Next: spot-check with `extract show <file>`, then `extract report` and `python -m dataPipeline.index.load_cache`")
    return 1 if st["failed"] else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ show
def find_cache(target: str, input_dir: Path) -> Path | None:
    """A markdown path or a file-hash prefix -> its cache file. The path may be absolute, relative to the
    current folder (markdowns/goodark/x.md) or to the input folder (goodark/x.md, also /goodark/x.md)."""
    rel_target = target.replace("\\", "/").lstrip("/")
    if rel_target.startswith(input_dir.name + "/"):
        rel_target = rel_target[len(input_dir.name) + 1:]
    for p in (Path(target), input_dir / rel_target):
        if p.is_file() and p.suffix == ".md":
            rel = p.resolve().relative_to(input_dir.resolve()) if p.resolve().is_relative_to(input_dir.resolve()) \
                else None
            supplier = rel.parts[0] if rel and len(rel.parts) > 1 else p.parent.name
            path = CACHE_DIR / supplier / f"{file_sha256(p)}.json"
            if path.exists():
                return path
            break  # markdown changed since it was classified: look the cache up by its file name below
    if re.fullmatch(r"[0-9a-f]{6,64}", target):
        hits = sorted(CACHE_DIR.glob(f"*/{target}*.json"))
        if len(hits) == 1:
            return hits[0]
    # by the `file` stored in the cache files (also finds the cache when the markdown is not on this PC)
    name = rel_target.lower()
    supplier = name.split("/", 1)[0] if "/" in name else "*"
    hits = [c for c in sorted(CACHE_DIR.glob(f"{supplier}/*.json"))
            if str(read_cache(c).get("file", "")).lower() in (name, name + ".md")]
    hits.sort(key=lambda c: c.stat().st_mtime)
    return hits[-1] if hits else None


def cmd_show(args) -> int:
    path = find_cache(args.target, Path(args.input))
    cache = read_cache(path) if path else {}
    if not cache:
        print(f"No index-cache file for {args.target!r}. Give the markdown path relative to {args.input} "
              f"(e.g. goodark/x.md), a path to the .md file, or a file-hash prefix; the datasheet must be "
              f"classified first (classify run).")
        return 1
    ex = cache.get("extraction")
    print(f"{cache.get('file')}  type {cache.get('type')}  ({path.relative_to(ROOT) if path.is_relative_to(ROOT) else path})")
    if cache.get("extraction_error"):
        e = cache["extraction_error"]
        print(f"  last extraction failed {e['at']}: {e['error']}")
    if not ex:
        print("  not extracted yet")
        return 1
    s = ex["stats"]
    print(f"  extracted {ex['at']} with {ex['model']}, spec list v{ex['spec_list_version']} ({ex.get('spec_list_status')}), "
          f"{s['parts']} part(s), {s['values']} values, {s['unverified']} unverified, {s['seconds']}s")
    if s.get("unknown_keys"):
        print(f"  keys not in the spec list (dropped): {s['unknown_keys']}")
    for i, part in enumerate(ex["parts"]):
        if args.limit and i >= args.limit:
            print(f"  ... {len(ex['parts']) - args.limit} more part(s) (--limit 0 shows all)")
            break
        name = part["part_number"] or f"[{part['family_row']}]"
        print(f"\n  {name}" + (f"  (family row {part['family_row']})" if part["part_number"] and part["family_row"] else "")
              + (f"  ordering table p{part['ordering_page']}" if part.get("ordering_page") else ""))
        for key, v in part["specs"].items():
            flag = "" if v.get("verified") else "  ! " + "; ".join(v.get("issues") or [])
            cond = ", ".join(f"{k}={x}" for k, x in (v.get("conditions") or {}).items())
            print(f"    {key:30.30} {show(v):24.24} p{v.get('page') or '?':<3} raw {v.get('raw')!r:18.18}"
                  f"{'  @ ' + cond if cond else ''}{flag}")
    return 0


# ------------------------------------------------------------------ report
def cmd_report(args) -> int:
    spec_lists = load_spec_lists(None, drafts=True)
    rows, per = [], defaultdict(Counter)
    for ds in find_datasheets(Path(args.input), args.supplier):
        cache = read_cache(ds.cache_path)
        t = cache.get("type")
        if t not in spec_lists:
            continue
        sl, ex, err = spec_lists[t], cache.get("extraction") or {}, cache.get("extraction_error")
        c = per[t]
        c["datasheets"] += 1
        if is_current(ex, sl):
            s = ex["stats"]
            c["extracted"] += 1
            for k in ("parts", "values", "unverified"):
                c[k] += s[k]
            c["coverage_x100"] += round(s["coverage"] * 100)
            state = "extracted"
        elif ex:
            c["outdated"] += 1
            state = "outdated"
        elif err:
            c["failed"] += 1
            state = "failed"
        else:
            state = "todo"
        s = ex.get("stats") or {}
        rows.append((ds.supplier, ds.rel, t, state, s.get("parts", ""), s.get("values", ""), s.get("unverified", ""),
                     s.get("coverage", ""), (err or {}).get("error", "") if state == "failed" else ""))
    if not per:
        print("No datasheet of a type with a spec list. Run Step 0 and draft spec lists first.")
        return 1
    print(f"{'type':28} {'list':10} {'datasheets':>10} {'extracted':>9} {'outdated':>8} {'failed':>6} {'parts':>7} "
          f"{'values':>8} {'unverified':>10} {'coverage':>8}")
    for t, c in sorted(per.items(), key=lambda kv: -kv[1]["datasheets"]):
        sl = spec_lists[t]
        unv = f"{c['unverified'] / c['values']:.0%}" if c["values"] else "-"
        cov = f"{c['coverage_x100'] / c['extracted']:.0f}%" if c["extracted"] else "-"
        print(f"{t:28.28} {sl.get('status', '?')[:6] + ' v' + str(sl.get('version')):10} {c['datasheets']:>10} "
              f"{c['extracted']:>9} {c['outdated']:>8} {c['failed']:>6} {c['parts']:>7} {c['values']:>8} {unv:>10} {cov:>8}")
    out = CACHE_DIR / "extract_report.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["supplier", "file", "type", "state", "parts", "values", "unverified", "coverage", "error"])
        w.writerows(sorted(rows))
    print(f"\ncoverage = share of the spec list's specs found per part. Per datasheet: {out}")
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="extract specs with the LLM into the index cache")
    p.add_argument("types", nargs="*", help="only these types (default: every spec list in spec_lists/)")
    p.add_argument("--drafts", action="store_true", help="also use spec lists with status draft (pilot)")
    p.add_argument("--limit", type=int, help="extract at most N datasheets (pilot)")
    p.add_argument("--workers", type=int, default=1,
                   help="parallel requests (also set OLLAMA_NUM_PARALLEL on the Ollama server)")
    p.add_argument("--max-chars", type=int, default=48000, help="characters of spec pages sent per datasheet")
    p.add_argument("--force", action="store_true", help="extract again, also datasheets already extracted")
    p.add_argument("-v", "--verbose", action="store_true", help="print every datasheet")
    p = sub.add_parser("show", help="print what was extracted from one datasheet")
    p.add_argument("target", help="markdown path (relative to --input or absolute) or a file-hash prefix")
    p.add_argument("--limit", type=int, default=20, help="parts to print (0 = all, default 20)")
    p = sub.add_parser("report", help="extraction state and quality per type, extract_report.csv")
    for p in sub.choices.values():
        p.add_argument("--input", default=str(MARKDOWN_DIR), help="folder with <supplier>/**/*.md (default markdowns/)")
        if p.prog.split()[-1] != "show":
            p.add_argument("--supplier", nargs="+", help="only these supplier folders")
    args = parser.parse_args(argv)

    from helpers.run_log import start_run_log
    start_run_log(f"index_extract_{args.command}")
    try:
        return {"run": cmd_run, "show": cmd_show, "report": cmd_report}[args.command](args)
    except LLMError as exc:  # Ollama not running, model not pulled, ...
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
