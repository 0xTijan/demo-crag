"""Spec lists per component type (plan v1, parts_alternatives_plan.md, "How a spec list is created").

    python -m dataPipeline.index.spec_lists list                    # types, datasheet counts, spec-list status
    python -m dataPipeline.index.spec_lists draft mosfet_n mlcc     # draft spec lists for these types
    python -m dataPipeline.index.spec_lists draft --top 10          # ... for the 10 types with most datasheets
    python -m dataPipeline.index.spec_lists draft --all             # ... for every type (rerun to resume)
    python -m dataPipeline.index.spec_lists check                   # validate every spec list (after editing)

Needs Step 0 first (`classify run`, `group`, `report`), because the samples are picked by the `type` that
`classify report` writes into each index-cache file.

`draft` works in two LLM steps:
1. inventory: for each of ~10 sample datasheets of the type (different suppliers, singles and families), the
   LLM lists every specification the datasheet gives (name, symbol, unit, min/typ/max, conditions). The
   result is cached in the datasheet's index-cache file (`spec_inventory`), so a second draft is fast.
2. merge: the LLM groups the items of all samples into specifications (synonyms merged), each with a key,
   unit, comparison rule and the ids of the items it covers. Code counts in how many samples each
   specification was found (`found_in`), keeps the ones found in at least --min-share of the samples
   (the specifications the datasheets have in common) under `specs`, and the rest under `rare_specs`.

The draft goes to spec_lists/<type>.yaml with `status: draft`. A person reviews it (rules, critical,
must_match, missing specs), sets `status: reviewed` and runs `check`.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python dataPipeline/index/spec_lists.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataPipeline.index.common import (  # noqa: E402
    MARKDOWN_DIR, ROOT, LLMError, OllamaJSON, compact_tables, find_datasheets, read_cache, split_pages, write_cache,
)

SPEC_LIST_DIR = ROOT / "spec_lists"
INVENTORY_VERSION = "2"   # bump when the inventory prompt or its input changes in a way that needs a re-run
INVENTORY_PROMPT = (Path(__file__).parent / "prompts" / "spec_inventory.txt").read_text(encoding="utf-8")
MERGE_PROMPT = (Path(__file__).parent / "prompts" / "spec_merge.txt").read_text(encoding="utf-8")
DEFAULT_MODEL = "qwen3:30b-a3b"

RULES = ("=", ">=", "<=", "≈", "contains", "info")
RULE_ALIASES = {"==": "=", "eq": "=", "equal": "=", "must_match": "=", "≥": ">=", "gte": ">=",
                "≤": "<=", "lte": "<=", "~": "≈", "~=": "≈", "approx": "≈", "near": "≈",
                "about": "≈", "range": "contains", "covers": "contains", "within": "contains", "": "info",
                "none": "info", "shown": "info"}
STATS = ("min", "typ", "max", "range", "value")
STATUSES = ("draft", "reviewed")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]*(@[a-z0-9_]+)?$")
MAX_ITEMS = 100

# Pages that hold no specifications to compare (unless they also hold ordering or spec tables).
SKIP_PAGE_RE = re.compile(
    r"tape and reel|taping|carrier tape|reel dimension|packing (?:quantity|information|specification)"
    r"|soldering (?:profile|condition)|reflow profile|recommended (?:land|pad|footprint|soldering)"
    r"|revision history|important notice|disclaimer|typical (?:characteristic|performance) curves?", re.I)
KEEP_PAGE_RE = re.compile(
    r"ordering|part number|absolute maximum|electrical characteristic|specification|features?\b", re.I)


# ------------------------------------------------------------------ inputs
def spec_pages(markdown: str, max_chars: int = 24000) -> str:
    """The pages that can hold specifications: page 1 always, others with tables and not only about packing,
    soldering or curves. Inline base64 images removed, cut at max_chars."""
    pages = split_pages(markdown)
    chosen = []
    for n in sorted(pages):
        text = compact_tables(re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", pages[n]))
        has_table = re.search(r"^\s*\|", text, re.M) is not None
        if n == min(pages) or (has_table and (not SKIP_PAGE_RE.search(text) or KEEP_PAGE_RE.search(text))):
            chosen.append(f"<!-- page: {n} -->\n{text}" if len(pages) > 1 else text)
    return "\n\n".join(chosen)[:max_chars]


def _short(value, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def inventory(markdown: str, llm, max_chars: int = 24000) -> dict:
    """inventory(markdown) -> {"items": [{name, symbol, unit, stat, conditions, example}], seconds, tokens}."""
    answer = llm(INVENTORY_PROMPT, spec_pages(markdown, max_chars))
    items, seen = [], set()
    for raw in answer.data.get("specs") or []:
        if not isinstance(raw, dict):
            continue
        item = {"name": re.sub(r"(?<=[a-z])\d+$", "", _short(raw.get("name"), 80)),  # "Resistance1" -> footnote
                "symbol": _short(raw.get("symbol"), 30), "unit": _short(raw.get("unit"), 20),
                "stat": _short(raw.get("stat"), 10).lower(), "conditions": _short(raw.get("conditions"), 120),
                "example": _short(raw.get("example"), 40)}
        if not item["name"] and not item["symbol"]:
            continue
        if item["stat"] not in STATS:
            item["stat"] = "value"
        ident = (item["name"].lower(), item["symbol"].lower(), item["stat"], item["conditions"].lower())
        if ident in seen:
            continue
        seen.add(ident)
        items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return {"items": items, "seconds": round(answer.seconds, 2), "tokens_in": answer.tokens_in,
            "tokens_out": answer.tokens_out}


# ------------------------------------------------------------------ samples
def datasheets_of_type(input_dir: Path, type_key: str, suppliers=None) -> list[tuple]:
    """[(Datasheet, cache)] whose cache `type` (written by `classify report`) is type_key."""
    out = []
    for ds in find_datasheets(input_dir, suppliers):
        cache = read_cache(ds.cache_path)
        if cache.get("type") == type_key:
            out.append((ds, cache))
    return out


def pick_samples(candidates: list[tuple], n: int) -> list[tuple]:
    """Up to n datasheets, spread over suppliers (round robin, biggest supplier first), alternating families
    and single parts within a supplier, high-confidence classifications first. Deterministic."""
    rank = {"high": 0, "medium": 1, "low": 2}
    by_supplier = defaultdict(lambda: {True: [], False: []})
    for ds, cache in candidates:
        c = cache.get("classification") or {}
        by_supplier[ds.supplier][bool(c.get("family"))].append((rank.get(c.get("confidence"), 3), ds.file_hash,
                                                                (ds, cache)))
    queues = {}
    for supplier, groups in by_supplier.items():
        fam, single = sorted(groups[True]), sorted(groups[False])
        merged = []
        for i in range(max(len(fam), len(single))):  # alternate: family, single, family, ...
            merged += [g[i][2] for g in (fam, single) if i < len(g)]
        queues[supplier] = merged
    order = sorted(queues, key=lambda s: (-len(queues[s]), s))
    picked = []
    while len(picked) < n and any(queues.values()):
        for s in order:
            if queues[s] and len(picked) < n:
                picked.append(queues[s].pop(0))
    return picked


# ------------------------------------------------------------------ merge
def item_lines(samples: list[dict]) -> str:
    """Numbered items for the merge prompt: D<sample>.<item>."""
    lines = []
    for d, s in enumerate(samples, 1):
        lines.append(f"Datasheet D{d} ({s['supplier']}, {'family' if s['family'] else 'single part'}):")
        for i, it in enumerate(s["items"], 1):
            lines.append(f"D{d}.{i} | {it['name']} | {it['symbol']} | {it['unit']} | {it['stat']} | "
                         f"{it['conditions']} | {it['example']}")
    return "\n".join(lines)


def norm_key(text: str) -> str:
    """'R(DS)on max @ VGS=10V' -> 'r_ds_on_max@vgs_10v'; keeps one '@'."""
    base, _, cond = str(text or "").lower().partition("@")
    clean = lambda t: re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:50]  # noqa: E731
    key = clean(base)
    return f"{key}@{clean(cond)}" if key and clean(cond) else key


def norm_rule(rule) -> str:
    r = str(rule or "").strip().lower()
    return r if r in RULES else RULE_ALIASES.get(r, "info")


# Condition variables that define a different specification, the same for every part of a type: gate drive
# (RDS(on) at VGS=10 V vs 4.5 V), temperature (leakage at TJ=25 vs 125 °C), frequency, pulse width. Any other
# condition in a key (IF=20 A, ID=10 A, VR=40 V, IPP=5 A, IT=1 mA) follows the part's own rating and differs
# between datasheets; left in the key, it splits one spec into one key per datasheet.
DEFINING_CONDITIONS = {"vgs", "vge", "tj", "tc", "ta", "tl", "f", "tp"}


def canonical_key(key: str) -> str:
    """Drop per-part conditions (and unit suffixes) from a key, keep defining ones:
    vf_max@if_20a_tj_125c -> vf_max@tj_125c; rds_on_max@vgs_10v_id_20a -> rds_on_max@vgs_10v;
    vc_max@ippm_5a -> vc_max; vwm_value@vrwm -> vwm_value; ir_max@tj_25c_ua -> ir_max@tj_25c."""
    base, _, cond = key.partition("@")
    if not cond:
        return key
    tokens, kept, i = cond.split("_"), [], 0
    while i < len(tokens):
        if tokens[i] in DEFINING_CONDITIONS and i + 1 < len(tokens) and tokens[i + 1][:1].isdigit():
            group = [tokens[i]]
            i += 1
            while i < len(tokens) and tokens[i][:1].isdigit():
                group.append(tokens[i])
                i += 1
                if re.search(r"[a-z]", group[-1]):  # "10v", "125c", "1mhz", "20us": the value ends here
                    break
            kept.append("_".join(group))
        else:
            i += 1
    return f"{base}@{'_'.join(kept)}" if kept else base


def _norm_name(text: str) -> str:
    return re.sub(r"[\s_()\[\].,:-]+", "", str(text or "").lower())


def _defining_ok(key: str, conditions: str) -> bool:
    """An item matches a key's defining conditions (vgs_4_5v -> 'vgs=4.5v' in 'VGS = 4.5 V, ID=10A')."""
    cond = str(conditions or "").lower().replace(" ", "").replace("°", "").replace("℃", "c").replace(",", ".")
    defining = key.partition("@")[2]
    if not defining:
        return True
    for m in re.finditer(r"(?:^|_)([a-z]+)_((?:\d+_)*\d+[a-z]*)", defining):
        var, value = m.group(1), m.group(2).replace("_", ".")
        if f"{var}={value}" not in cond:
            return False
    return True


def merge_specs(type_key: str, type_name: str, samples: list[dict], llm, min_share: float = 0.5) -> dict:
    """One LLM call groups the samples' items into specifications; code counts coverage and builds the
    spec-list dict (not yet written). Returns {"spec_list": ..., "stats": ...}."""
    user = (f"Component type: {type_name} ({type_key})\n{len(samples)} sample datasheets.\n\n"
            + item_lines(samples))
    answer = llm(MERGE_PROMPT, user)
    data = answer.data
    valid = {f"D{d}.{i}": (d, it) for d, s in enumerate(samples, 1) for i, it in enumerate(s["items"], 1)}
    used: set = set()
    specs: dict = {}
    for raw in data.get("specs") or []:
        if not isinstance(raw, dict):
            continue
        key = canonical_key(norm_key(raw.get("key") or raw.get("label")))
        if not key:
            continue
        members = [m for m in (str(x).strip() for x in raw.get("members") or []) if m in valid and m not in used]
        used.update(members)
        spec = specs.get(key)
        if spec is None:
            stat = _short(raw.get("stat"), 10).lower()
            spec = specs[key] = {
                "label": _short(raw.get("label"), 80) or key, "unit": _short(raw.get("unit"), 20) or None,
                "stat": stat if stat in STATS else "value", "conditions": _short(raw.get("conditions"), 120) or None,
                "rule": norm_rule(raw.get("rule")), "critical": raw.get("critical") is True,
                "_members": []}
        spec["_members"] += members

    n = len(samples)
    needed = max(1, math.ceil(min_share * n))
    common_specs, rare = {}, {}
    for key, spec in specs.items():
        found = {valid[m][0] for m in spec["_members"]}
        # The merge often leaves items of some samples out of a spec. Count a sample also when one of its items
        # has the name or symbol of a member, the same statistic and the key's defining conditions, so a spec
        # every datasheet has is not filed as rare because the merge missed a few items.
        names = {_norm_name(x) for m in spec["_members"] for x in (valid[m][1]["name"], valid[m][1]["symbol"])}
        names.discard("")
        stats = {valid[m][1]["stat"] for m in spec["_members"]} | {spec["stat"]}
        for ident, (d, it) in valid.items():
            if d in found:
                continue
            if (_norm_name(it["symbol"]) in names or _norm_name(it["name"]) in names) and \
                    (it["stat"] in stats or "value" in (it["stat"], spec["stat"])) and \
                    _defining_ok(key, it["conditions"]):
                found.add(d)
        found = sorted(found)
        aliases = []
        for m in spec["_members"]:
            it = valid[m][1]
            for a in (it["name"], it["symbol"]):
                if a and a.lower() not in {x.lower() for x in aliases}:
                    aliases.append(a)
        out = {k: v for k, v in spec.items() if not k.startswith("_")}
        out["aliases"] = aliases[:12]
        out["found_in"] = f"{len(found)}/{n}"
        (common_specs if len(found) >= needed else rare)[key] = (len(found), out)

    def ordered(d):
        return {k: v for k, (_, v) in sorted(d.items(), key=lambda kv: (-kv[1][0], not kv[1][1]["critical"], kv[0]))}

    common_specs, rare = ordered(common_specs), ordered(rare)
    ensure_package(common_specs, rare)
    # must_match is for equal-or-nothing keys (package, pin count, polarity); a rating there would turn every
    # higher-rated part into a mismatch, so keys whose rule is not "=" are left out
    must = [k for k in dict.fromkeys(norm_key(x) for x in data.get("must_match") or [])
            if k in common_specs and common_specs[k]["rule"] == "="]
    if "package" in common_specs and "package" not in must:
        must.insert(0, "package")
    spec_list = {
        "type": type_key,
        "name": type_name,
        "version": 1,
        "status": "draft",
        "must_match": must,
        "visual_check": _short(data.get("visual_check"), 200) or None,
        "drafted": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "model": getattr(llm, "model", "?"), "min_share": min_share,
                    "samples": [s["file"] for s in samples]},
        "specs": common_specs,
        "rare_specs": rare,
    }
    stats = {"items": len(valid), "grouped": len(used), "specs": len(common_specs), "rare": len(rare),
             "seconds": round(answer.seconds, 1), "tokens_in": answer.tokens_in, "tokens_out": answer.tokens_out}
    return {"spec_list": spec_list, "stats": stats}


PACKAGE_SPEC = {"label": "Package / case", "unit": None, "stat": "value", "conditions": None, "rule": "=",
                "critical": True, "aliases": ["Package", "Case"], "found_in": "added"}


def ensure_package(specs: dict, rare: dict) -> None:
    """Every spec list compares the package (plan: drop-in = same footprint). Datasheets often name it only in
    the title or an outline heading, so the samples miss it: move it up from rare_specs, or add it."""
    if "package" in specs:
        specs["package"].update(rule="=", critical=True)
        return
    spec = dict(rare.pop("package", None) or PACKAGE_SPEC)
    spec.update(rule="=", critical=True, unit=None)
    specs["package"] = spec


# ------------------------------------------------------------------ files
HEADER = """# Spec list for one component type (plan v1, parts_alternatives_plan.md, "Spec lists").
# Drafted by `python -m dataPipeline.index.spec_lists draft`. Review it, then set status: reviewed and run
# `python -m dataPipeline.index.spec_lists check`. Raise `version` on every change after review.
#
# rule:      "=" must be equal | ">=" equal or higher is fine | "<=" equal or lower is fine
#            "≈" about equal | "contains" the replacement's range must cover the requested one | "info" not compared
# critical:  true = a part is unusable when this spec is worse
# found_in:  in how many sample datasheets the draft found this spec (information only)
# specs:     used for extraction and comparison; rare_specs: found in too few samples, not used (move up if needed)
"""


def spec_list_path(type_key: str) -> Path:
    return SPEC_LIST_DIR / f"{type_key}.yaml"


def load_spec_list(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def save_spec_list(spec_list: dict, path: Path) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + yaml.safe_dump(spec_list, allow_unicode=True, sort_keys=False, width=110),
                    encoding="utf-8")


def target_path(type_key: str, force: bool) -> Path:
    """spec_lists/<type>.yaml, unless that exists: then <type>.draft.yaml (a reviewed list is never replaced;
    an existing draft only with --force)."""
    path = spec_list_path(type_key)
    if not path.exists():
        return path
    status = (load_spec_list(path) or {}).get("status")
    return path if force and status == "draft" else path.with_name(f"{type_key}.draft.yaml")


def validate(spec_list: dict, type_keys: set | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) of one spec list."""
    errors, warnings = [], []
    key = spec_list.get("type")
    if not key or not isinstance(key, str):
        errors.append("missing `type`")
    elif type_keys is not None and key not in type_keys:
        warnings.append(f"type {key!r} is not in index_cache/type_list.yaml")
    if not spec_list.get("name"):
        errors.append("missing `name`")
    if not isinstance(spec_list.get("version"), int) or spec_list.get("version") < 1:
        errors.append("`version` must be a whole number >= 1")
    if spec_list.get("status") not in STATUSES:
        errors.append(f"`status` must be one of {', '.join(STATUSES)}")
    specs = spec_list.get("specs")
    if not isinstance(specs, dict) or not specs:
        errors.append("`specs` is empty")
        specs = {}
    for k, s in specs.items():
        where = f"specs.{k}"
        if not KEY_RE.match(str(k)):
            errors.append(f"{where}: key must be lower case snake_case, optionally with one '@' condition")
        if not isinstance(s, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        if not s.get("label"):
            errors.append(f"{where}: missing `label`")
        if s.get("rule") not in RULES:
            errors.append(f"{where}: rule {s.get('rule')!r} is not one of {' '.join(RULES)}")
        if s.get("stat", "value") not in STATS:
            errors.append(f"{where}: stat {s.get('stat')!r} is not one of {' '.join(STATS)}")
        if not isinstance(s.get("critical", False), bool):
            errors.append(f"{where}: `critical` must be true or false")
        if s.get("rule") in (">=", "<=", "≈", "contains") and not s.get("unit"):
            warnings.append(f"{where}: rule {s['rule']} compares numbers but there is no unit")
    for k in spec_list.get("must_match") or []:
        if k not in specs:
            errors.append(f"must_match: {k!r} is not a key in `specs`")
        elif isinstance(specs[k], dict) and specs[k].get("rule") != "=":
            warnings.append(f"must_match: {k!r} has rule {specs[k].get('rule')!r}; must_match is for keys that "
                            f"must be equal (package, pin count, polarity)")
    if specs and "package" not in specs:
        warnings.append("no `package` spec: drop-in replacements cannot be recognised")
    if specs and not any(s.get("critical") for s in specs.values() if isinstance(s, dict)):
        warnings.append("no spec is critical")
    if specs and all(s.get("rule") == "info" for s in specs.values() if isinstance(s, dict)):
        warnings.append("every spec has rule info: nothing would be compared")
    return errors, warnings


def known_types() -> dict:
    from dataPipeline.index.classify import load_type_list
    return load_type_list()


# ------------------------------------------------------------------ commands
def cmd_list(args) -> int:
    types = known_types()
    if not types:
        print("No type list yet. Run Step 0: python -m dataPipeline.index.classify run / group / report")
        return 1
    print(f"{'type':32} {'name':40} {'datasheets':>10}  spec list")
    for key, t in sorted(types.items(), key=lambda kv: (kv[0].startswith("_"), -(kv[1].get("datasheets") or 0))):
        if key.startswith("_"):
            continue
        path = spec_list_path(key)
        if path.exists():
            sl = load_spec_list(path)
            status = f"{sl.get('status', '?')} v{sl.get('version', '?')}, {len(sl.get('specs') or {})} specs"
        else:
            status = "-"
        if path.with_name(f"{key}.draft.yaml").exists():
            status += " (+ new draft)"
        print(f"{key:32.32} {str(t.get('name', key)):40.40} {t.get('datasheets') or 0:>10}  {status}")
    return 0


def _inventory_for(ds, cache: dict, llm, args) -> tuple[dict, bool]:
    """(inventory, from_cache)"""
    inv = cache.get("spec_inventory") or {}
    if inv.get("version") == INVENTORY_VERSION and not args.force_inventory:
        return inv, True
    markdown = ds.path.read_text(encoding="utf-8", errors="replace")
    result = inventory(markdown, llm, args.max_chars)
    inv = {"version": INVENTORY_VERSION, "model": getattr(llm, "model", "?"),
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result}
    fresh = read_cache(ds.cache_path)  # re-read: another step may have written meanwhile
    fresh["spec_inventory"] = inv
    write_cache(ds.cache_path, fresh)
    return inv, False


def draft_type(type_key: str, args, inventory_llm, merge_llm, types: dict) -> int:
    name = types.get(type_key, {}).get("name", type_key)
    candidates = datasheets_of_type(Path(args.input), type_key, args.supplier)
    if not candidates:
        print(f"{type_key}: no datasheets with this type. Run `classify report` first, or check the key.")
        return 1
    samples = pick_samples(candidates, args.samples)
    n_suppliers = len({ds.supplier for ds, _ in samples})
    print(f"{type_key} ({name}): {len(candidates)} datasheets, {len(samples)} samples from {n_suppliers} suppliers")

    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_inventory_for, ds, cache, inventory_llm, args): (ds, cache) for ds, cache in samples}
        for fut in cf.as_completed(futures):
            ds, cache = futures[fut]
            try:
                inv, cached = fut.result()
            except (LLMError, OSError) as exc:
                print(f"  ! {ds.rel}: {exc}", file=sys.stderr)
                continue
            fam = bool((cache.get("classification") or {}).get("family"))
            results.append({"file": ds.rel, "supplier": ds.supplier, "family": fam, "items": inv["items"]})
            if args.verbose:
                print(f"  {ds.rel}: {len(inv['items'])} specs listed"
                      + (" (cached)" if cached else f", {inv.get('seconds', '?')}s"))
    results.sort(key=lambda r: r["file"])
    results = [r for r in results if r["items"]]
    if len(results) < 2:
        print(f"  ! {type_key}: fewer than 2 samples with specifications, no draft written", file=sys.stderr)
        return 1

    merged = merge_specs(type_key, name, results, merge_llm, args.min_share)
    st, spec_list = merged["stats"], merged["spec_list"]
    path = target_path(type_key, args.force)
    save_spec_list(spec_list, path)
    print(f"  {st['items']} items from {len(results)} samples, {st['grouped']} grouped -> {st['specs']} specs in "
          f"at least {math.ceil(args.min_share * len(results))}/{len(results)} samples, {st['rare']} rare "
          f"(merge {st['seconds']}s, {st['tokens_in']} tokens in, {st['tokens_out']} out)")
    print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if path.name.endswith(".draft.yaml"):
        print(f"  ({spec_list_path(type_key).name} already exists and was not changed: compare the two by hand)")
    errors, warnings = validate(spec_list)
    for w in warnings:
        print(f"  note: {w}")
    for e in errors:
        print(f"  ! {e}", file=sys.stderr)
    return 1 if errors else 0


def cmd_draft(args, inventory_llm=None, merge_llm=None) -> int:
    types = known_types()
    keys = list(args.types or [])
    ranked = sorted((k for k in types if not k.startswith("_")),
                    key=lambda k: (-(types[k].get("datasheets") or 0), k))
    picked = ranked if getattr(args, "all", False) else ranked[:args.top] if args.top else []
    if picked:
        # --all / --top: every type with enough datasheets that has no spec list yet (resumable: a rerun
        # after Ctrl+C or a crash continues with the types still missing; --force also redoes drafts)
        min_ds = getattr(args, "min_datasheets", 3)
        few = [k for k in picked if (types[k].get("datasheets") or 0) < min_ds]
        done = [k for k in picked if k not in few and spec_list_path(k).exists() and not args.force]
        keys += [k for k in picked if k not in keys and k not in few and k not in done]
        print(f"{len(picked)} types: {len(keys)} to draft, {len(done)} already have a spec list, "
              f"{len(few)} with fewer than {min_ds} datasheets skipped (--min-datasheets)")
    if not keys:
        if picked:
            return 0
        print("Name the types (e.g. `draft mosfet_n mlcc`), or use --top N or --all. See: spec_lists list")
        return 1
    if inventory_llm is None:
        inventory_llm = OllamaJSON("specInventoryModel", DEFAULT_MODEL)
        inventory_llm.check_model()
    if merge_llm is None:
        merge_llm = OllamaJSON("specListModel", DEFAULT_MODEL)
        merge_llm.check_model()
    failed = 0
    try:
        for i, key in enumerate(keys, 1):
            if len(keys) > 1:
                print(f"\n[{i}/{len(keys)}] ", end="")
            try:
                failed += draft_type(key, args, inventory_llm, merge_llm, types)
            except LLMError as exc:
                print(f"  ! {key}: {exc}", file=sys.stderr)
                failed += 1
    except KeyboardInterrupt:
        print("\nStopped. Finished spec lists and sample inventories are kept; run the same command again "
              "to continue.")
        return 1
    if failed:
        print(f"\n{failed} type(s) without a new draft (see the messages above)")
    print("\nNext: review the files in spec_lists/ (rules, critical, must_match, missing specs), set "
          "status: reviewed, then run: python -m dataPipeline.index.spec_lists check")
    return 1 if failed else 0


def cmd_check(args) -> int:
    paths = [spec_list_path(k) for k in args.types] if args.types else sorted(SPEC_LIST_DIR.glob("*.yaml"))
    paths = [p for p in paths if not p.name.endswith(".draft.yaml") or args.types]
    if not paths:
        print(f"No spec lists in {SPEC_LIST_DIR}. Run: python -m dataPipeline.index.spec_lists draft ...")
        return 1
    type_keys = set(known_types()) or None
    bad = 0
    for path in paths:
        if not path.exists():
            print(f"{path.name}: missing")
            bad += 1
            continue
        try:
            sl = load_spec_list(path)
        except Exception as exc:  # noqa: BLE001 - YAML syntax error after editing
            print(f"{path.name}: not valid YAML ({exc})")
            bad += 1
            continue
        errors, warnings = validate(sl, type_keys)
        if path.stem != sl.get("type"):
            errors.append(f"file name {path.name} does not match type {sl.get('type')!r}")
        specs = sl.get("specs") or {}
        crit = sum(1 for s in specs.values() if isinstance(s, dict) and s.get("critical"))
        print(f"{path.name}: {sl.get('status', '?')} v{sl.get('version', '?')}, {len(specs)} specs "
              f"({crit} critical), must_match {sl.get('must_match') or []}"
              + (" - OK" if not errors else f" - {len(errors)} error(s)"))
        for e in errors:
            print(f"  ! {e}")
        for w in warnings:
            print(f"  note: {w}")
        if args.verbose and isinstance(specs, dict):
            for k, s in specs.items():
                if isinstance(s, dict):
                    print(f"    {k:34.34} {s.get('rule', ''):8} {str(s.get('unit') or ''):8} "
                          f"{'critical' if s.get('critical') else '':8} {s.get('found_in', '')}")
        bad += bool(errors)
    return 1 if bad else 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="types with datasheet counts and spec-list status")
    p = sub.add_parser("draft", help="draft spec lists with the LLM")
    p.add_argument("types", nargs="*", help="type keys from index_cache/type_list.yaml")
    p.add_argument("--top", type=int, help="also the N types with the most datasheets (only those without a list)")
    p.add_argument("--all", action="store_true",
                   help="every type without a spec list yet (rerun to resume; with --force also redo drafts)")
    p.add_argument("--min-datasheets", type=int, default=3,
                   help="with --top/--all: skip types with fewer datasheets (default 3)")
    p.add_argument("--samples", type=int, default=10, help="sample datasheets per type (default 10)")
    p.add_argument("--min-share", type=float, default=0.5,
                   help="keep specs found in at least this share of the samples (default 0.5)")
    p.add_argument("--max-chars", type=int, default=24000, help="characters of spec pages sent per datasheet")
    p.add_argument("--workers", type=int, default=1,
                   help="parallel inventory requests (also set OLLAMA_NUM_PARALLEL on the Ollama server)")
    p.add_argument("--input", default=str(MARKDOWN_DIR), help="folder with <supplier>/**/*.md")
    p.add_argument("--supplier", nargs="+", help="only samples from these supplier folders")
    p.add_argument("--force", action="store_true", help="replace an existing draft (never a reviewed list)")
    p.add_argument("--force-inventory", action="store_true", help="list the samples' specs again (no cache)")
    p.add_argument("-v", "--verbose", action="store_true", help="print every sample")
    p = sub.add_parser("check", help="validate spec lists")
    p.add_argument("types", nargs="*", help="only these types (default: every spec_lists/*.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="print every spec")
    args = parser.parse_args(argv)

    from helpers.run_log import start_run_log
    start_run_log(f"index_spec_lists_{args.command}")
    try:
        return {"list": cmd_list, "draft": cmd_draft, "check": cmd_check}[args.command](args)
    except LLMError as exc:  # Ollama not running, model not pulled, ...
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
