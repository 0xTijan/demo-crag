# Finding alternatives from supplier datasheets: plan (v1, simple)

> **The problem.** Given a part number from a manufacturer we don't stock, we need to read its datasheet
> and find the closest parts from our 110 suppliers. Those suppliers' 10,000+ markdown datasheets cover
> about 200 component types, each with its own specs, and many datasheets describe whole families of part
> numbers. The hard part is turning all of that into specs with one shared set of names and units, without
> building and maintaining a table per type. Then we compare spec by spec within the same type, label each
> result drop-in, functional or partial, show the datasheet page behind every value, and keep a match as
> fact only after an engineer confirms it.

**The approach in one sentence:** find candidates using a few key specs per component type, then list
their specs side by side with the requested part (like the reference screenshot), and let an engineer decide.

Updated 2026-11-18. Everything runs locally (30B+ model); only web search reaches the internet.
The complete, more complex design is kept in [`parts_alternatives_full_design.md`](parts_alternatives_full_design.md)
and is used only where v1 turns out not to be enough. The differences are explained in
[`parts_alternatives_plan_comparison.md`](parts_alternatives_plan_comparison.md).

**Where the effort is:** most of the work goes into making the indexed data correct (spec lists, checking
extraction); most of the waiting is the indexing run; searching on top of good data is the easy part.

---

## 1. Overview

```
 ONCE                       OFFLINE (then every 2 weeks)        PER REQUEST
┌──────────────────┐       ┌──────────────────────┐   ┌────────────────┐   ┌──────────────────┐   ┌────────────────┐
│ 0. CLASSIFY      │──────►│ 1. INDEX             │──►│ 2. SHORTLIST   │──►│ 3. SPEC TABLE    │──►│ 4. CONFIRM     │
│ every datasheet  │       │ datasheets of types  │   │ same type,     │   │ requested vs top │   │ sales → eng.   │
│ → type; pick the │       │ with a spec list →   │   │ % key specs    │   │ candidates,      │   │ confirm/reject │
│ first ~10 types; │       │ key specs (one LLM   │   │ met → top 10   │   │ label, page per  │   │ + reason       │
│ write spec lists │       │ call per datasheet)  │   │                │   │ value            │   │                │
└──────────────────┘       └──────────────────────┘   └────────────────┘   └──────────────────┘   └────────────────┘
                                                        ▲ the chat agent in Open WebUI uses these steps as tools (section 9)
```

---

## 2. Where the data lives

### The places

```
markdowns/suppliers/<supplier>/*.md      ← INPUT: converted datasheets (indexing doesn't change them)
spec_lists/*.yaml                        ← RULES: which specs to extract per type and how to compare them (in git)
        │
        ▼  indexing (LLM + checks)
index_cache/<supplier>/<file_hash>.json  ← RESULT AS FILES: one per datasheet; the master copy (company git)
        │
        ▼  load_cache (minutes, no LLM)
Postgres                                 ← RESULT FOR SEARCHING: fast copy, can be rebuilt from the cache
  datasheets      one row per datasheet: type and indexing status
  catalog_parts   one row per part, or per availability row of a family
  external_parts  requested parts (same layout, never mixed with ours)
  matches         confirmations and rejections: people's decisions, backed up separately
```

- **Markdowns** are the proof: the spec table links to their pages, and the raw-text check reads them.
- **Spec lists** say what to extract and how to compare it.
- **Cache files** are the master copy of the indexing results. They survive a reinstall (section 12).
- **The database** is the fast, searchable copy. It can be deleted and rebuilt from the cache in minutes.
  The only table that can't be rebuilt is `matches`, because people create it.

### Tables

```sql
datasheets     (id, supplier, file, file_hash, type, extractor_version, spec_list_version,
                status,        -- classified | indexed | failed | type_not_supported
                error text, indexed_at);
catalog_parts  (id, datasheet_id, supplier, type, part_number, family_row text,
                ordering_page int, specs jsonb);
external_parts (id, manufacturer, part_number, file, file_hash, extractor_version, type,
                specs jsonb, fetched_at);
matches        (id, external_part_id, catalog_part_id, concrete_pn, label, score,
                compared_specs jsonb,
                status,        -- proposed | sales_confirmed | confirmed | rejected | needs_recheck
                decided_by, decided_role, decided_at, reason);
```

- `datasheets` has a row for **every** datasheet, including types without a spec list yet. It gives the
  counts per type, and the list of failed files to look at.
- `catalog_parts` rows exist only for datasheets of supported types.
- **Our parts and external parts are never mixed:** separate tables, separate folders
  (`markdowns/suppliers/…`, `markdowns/external/…`) and separate cache folders. `matches` is the only
  table that points to both.

### Example: what one datasheet becomes

Cache file `index_cache/<supplier>/a3f9….json` for the 2N7000:

```json
{
  "file": "markdowns/suppliers/<supplier>/2N7000.md",
  "file_hash": "a3f9…",
  "extractor_version": "v1.0",
  "type": "mosfet_n",
  "spec_list_version": "mosfet_n@3",
  "parts": [
    {"part_number": "2N7000",
     "specs": {
       "package":            {"value": "TO-92", "page": 4, "raw": "TO-92"},
       "vds_max":            {"value": 60,  "unit": "V",   "stat": "max", "conditions": {}, "page": 1, "raw": "60"},
       "id_max":             {"value": 0.2, "unit": "A",   "stat": "max", "conditions": {"ta": "25 °C"}, "page": 1, "raw": "0.2"},
       "rds_on_max@vgs_10v": {"value": 5,   "unit": "ohm", "stat": "max", "conditions": {"vgs": "10 V", "id": "500 mA"}, "page": 2, "raw": "5"}
     }}
  ]
}
```

For a family like the Viking MLCC, there is **one entry per availability row** instead of one per part:

```json
{"family_row": "0603 / X7R / 50 V",
 "part_number": null,
 "ordering_page": 3,
 "specs": {
   "size":        {"value": "0603 (inch)", "page": 5, "raw": "0603"},
   "dielectric":  {"value": "X7R",         "page": 5, "raw": "X7R"},
   "v_rated":     {"value": 50, "unit": "V", "page": 5, "raw": "50V"},
   "capacitance": {"min": 1.5e-10, "max": 4.7e-7, "unit": "F", "page": 5, "raw": "151~474"}
 }}
```

The MLCC datasheet becomes a few hundred such entries, not millions of part numbers. The exact part number
(e.g. `03KTB500104`) is built only when it's shown as a candidate, from the ordering table on page 3.

In the database these become rows of `catalog_parts`:

| id | datasheet_id | supplier | type | part_number | family_row | ordering_page | specs |
|---|---|---|---|---|---|---|---|
| 1 | 17 | Supplier A | mosfet_n | 2N7000 | – | – | `{"vds_max": {"value": 60, …}, …}` |
| 2 | 42 | Viking | mlcc | – | 0603 / X7R / 50 V | 3 | `{"capacitance": {"min": 1.5e-10, …}, …}` |

**Value format.** Every value uses the same fields: `value` (or `min` / `max` for ranges), `unit`, `stat`,
`conditions`, `page`, `raw`, even where v1 doesn't use all of them yet. This keeps the upgrade cheap (section 16).

### Generated SQL views per type (proposed)

For SQL queries, reports, or an LLM writing SQL, code generates one readable view per type **from its
spec list**. Nobody edits them by hand; changing the YAML regenerates the view. About a day of work.

```sql
-- generated from spec_lists/mosfet_n.yaml
CREATE VIEW mosfet_n AS
SELECT part_number, supplier,
       specs->'package'->>'value'                       AS package,
       (specs->'vds_max'->>'value')::numeric            AS vds_max_v,
       (specs->'id_max'->>'value')::numeric             AS id_max_a,
       (specs->'rds_on_max@vgs_10v'->>'value')::numeric AS rds_on_max_10v_ohm
FROM catalog_parts WHERE type = 'mosfet_n';
```

Why not hand-written SQL tables per type: the spec list is needed anyway (it holds the comparison rules,
which SQL columns can't), each value needs its proof fields and ranges, and every spec change would mean
changing table structures. Generated views give the SQL tables without that maintenance.

---

## 3. Step 0: Classify all datasheets (once, before anything else)

The first types to support can only be chosen once it's known which types exist and how many datasheets
each has.

1. **Quick classification of all ~10k datasheets:** the LLM reads only the first 1–2 pages and returns a
   free-text type ("N-channel MOSFET", "MLCC", "Schottky diode"). Short calls; runs overnight.
2. **Group the labels:** the LLM merges synonyms ("MLCC", "multilayer ceramic capacitor", "SMD ceramic cap"
   → `mlcc`) into one type list. You check the list once.
3. **Count datasheets per type** and pick the first ~10. Later the choice is made by request volume,
   once that data exists.

The type is stored in `datasheets.type` and in the cache file. New datasheets in the two-weekly run are
classified the same way.

---

## 4. Spec lists

### Format

One small file per type, e.g. `spec_lists/mosfet_n.yaml`. It names only the **5–10 specs that decide a
replacement**.

```yaml
type: mosfet_n
name: N-channel MOSFET
version: 3                             # raised on every change; re-indexing uses it
must_match: [package]                  # different → can't be Drop-in
visual_check: "Confirm pinout (G/D/S order) on the package drawing."
specs:
  package:            {label: "Package (footprint)",    rule: "="}
  vds_max:            {label: "V(DS) max",              unit: V,    rule: ">=", critical: true}
  id_max:             {label: "I(D) max",               unit: A,    rule: ">=", critical: true}
  rds_on_max@vgs_10v: {label: "R(DS)on max @ VGS=10 V", unit: ohm,  rule: "<="}
  vgs_th_max:         {label: "V(GS)th max",            unit: V,    rule: "<="}
  tj_max:             {label: "T(J) max",               unit: degC, rule: ">="}
```

Rules: `=` must be equal, `>=` / `<=` equal or better, `≈` close to (with a tolerance), `contains`
the candidate's range must cover the requested one (e.g. temperature range).

### Creating a spec list (~45–60 min per type)

**1. Draft (LLM, ~5 min).** Pick ~10 sample datasheets of the type: different suppliers, single parts and
families, small and large parts. Give the LLM their spec tables, the YAML format and the MOSFET example,
with an instruction like:

> "List the 5–10 specs that decide whether one part can replace another. Use specs that most of these
> datasheets give. For each: a key with its condition and min/max (e.g. `rds_on_max@vgs_10v`), label, unit,
> rule (`=`, `>=`, `<=`, `≈`, `contains`), critical yes/no. Name what must match for a drop-in (package…),
> and anything that can only be checked on a drawing."

**2. Check the draft against the samples (automatic).** Run the normal index call (section 5) with the
draft on the same 10 datasheets, and count how often each spec was found:

```
mosfet_n draft — found in 10 sample datasheets
  package            10/10
  vds_max            10/10
  id_max             10/10
  rds_on_max@vgs_10v  8/10
  rds_on_max@vgs_4v5  5/10   ← keep as non-critical, or drop
  qg_total_max        3/10   ← drop: too rarely given to shortlist with
```

Specs that are rarely given can't help find candidates: drop them, or use the version most datasheets give.

**3. Review by a person (~10–20 min).** An engineer, or you with an engineer for the important types,
checks what the LLM most often gets wrong:
- **Direction:** voltage rating `>=`, RDS(on) `<=`, capacitance `≈` (equal, not "more is better").
- **Critical:** which specs make a part unusable when worse.
- **Must match for a drop-in:** package, size system (inch vs metric), pin count, polarity / channel type.
- **Missing:** a spec engineers always look at that the samples didn't show.

A useful reference while reviewing: the filter list Digi-Key or Mouser use for that category. It shows which
specs the industry uses to choose this type of part. Look at it; don't copy data from it.

**4. Trial run (~20–30 min).** Index ~30 datasheets of the type and look at the extracted values as a table
(wrong units, empty columns, odd values). Fix the YAML if needed, then index all datasheets of the type.

| Per type | Time |
|---|---|
| LLM draft + automatic check | ~10 min, mostly waiting |
| Human review | ~10–20 min |
| Trial run + fixes | ~20–30 min |
| **Total** | **~45–60 min**, so ~1–1.5 days for the first 10 types |

### Changing a spec list (e.g. adding a spec to MOSFETs)

No code changes; only that type's datasheets are re-run. Example: add the maximum gate-source voltage.

1. **Edit the YAML and raise its version** (2 min):
   ```yaml
   version: 4                     # was 3
   specs:
     ...
     vgs_max: {label: "V(GS) max", unit: V, rule: ">=", aliases: ["VGS", "Gate-Source Voltage"]}   # new
   ```
2. **Check it on the ~10 sample datasheets** (~10 min): is `vgs_max` found in most of them? An engineer
   confirms the direction and whether it's critical.
3. **Re-index only that type.** The indexer sees cache files with `mosfet_n@3` while the list is now `@4`
   and re-runs exactly those datasheets. Recommended: ask the LLM **only for the new spec** and merge it into
   the existing cache files. The output is tiny, so this is several times faster than a full re-extraction.
4. **Load into the database** with `load_cache` (minutes).

| Time (assuming ~600 MOSFET datasheets) | One at a time | Parallel |
|---|---|---|
| Full re-extraction | ~4–6 h | < 1 h |
| New spec only | ~1–2 h | ~10–20 min |

Plus ~15 minutes of human time for steps 1–2.

| | Effect of adding a spec |
|---|---|
| Other types | untouched |
| Search during the re-run | keeps working; not yet re-indexed parts show `?` (not stated) for the new spec |
| Confirmed matches | stay valid: the new spec wasn't compared (decision 1) |
| Cached external parts of that type | get the new spec in the same run, or the next time they're requested |
| Generated SQL view | regenerated from the YAML |
| Code | no change |

Changes that need more care:
- **Renaming a spec:** stored values and saved matches use the old name and would need migrating.
  Avoid renames: add the new spec and stop using the old one.
- **Changing a rule** (e.g. `<=` → `≈`): no re-indexing, only the scoring changes. Saved matches compared
  under the old rule may deserve a quick engineer look.
- **A new kind of rule** (beyond `=`, `>=`, `<=`, `≈`, `contains`): a few lines in the comparison function.
- **Removing a spec:** delete it from the YAML; stored values are simply ignored.

**Adding a whole new type** = creating one spec list (~45–60 min) + indexing that type. No code changes.

---

## 5. Step 1: Index (offline)

### One LLM call per datasheet

Runs only for datasheets whose type (from Step 0) has a spec list.
Input: the markdown + that type's spec list. Output (temperature 0): every part, or for families every row
of the ordering/availability table, with its key specs **in the units of the spec list** and the **page**
each value came from, in the value format of section 2. To save time the LLM can write a compact line per
value, which code turns into the full format (section 11).

**Two checks in code:** each value's `raw` text must appear on the cited page, and every range must have
min ≤ max. A value that fails is marked *unverified* and shows a warning. This catches most invented or
broken values.

- Supplier = folder name. Only new or changed files are indexed (file hash), like the report pipeline.
- Results go to the cache file and `catalog_parts`; `datasheets.status` becomes `indexed` or `failed`.
- Datasheets of types without a spec list get `type_not_supported`; requests for those types are answered
  by the chat agent's fallback (section 9) or "type not supported yet".

### Watch out for (seen in the sample datasheets)

- **Test conditions:** RDS(on) is given at VGS=10 V and 4.5 V → separate specs in the list.
- **min / typ / max:** the spec list says which one (e.g. `vgs_th_max`), so like is compared with like.
- **Package names:** `0603` can be inch or metric → the spec list says which; the LLM writes the standard name.
- **Typos in the source** (e.g. `225~1.6` in the MLCC table): the raw-text check doesn't catch these, because
  the text really is on the page. The min ≤ max check marks them unverified.

---

## 6. Step 2: Shortlist (per request)

1. **Saved answers first:** engineer-confirmed matches for this part number are shown first; rejected
   ones are shown with their reason.
2. **Get the requested part's specs:** from `external_parts` if fetched before, otherwise the existing web
   pipeline → markdown → **the same Step 0 + Step 1 calls** → the exact part number's specs (for a family part
   the LLM reads the ordering table to decode it). If no manufacturer is given and several parts fit,
   **ask the salesperson** which one.
3. **Filter and score in plain Python:** only catalogue parts of the **same type**, scored by the
   share of key specs met (critical specs count more). Take the top ~10; if equally good parts come from
   different suppliers, list all of them.

---

## 7. Step 3: Spec table (the screenshot)

- **Columns:** requested part + top candidates. **Rows:** the type's key specs. Each cell: value,
  ✓ meets / ✗ worse / ? not stated, and the datasheet page.
- **Labels, set by code from the spec list:**
  - **Drop-in:** `must_match` specs equal and all key specs met.
  - **Functional:** all critical specs met; package differs or a non-critical spec is worse.
  - **Partial:** a critical spec fails or is missing; shown last, with a warning.
- **One short LLM call per family candidate:** build the exact part number from the ordering table
  (e.g. Viking `03KTB500104` for a 0603 / X7R / 50 V / 100 nF request).
- **Warnings:** unverified values, missing specs, and the type's `visual_check` note (e.g. pinout).

The matching service has one entry point: `find_alternatives(part_number, manufacturer=None)` → JSON
(or a question back to the salesperson).

---

## 8. Step 4: Confirm

```
proposed ──► sales confirms ──► engineer confirms ──► confirmed (a fact)
             (only a note)  └──► engineer rejects  ──► rejected, with a written reason (still shown)
```

- A confirmation covers only the **two exact part numbers**.
- The match stores a copy of the compared specs. When a datasheet is re-indexed and one of those specs
  changed, the match goes to **needs re-check**.
- "Engineer" = a role in your own system.

---

## 9. Chat agent and MCP tools in Open WebUI (proposed)

The index makes search exact and fast; an agent on top makes it conversational. The agent doesn't search
the raw markdowns itself (too slow on a local model, can't do "≥ 60 V" in text, and misses parts without
saying so). It uses the index through tools:

| Tool (MCP) | Does |
|---|---|
| `find_alternatives(part_number, manufacturer?)` | the spec table (sections 6–7) |
| `get_specs(part_number)` | one part's indexed specs |
| `search_catalog(type, filters)` | e.g. "MOSFETs, VDS ≥ 60 V, SOT-23" |
| `open_datasheet(part_number, page)` | the actual datasheet page, as proof |
| `confirm_match / reject_match(match_id, reason?)` | the confirmation flow (section 8), with role checks |

Then a salesperson can ask follow-ups: "only automotive ones", "why isn't part X in the list?", "show page 2
of the second candidate".

The agent also does the two jobs where reading is unavoidable:
- **The request side:** finding the external part's datasheet on the web and decoding its part number.
- **Fallback for types without a spec list:** using `datasheets.type`, it reads the few dozen datasheets of
  that type and compares them, with a clear warning ("type not indexed, compared by reading datasheets").
  Slow, but better than no answer for rare requests.

---

## 10. Time per request

| Case | Time |
|---|---|
| Never seen before | **~1–3 min**: find + download + convert the datasheet (20 s – 2 min), index it (20–60 s), shortlist (< 1 s), part numbers for families (10–30 s) |
| Seen before | seconds |
| Confirmed match exists | instant |
| Type without a spec list (agent fallback) | several minutes |

Estimates; measure on ~20 real requests.

---

## 11. Indexing time (one-time, then incremental)

A local LLM reads input fast and writes output slowly, so the time depends mostly on **how much it writes**.
Estimates for a 30B model on one good GPU writing ~40 tokens/s; **measure with the pilot below**.

| Datasheet | Input | Output | Time, one at a time |
|---|---|---|---|
| Single part (2N7000) | ~3k tokens | ~300–500 tokens | ~10–15 s |
| Typical | ~8k tokens | ~1k tokens | ~25–35 s |
| Big family (Viking MLCC, ~200 availability rows) | ~20k tokens | ~5–10k tokens | ~2–5 min |

| Run | One at a time (Ollama) | Many in parallel (e.g. vLLM)* |
|---|---|---|
| Step 0, classification (first pages, tiny output) | ~3–8 h | < 1 h |
| Index only the first ~10 types (maybe 2–4k datasheets) | ~1–2 days | ~3–6 h |
| Index all ~10k datasheets | ~3–5 days | ~0.5–1 day |

*One GPU can handle many requests at once, usually 5–10× more total output; measure on the real setup.

### Making it faster

1. **Parallel requests** with a batching server such as vLLM instead of one request at a time. The biggest win.
2. **Compact LLM output:** one short line per value (`vds_max | 60 | V | max | p1 | "60"`); code turns it into
   the full value format. Output is the slow part, so this can cut the time 2–3×.
3. **Skip pages that don't matter:** code drops tape-and-reel, packaging, soldering and reliability pages
   before the LLM sees them (about a third of the MLCC file).
4. **Index only the supported types**, starting with the first ~10.
5. **Test a mixture-of-experts model:** some 30B-class models use only a few billion parameters per token and
   write several times faster at similar quality. Compare on the sample datasheets.
6. **Run overnight / on weekends;** the run can stop and resume where it left off.

### Afterwards

- Two-weekly runs: only new or changed files (by file hash), usually minutes to an hour.
- Changing a spec list: only that type's datasheets (section 4).
- A new external part: ~20–60 s for its datasheet.
- A new installation: no LLM run at all (section 12).

### Pilot (first task of Phase 0)

Index ~50 datasheets (single parts, normal families, a few huge ones like the MLCC). Measure seconds per
datasheet one at a time and in parallel, then extrapolate. One afternoon replaces these estimates with real numbers.

---

## 12. Index cache (no re-indexing on a new installation)

```
index_cache/
  <supplier>/<file_hash>.json        our suppliers' datasheets (format in section 2)
index_cache_external/
  <manufacturer>/<file_hash>.json    external parts, kept apart from our suppliers' data
```

- **New installation:** `load_cache` reads these files into Postgres in minutes, with no LLM.
- **Indexing skips** a datasheet when its cache file has the same file hash, extractor version and spec-list
  version. Only new, changed or re-specced files go to the LLM.
- **Kept in git:** small text files; a two-weekly run only changes the files of datasheets that changed.
  Roughly 2–20 KB per datasheet (more for big families), so very roughly 50–300 MB in total. If it grows
  much larger: Git LFS or a separate data repository.
- **Not in the cache:** the `matches` table. It's company data created by people and can't be recomputed,
  so it gets regular database backups.
- **Where the repository lives:** supplier specs and customer requests are company data. They go to the
  company's own git server or a private repository the company owns and approves, never a personal one.
- **Also needed on a new PC:** the markdown files, the spec-list YAML files and the code.

---

## 13. Testing

- **Starter test set:** an engineer writes ~30–50 requests with the correct alternative and label.
  Every confirmed or rejected match is added later.
- **Measure:** is the engineer's answer in the top 10? Does the label agree?
- **Spot-check extraction:** ~30 datasheets, key specs compared by hand.
- **Family datasheets:** rows extracted vs rows in the availability table.

---

## 14. Phases (~5–8 weeks, plus ~1 week for the chat agent)

| Phase | Work | Done when |
|---|---|---|
| 0 | Pilot: index ~50 datasheets and measure the speed (section 11). Then classify all datasheets (Step 0), group the labels into a type list, pick the first ~10 types | real speed numbers; every datasheet has a row in `datasheets` with a type |
| 1 | Spec lists for the first ~10 types (~1–1.5 days), then index their datasheets, writing the index cache | the first 10 types have key specs, checked by a trial run; cache loads on a clean database |
| 2 | Request flow, shortlist, spec table; `find_alternatives` as an MCP tool in Open WebUI | a real part number returns the table |
| 3 | Confirm / reject, test set, measurements | results measured on the test set |
| 4 | Chat agent tools (section 9), generated SQL views, fallback for unsupported types | follow-up questions answered from the index |

---

## 15. When to add more (from the full design)

| Add | Only when |
|---|---|
| Raw-facts layer + alias dictionary | key specs come out inconsistent across suppliers |
| Deterministic part-number generator for families | the LLM gets family part numbers wrong |
| More specs per type / weighted scoring | the engineer's answer is often missing from the top 10 |
| LLM review of candidates' full datasheets | engineers often find problems the key specs didn't show |
| Automatic type tree for all ~200 types | after the first ~10 types work well |
| Package alias table, separate DB users | before wider production use |

---

## 16. Upgrade path to the full design

**Mostly no rewrite.** About half of v1 stays as it is; the rest is extended or replaced piece by piece.
The one real replacement is the inside of the extractor, and it stays cheap only if v1 follows the rules below.

### Part by part

| Part of v1 | When upgrading | What happens |
|---|---|---|
| Open WebUI tools + spec table | **stays** | Same tools and table format. |
| Fetching the external datasheet | **stays** | Same web search → PDF → markdown. |
| Confirm / reject flow + `matches` table | **stays** | Already in its final shape. |
| Test set + measurements | **stays** | This is how each upgrade is shown to help. |
| Step 0 type list + `datasheets` table | **stays** | Starting point of the automatic type tree; becomes the full design's documents table. |
| Spec-list YAML files | **extended** | They become the registry: add aliases, conditions and importance, load them into the database. |
| Storage (`catalog_parts`, `external_parts`) | **extended** | Specs stay as JSON per part; tables are added around them (families, a flattened values table, the vocabulary). Existing data is migrated by a script, not re-extracted. |
| Shortlist + scoring | **extended** | More specs, per-spec penalties and an SQL pre-filter. The label rules stay. |
| Family part numbers | **replaced** (small) | The LLM call is swapped for the decoder-based generator, with the same input and output. |
| Extractor (one LLM call per datasheet) | **replaced** | Becomes table-by-table reading + alias dictionary + unit conversion in code. Its output format stays the same, so nothing after it notices. |
| LLM review, validation rules, corrections, separate DB users | **new** | Separate additions; nothing existing changes. |

**Effort:** a complete upgrade is roughly **10–16 more weeks** (with agentic coding, full-time), compared with
15–24 weeks for the full design from scratch. In practice only the pieces the tests call for are added
(section 15).

### Rules for v1 that keep the upgrade cheap

1. **Full value format from day one** (section 2), so storage, scoring and the table don't care which
   extractor produced a value.
2. **Conditions and min/typ/max in the spec names**, e.g. `rds_on_max@vgs_10v`. Renaming later means
   migrating data and confirmed matches.
3. **Each step a separate module with a fixed input and output:**
   `classify(markdown) → type`, `extract(markdown, spec_list) → parts with specs`,
   `shortlist(requested_part) → candidates`, `label(requested, candidate, spec_list) → label`,
   `build_part_number(candidate, requested) → part number`, `render(result) → table`.
4. **Spec lists written in the registry's terms:** `unit`, `rule`, `critical`, optional `aliases`, and a `version`.
5. **File hash, extractor version and spec-list version stored for every datasheet**, so a change
   re-processes only what it affects.
6. **Measure from the first week:** rows extracted vs rows in the table (family datasheets), and whether the
   engineer's answer is in the top 10.

### What would force a rewrite

Breaking rules 1–3: specs stored as free-form JSON with varying names, or extraction, scoring and display
mixed in one script. Then everything after the extractor depends on how the extractor works, and replacing
it breaks the rest.

---

## 17. Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Datasheet update vs confirmed match | Stays valid unless a compared spec changed; then "needs re-check" |
| 2 | What a confirmation covers | Only the two exact part numbers |
| 3 | Rejected matches | Engineer writes a reason; the match is still shown, marked "Rejected" |
| 4 | Specs not from a datasheet | Won't happen: every value has a datasheet page as proof |
| 5 | Same part from several suppliers | List all equally good ones, one per supplier |
| 6 | Part number without manufacturer, several possible parts | Ask the salesperson |
| 7 | Partial results failing a critical spec | Shown last, with a warning |
| 8 | Historical cross-references | None yet: starter test set of ~30–50, then grow from confirmations |
| 9 | Top component types | Not known yet: start with the types that have the most datasheets |
| 10 | Package list | None yet: the LLM writes standard package names; a dictionary only if needed |
| 11 | Local model | 30B+: a whole typical datasheet fits in one call |
| 12 | Supplier | From the folder name |
| 13 | Chat tool | Demo data now; will use the MCP tools of section 9 |
| 14 | Who is an engineer | A role in your own system |
| 15 | Drawings (mechanical / electromechanical) | Ignored for now; `visual_check` notes still shown |
| 16 | Scope of v1 | Simple version: key specs for shortlisting, side-by-side spec table, engineer decides |
| 17 | Re-indexing on a new installation | Not needed: results are kept as an index cache in the company's git |

### Proposed, not yet decided

- **Chat agent and MCP tools** (section 9), including the fallback for types without a spec list.
- **Generated SQL views per type** (section 2), instead of hand-written SQL tables.

### Still open

- The real priority list of component types, once request and sales data is available.
- How much engineer time is available for the spec lists and the starter test set.
