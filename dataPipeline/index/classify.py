"""Step 0 of plan v1 (parts_alternatives_plan.md): classify every markdown datasheet by component type.

    python -m dataPipeline.index.classify run      # LLM reads the first 1-2 pages of each datasheet -> type label
    python -m dataPipeline.index.classify group    # LLM merges synonym labels into a type list (type_list.yaml)
    python -m dataPipeline.index.classify report   # apply the (edited) type list, count datasheets per type

1. `run` writes `classification` into index_cache/<supplier>/<file_hash>.json. It skips files already
   classified with the same file hash and classifier version, so it can be stopped and resumed. It
   prints the real seconds and tokens per datasheet (the plan's speed pilot).
2. `group` turns the free-text labels ("N-channel MOSFET", "N-channel enhancement MOSFET", ...) into
   type keys (mosfet_n) and writes index_cache/type_list.yaml. Check and edit that file once.
3. `report` reads type_list.yaml, writes each datasheet's `type` into its cache file, and prints how many
   datasheets each type has, so the first ~10 types can be chosen.
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

if __package__ in (None, ""):  # allow `python dataPipeline/index/classify.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataPipeline.index.common import (  # noqa: E402
    CACHE_DIR, MARKDOWN_DIR, LLMError, OllamaJSON, find_datasheets, first_pages, read_cache, write_cache,
)

CLASSIFIER_VERSION = "1"   # bump when the prompt or the input changes in a way that needs a re-run
CLASSIFY_PROMPT = (Path(__file__).parent / "prompts" / "classify.txt").read_text(encoding="utf-8")
GROUP_PROMPT = (Path(__file__).parent / "prompts" / "group.txt").read_text(encoding="utf-8")
TYPE_LIST = CACHE_DIR / "type_list.yaml"
NOT_DATASHEET = "_not_a_datasheet"   # type key for documents the classifier says are not datasheets
UNCLASSIFIED = "_unclassified"       # labels not (yet) in the type list
DEFAULT_MODEL = "qwen3:30b-a3b"
CONFIDENCE = ("high", "medium", "low")


# ------------------------------------------------------------------ classify one datasheet
def classify(markdown: str, llm, pages: int = 2, max_chars: int = 12000) -> dict:
    """classify(markdown) -> {is_datasheet, label, family, part_numbers, confidence, ...} (plan rule 3).

    `llm(system, user)` returns an LLMAnswer; Ollama in production, a fake in tests."""
    answer = llm(CLASSIFY_PROMPT, first_pages(markdown, pages, max_chars))
    d = answer.data
    label = " ".join(str(d.get("type") or "").split())[:120]
    confidence = str(d.get("confidence") or "").lower()
    parts = d.get("part_numbers") or []
    return {
        "is_datasheet": d.get("is_datasheet") is not False,
        "label": label,
        "family": bool(d.get("family")),
        "part_numbers": [str(p)[:60] for p in parts[:5]] if isinstance(parts, list) else [],
        "confidence": confidence if confidence in CONFIDENCE else "low",
        "seconds": round(answer.seconds, 2),
        "tokens_in": answer.tokens_in,
        "tokens_out": answer.tokens_out,
    }


def normalise_label(label: str) -> str:
    """Spelling variants of one label become one ("N-Channel MOSFETs " -> "n-channel mosfet")."""
    text = " ".join(label.lower().replace("_", " ").split())
    return re.sub(r"(?<=[a-z]{3})s$", "", text)  # simple plural


# ------------------------------------------------------------------ run
def cmd_run(args, llm=None) -> int:
    datasheets = find_datasheets(Path(args.input), args.supplier)
    if not datasheets:
        print(f"No markdown datasheets in {args.input}/<supplier>/... (use --input)")
        return 1
    todo = [d for d in datasheets
            if args.force or read_cache(d.cache_path).get("classification", {}).get("version") != CLASSIFIER_VERSION]
    already = len(datasheets) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(datasheets)} datasheets in {args.input}, {already} already classified, "
          f"{len(todo)} to classify with {args.workers} parallel request(s)")
    if not todo:
        return 0
    if llm is None:
        llm = OllamaJSON("datasheetClassifierModel", DEFAULT_MODEL)
        llm.check_model()
        print(f"model: {llm.model} {llm.options}")

    lock = threading.Lock()
    stats = {"done": 0, "failed": 0, "seconds": 0.0, "tokens_in": 0, "tokens_out": 0}
    started = last_print = time.monotonic()

    def work(ds):
        markdown = ds.path.read_text(encoding="utf-8", errors="replace")
        result = classify(markdown, llm, args.pages, args.max_chars)
        cache = read_cache(ds.cache_path)
        cache.update(file=ds.rel, supplier=ds.supplier, file_hash=ds.file_hash)
        cache["classification"] = {"version": CLASSIFIER_VERSION, "model": getattr(llm, "model", "?"),
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result}
        write_cache(ds.cache_path, cache)
        return ds, result

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, d) for d in todo]
        try:
            for fut in cf.as_completed(futures):
                try:
                    ds, r = fut.result()
                except (LLMError, OSError) as exc:
                    with lock:
                        stats["failed"] += 1
                    print(f"  ! {exc}", file=sys.stderr)
                    continue
                with lock:
                    stats["done"] += 1
                    for k in ("seconds", "tokens_in", "tokens_out"):
                        stats[k] += r[k]
                if args.verbose:
                    print(f"  {ds.rel}: {r['label']!r}{' (family)' if r['family'] else ''}"
                          f"{'' if r['is_datasheet'] else ' [not a datasheet]'} {r['confidence']}, {r['seconds']}s")
                now = time.monotonic()
                if now - last_print >= 30 or stats["done"] + stats["failed"] == len(todo):
                    last_print = now
                    n = stats["done"] + stats["failed"]
                    rate = n / (now - started)
                    eta = (len(todo) - n) / rate if rate else 0
                    print(f"  .. {n}/{len(todo)} ({stats['failed']} failed), {rate * 3600:.0f}/h, "
                          f"ETA {eta / 60:.0f} min", flush=True)
        except KeyboardInterrupt:
            print("Stopped. Already classified datasheets are kept; run again to continue.")
            for f in futures:
                f.cancel()
            return 1

    done = stats["done"] or 1
    wall = time.monotonic() - started
    print(f"\nClassified {stats['done']}, failed {stats['failed']} in {wall / 60:.1f} min "
          f"({wall / done:.1f} s per datasheet with {args.workers} worker(s); "
          f"one request: {stats['seconds'] / done:.1f} s, {stats['tokens_in'] / done:.0f} tokens in, "
          f"{stats['tokens_out'] / done:.0f} out)")
    if len(datasheets) > done:
        print(f"Estimate for all {len(datasheets)} datasheets at this speed: "
              f"{len(datasheets) * wall / done / 3600:.1f} h")
    print("Next: python -m dataPipeline.index.classify group")
    return 1 if stats["failed"] else 0


# ------------------------------------------------------------------ group
def load_type_list(path: Path | None = None) -> dict:
    """{key: {name, labels: [...], ...}} from type_list.yaml (empty if there is none yet)."""
    import yaml

    path = path or TYPE_LIST
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): dict(v or {}) for k, v in (data.get("types") or {}).items()}


def save_type_list(types: dict, counts: Counter, path: Path | None = None) -> None:
    import yaml

    path = path or TYPE_LIST
    order = sorted(types, key=lambda k: (k.startswith("_"), -counts.get(k, 0), k))
    body = {k: {"name": types[k].get("name", k), "datasheets": counts.get(k, 0),
                "labels": sorted(set(types[k].get("labels") or []))} for k in order}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Step 0 type list (plan v1). Check it once and edit freely: rename keys and names, move labels\n"
        "# between types, merge types. `classify report` applies it; `classify group` only adds labels that\n"
        "# are not in it yet. `datasheets` is a count, written by the tools.\n"
        + yaml.safe_dump({"types": body}, allow_unicode=True, sort_keys=False, width=110),
        encoding="utf-8")


def current_labels(input_dir: Path, suppliers=None) -> tuple[Counter, dict, int]:
    """(datasheets per normalised label, normalised -> most common spelling, unclassified count)."""
    counts, spellings, missing = Counter(), defaultdict(Counter), 0
    for ds in find_datasheets(input_dir, suppliers):
        c = read_cache(ds.cache_path).get("classification")
        if not c:
            missing += 1
            continue
        label = c["label"] if c.get("is_datasheet", True) and c.get("label") else NOT_DATASHEET
        norm = label if label == NOT_DATASHEET else normalise_label(label)
        counts[norm] += 1
        spellings[norm][label] += 1
    return counts, {n: s.most_common(1)[0][0] for n, s in spellings.items()}, missing


def label_index(types: dict) -> dict:
    return {normalise_label(lbl): key for key, t in types.items() for lbl in (t.get("labels") or [])}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "type"


def group_labels(labels: list[str], types: dict, llm, chunk: int = 120, log=print) -> dict:
    """Assign `labels` (spellings) to type keys with the LLM, chunk by chunk. Returns the updated types."""
    types = {k: dict(v, labels=list(v.get("labels") or [])) for k, v in types.items()}
    for start in range(0, len(labels), chunk):
        batch = labels[start:start + chunk]
        for attempt in (1, 2):
            existing = "\n".join(f"{k}: {t.get('name', k)}" for k, t in types.items() if not k.startswith("_"))
            user = ("Existing types (key: name):\n" + (existing or "(none yet)") +
                    "\n\nLabels to assign:\n" + "\n".join(batch))
            try:
                data = llm(GROUP_PROMPT, user).data
            except LLMError as exc:
                log(f"  ! grouping call failed: {exc}")
                data = {}
            for t in data.get("new_types") or []:
                key = slug(str(t.get("key") or ""))
                if key and key not in types:
                    types[key] = {"name": str(t.get("name") or key), "labels": []}
            wanted = {normalise_label(b): b for b in batch}
            for a in data.get("assign") or []:
                norm = normalise_label(str(a.get("label") or ""))
                key = slug(str(a.get("type") or ""))
                if norm in wanted and key:
                    types.setdefault(key, {"name": wanted[norm], "labels": []})["labels"].append(wanted.pop(norm))
            if not wanted:
                break
            if attempt == 1:
                log(f"  {len(wanted)} label(s) not assigned, asking again")
                batch = list(wanted.values())
        for left in wanted.values():  # still unassigned: a type of its own, to be checked by hand
            types.setdefault(slug(left), {"name": left, "labels": []})["labels"].append(left)
        log(f"  .. {min(start + chunk, len(labels))}/{len(labels)} labels assigned, {len(types)} types")
    return types


def cmd_group(args, llm=None) -> int:
    counts, spelling, missing = current_labels(Path(args.input), args.supplier)
    if not counts:
        print("No classified datasheets yet. Run: python -m dataPipeline.index.classify run")
        return 1
    types = load_type_list()
    known = label_index(types)
    new = sorted((n for n in counts if n not in known and n != NOT_DATASHEET), key=lambda n: -counts[n])
    print(f"{sum(counts.values())} classified datasheets, {len(counts)} distinct labels, "
          f"{len(new)} not in the type list yet" + (f" ({missing} datasheets not classified yet)" if missing else ""))
    if new:
        if llm is None:
            llm = OllamaJSON("typeGroupingModel", DEFAULT_MODEL)
            llm.check_model()
        types = group_labels([spelling[n] for n in new], types, llm, args.chunk)
    if NOT_DATASHEET in counts:
        types.setdefault(NOT_DATASHEET, {"name": "Not a datasheet (application note, brochure, ...)", "labels": []})
    save_type_list(types, type_counts(counts, label_index(types)))
    print(f"Wrote {TYPE_LIST} with {len(types)} types. Check and edit it, then run: "
          f"python -m dataPipeline.index.classify report")
    return 0


# ------------------------------------------------------------------ report
def type_counts(label_counts: Counter, index: dict) -> Counter:
    out = Counter()
    for norm, n in label_counts.items():
        out[NOT_DATASHEET if norm == NOT_DATASHEET else index.get(norm, UNCLASSIFIED)] += n
    return out


def cmd_report(args) -> int:
    types = load_type_list()
    if not types:
        print(f"No type list yet ({TYPE_LIST}). Run: python -m dataPipeline.index.classify group")
        return 1
    index = label_index(types)
    per_type, per_supplier, families = Counter(), defaultdict(Counter), Counter()
    unmapped, missing, changed, rows = Counter(), 0, 0, []
    for ds in find_datasheets(Path(args.input), args.supplier):
        cache = read_cache(ds.cache_path)
        c = cache.get("classification")
        if not c:
            missing += 1
            continue
        if not c.get("is_datasheet", True) or not c.get("label"):
            key = NOT_DATASHEET
        else:
            key = index.get(normalise_label(c["label"]), UNCLASSIFIED)
            if key == UNCLASSIFIED:
                unmapped[c["label"]] += 1
        if cache.get("type") != key:
            cache["type"] = key
            write_cache(ds.cache_path, cache)
            changed += 1
        per_type[key] += 1
        per_supplier[key][ds.supplier] += 1
        families[key] += bool(c.get("family"))
        rows.append((ds.supplier, ds.rel, key, c.get("label", ""), c.get("family"), c.get("confidence")))

    total = sum(per_type.values())
    print(f"{total} classified datasheets ({missing} not classified yet), {changed} cache files updated\n")
    print(f"{'type':32} {'name':42} {'datasheets':>10} {'families':>8} {'suppliers':>9}")
    for key, n in per_type.most_common():
        name = types.get(key, {}).get("name", "labels not in type_list.yaml" if key == UNCLASSIFIED else key)
        print(f"{key:32.32} {str(name):42.42} {n:>10} {families[key]:>8} {len(per_supplier[key]):>9}")
    if unmapped:
        print(f"\n{sum(unmapped.values())} datasheets have labels that are not in the type list "
              f"(run `group` again, or add them by hand), e.g.: " +
              ", ".join(f"{lbl!r}" for lbl, _ in unmapped.most_common(8)))
    out = CACHE_DIR / "type_report.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["supplier", "file", "type", "label", "family", "confidence"])
        w.writerows(sorted(rows))
    print(f"\nPer datasheet: {out}")
    save_type_list(types, per_type)  # refresh the counts in the YAML
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "group", "report"):
        p = sub.add_parser(name)
        p.add_argument("--input", default=str(MARKDOWN_DIR),
                       help="folder with <supplier>/**/*.md (default markdowns/suppliers)")
        p.add_argument("--supplier", nargs="+", help="only these supplier folders")
        if name == "run":
            p.add_argument("--workers", type=int, default=1,
                           help="parallel requests (also set OLLAMA_NUM_PARALLEL on the Ollama server)")
            p.add_argument("--limit", type=int, help="classify at most N datasheets (pilot)")
            p.add_argument("--pages", type=int, default=2, help="pages read per datasheet (default 2)")
            p.add_argument("--max-chars", type=int, default=12000, help="characters sent per datasheet")
            p.add_argument("--force", action="store_true", help="classify again, also already classified files")
            p.add_argument("-v", "--verbose", action="store_true", help="print every result")
        if name == "group":
            p.add_argument("--chunk", type=int, default=120, help="labels per LLM call (default 120)")
    args = parser.parse_args(argv)

    from helpers.run_log import start_run_log
    start_run_log(f"index_classify_{args.command}")
    try:
        return {"run": cmd_run, "group": cmd_group, "report": cmd_report}[args.command](args)
    except LLMError as exc:  # Ollama not running, model not pulled, ...
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
