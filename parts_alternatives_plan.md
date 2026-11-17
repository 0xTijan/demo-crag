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

Written 2026-11-17. Everything runs locally (30B+ model); only web search reaches the internet.
The complete, more complex design is kept in [`parts_alternatives_full_design.md`](parts_alternatives_full_design.md)
and is used only where v1 turns out not to be enough.

---

## 1. Overview: 4 steps

```
 OFFLINE (once, then every 2 weeks)              PER REQUEST
┌──────────────────────┐   ┌────────────────┐   ┌──────────────────────┐   ┌────────────────┐
│ 1. INDEX             │──►│ 2. SHORTLIST   │──►│ 3. SPEC TABLE        │──►│ 4. CONFIRM     │
│ each datasheet →     │   │ same type,     │   │ requested vs top     │   │ sales → eng.   │
│ type + key specs     │   │ % key specs    │   │ candidates, label,   │   │ confirm/reject │
│ (one LLM call)       │   │ met → top 10   │   │ page per value       │   │ + reason       │
└──────────────────────┘   └────────────────┘   └──────────────────────┘   └────────────────┘
```

Only **4 things are stored**: the spec-list files, `catalog_parts`, `external_parts` and `matches`.

**Before Step 1, once:** every datasheet is classified by type, and a spec list is created for each of the
first ~10 types (section 2, "Step 0" and "How a spec list is created").

---

## 2. Step 1: Index (offline)

### Step 0: Classify all datasheets (once, before anything else)

The first types to support can only be chosen once it's known which types exist and how many datasheets
each has.

1. **Quick classification of all ~10k datasheets:** the LLM reads only the first 1–2 pages and returns a
   free-text type ("N-channel MOSFET", "MLCC", "Schottky diode"). Short calls; runs overnight.
2. **Group the labels:** the LLM merges synonyms ("MLCC", "multilayer ceramic capacitor", "SMD ceramic cap"
   → `mlcc`) into one type list. You check the list once.
3. **Count datasheets per type** and pick the first ~10. Later the choice is made by request volume,
   once that data exists.

Every datasheet is stored with its type. New datasheets in the two-weekly run are classified the same way.

### A spec list per component type

One small file per type, e.g. `spec_lists/mosfet_n.yaml`. It names only the **5–10 specs that decide a
replacement**. How it's created is described below.

```yaml
type: mosfet_n
name: N-channel MOSFET
must_match: [package]                  # different → can't be Drop-in
visual_check: "Confirm pinout (G/D/S order) on the package drawing."
specs:
  package:        {label: "Package (footprint)",      rule: "="}
  vds_max:        {label: "V(DS) max",                unit: V,    rule: ">=", critical: true}
  id_max:         {label: "I(D) max",                 unit: A,    rule: ">=", critical: true}
  rds_on_max@vgs_10v: {label: "R(DS)on max @ VGS=10 V", unit: ohm, rule: "<="}
  vgs_th_max:     {label: "V(GS)th max",              unit: V,    rule: "<="}
  tj_max:         {label: "T(J) max",                 unit: degC, rule: ">="}
```

Rules: `=` must be equal, `>=` / `<=` equal or better, `≈` close to (with a tolerance), `contains`
the candidate's range must cover the requested one (e.g. temperature range).

### How a spec list is created (~45–60 min per type)

**1. Draft (LLM, ~5 min).** Pick ~10 sample datasheets of the type: different suppliers, single parts and
families, small and large parts. Give the LLM their spec tables, the YAML format and the MOSFET example,
with an instruction like:

> "List the 5–10 specs that decide whether one part can replace another. Use specs that most of these
> datasheets give. For each: a key with its condition and min/max (e.g. `rds_on_max@vgs_10v`), label, unit,
> rule (`=`, `>=`, `<=`, `≈`, `contains`), critical yes/no. Name what must match for a drop-in (package…),
> and anything that can only be checked on a drawing."

**2. Check the draft against the samples (automatic).** Run the normal index call (below) with the draft
on the same 10 datasheets, and count how often each spec was found:

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

The YAML files are kept in git. Changing one means re-indexing only that type.

### One LLM call per datasheet

Runs only for datasheets whose type (from Step 0) has a spec list.
Input: the markdown + that type's spec list. Output (JSON, temperature 0):
- every part, or for families every row of the ordering/availability table, with its key specs
  **in the units of the spec list** and the **page** each value came from.

```json
{"type": "mosfet_n",
 "parts": [{"part_number": "2N7000",
   "specs": {
     "package":            {"value": "TO-92", "page": 4, "raw": "TO-92"},
     "vds_max":            {"value": 60,  "unit": "V",   "stat": "max", "conditions": {},
                            "page": 1, "raw": "60"},
     "id_max":             {"value": 0.2, "unit": "A",   "stat": "max", "conditions": {"ta": "25 °C"},
                            "page": 1, "raw": "0.2"},
     "rds_on_max@vgs_10v": {"value": 5,   "unit": "ohm", "stat": "max",
                            "conditions": {"vgs": "10 V", "id": "500 mA"}, "page": 2, "raw": "5"}}}]}
```

Every value uses the same fields as the full design (`value`, `unit`, `stat`, `conditions`, `page`, `raw`),
even where v1 doesn't use all of them yet. Ranges use `min` / `max` instead of `value`. See section 11.

A family (like the Viking MLCC) gives one entry per availability row, with ranges:
`0603 (inch) / X7R / 50 V → capacitance 150 pF – 470 nF, page 5`, plus the page of the ordering
("Part Numbering") table, used later to build an exact part number.

**Two checks in code:** each value's `raw` text must appear on the cited page, and every range must have
min ≤ max. A value that fails is marked *unverified* and shows a warning. This catches most invented or broken values.

- Supplier = folder name. Only new or changed files are indexed (file hash), like the report pipeline.
- Datasheets of types without a spec list are stored with their type only; requests for those types
  answer "type not supported yet" until a spec list is added.

### Watch out for (seen in the sample datasheets)

- **Test conditions:** RDS(on) is given at VGS=10 V and 4.5 V → separate specs in the list.
- **min / typ / max:** the spec list says which one (e.g. `vgs_th_max`), so like is compared with like.
- **Package names:** `0603` can be inch or metric → the spec list says which; the LLM writes the standard name.
- **Typos in the source** (e.g. `225~1.6` in the MLCC table): the raw-text check doesn't catch these, because
  the text really is on the page. A simple check in code that every range has min ≤ max marks them unverified.

---

## 3. Step 2: Shortlist (per request)

1. **Saved answers first:** engineer-confirmed matches for this part number are shown first; rejected
   ones are shown with their reason.
2. **Get the requested part's specs:** from `external_parts` if fetched before, otherwise the existing web
   pipeline → markdown → **the same Step 1 LLM call** → the exact part number's specs (for a family part
   the LLM reads the ordering table to decode it). If no manufacturer is given and several parts fit,
   **ask the salesperson** which one.
3. **Filter and score in plain Python:** only catalogue parts of the **same type**, scored by the
   share of key specs met (critical specs count more). Take the top ~10; if equally good parts come from
   different suppliers, list all of them.

---

## 4. Step 3: Spec table (the screenshot)

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
(or a question back to the salesperson). The Open WebUI tool calls it and renders the table.

---

## 5. Step 4: Confirm

```
proposed ──► sales confirms ──► engineer confirms ──► confirmed (a fact)
             (only a note)  └──► engineer rejects  ──► rejected, with a written reason (still shown)
```

- A confirmation covers only the **two exact part numbers**.
- The match stores a copy of the compared specs. When a datasheet is re-indexed and one of those specs
  changed, the match goes to **needs re-check**.
- "Engineer" = a role in your own system.

---

## 6. Storage

```sql
catalog_parts  (id, supplier, file, file_hash, extractor_version, type, part_number,
                family_row text, ordering_page int, specs jsonb, indexed_at);
external_parts (id, manufacturer, part_number, file, file_hash, extractor_version, type,
                specs jsonb, fetched_at);
matches        (id, external_part_id, catalog_part_id, concrete_pn, label, score,
                compared_specs jsonb,
                status,        -- proposed | sales_confirmed | confirmed | rejected | needs_recheck
                decided_by, decided_role, decided_at, reason);
-- + spec_lists/*.yaml files
```

**Our parts and external parts are never mixed:** separate tables and separate folders
(`markdowns/suppliers/…`, `markdowns/external/…`). `matches` is the only table that points to both.

---

## 7. Time per request

| Case | Time |
|---|---|
| Never seen before | **~1–3 min**: find + download + convert the datasheet (20 s – 2 min), index it (20–60 s), shortlist (< 1 s), part numbers for families (10–30 s) |
| Seen before | seconds |
| Confirmed match exists | instant |

Estimates; measure on ~20 real requests.

---

## 8. Testing

- **Starter test set:** an engineer writes ~30–50 requests with the correct alternative and label.
  Every confirmed or rejected match is added later.
- **Measure:** is the engineer's answer in the top 10? Does the label agree?
- **Spot-check extraction:** ~30 datasheets, key specs compared by hand.

---

## 9. Phases (~5–8 weeks)

| Phase | Work | Done when |
|---|---|---|
| 0 | Classify all datasheets by type (Step 0); group the labels into a type list; pick the first ~10 types | every datasheet has a type; counts per type known |
| 1 | Spec lists for the first ~10 types (~1–1.5 days), then index their datasheets | the first 10 types have key specs, checked by a trial run |
| 2 | Request flow, shortlist, spec table in Open WebUI | a real part number returns the table |
| 3 | Confirm / reject, test set, measurements | results measured on the test set |

**Adding a type later = creating one spec list (~45–60 min, as above) + indexing that type.** No code changes.

---

## 10. When to add more (from the full design)

| Add | Only when |
|---|---|
| Raw-facts layer + alias dictionary | key specs come out inconsistent across suppliers |
| Deterministic part-number generator for families | the LLM gets family part numbers wrong |
| More specs per type / weighted scoring | the engineer's answer is often missing from the top 10 |
| LLM review of candidates' full datasheets | engineers often find problems the key specs didn't show |
| Automatic type tree for all ~200 types | after the first ~10 types work well |
| Package alias table, separate DB users, per-type SQL views | before wider production use, or when reports are needed |

---

## 11. Upgrade path to the full design

**Mostly no rewrite.** About half of v1 stays as it is; the rest is extended or replaced piece by piece.
The one real replacement is the inside of the extractor, and it stays cheap only if v1 follows the rules below.

### Part by part

| Part of v1 | When upgrading | What happens |
|---|---|---|
| Open WebUI tool + spec table | **stays** | Same `find_alternatives` entry point and the same table format. |
| Fetching the external datasheet | **stays** | Same web search → PDF → markdown. |
| Confirm / reject flow + `matches` table | **stays** | Already in its final shape: statuses, reason, compared specs, re-check. |
| Test set + measurements | **stays** | This is how each upgrade is shown to help. |
| Step 0 type list | **stays** | Becomes the starting point of the automatic type tree. |
| Spec-list YAML files | **extended** | They become the registry: add aliases, conditions and importance, load them into the database. |
| Storage (`catalog_parts`, `external_parts`) | **extended** | The full design also keeps specs as JSON per part. Tables are added around it (documents, families, a flattened values table built from the JSON, the vocabulary). Existing data is migrated by a script, not re-extracted. |
| Shortlist + scoring | **extended** | Same function; more specs, per-spec penalties and an SQL pre-filter are added. The label rules stay. |
| Family part numbers | **replaced** (small) | The LLM call is swapped for the decoder-based generator, with the same input and output. |
| Extractor (one LLM call per datasheet) | **replaced** | Becomes triage + table-by-table reading + alias dictionary + unit conversion in code. The biggest change; its output format stays the same, so nothing after it notices. |
| LLM review, validation rules, corrections, separate DB users | **new** | Separate additions; nothing existing changes. |

**Effort:** a complete upgrade is roughly **10–16 more weeks** (with agentic coding, full-time), compared with
15–24 weeks for the full design from scratch. In practice only the pieces the tests call for are added
(section 10).

### Rules for v1 that keep the upgrade cheap

1. **Full value format from day one.** Every value carries `value` (or `min`/`max`), `unit`, `stat`,
   `conditions`, `page` and `raw`, as in the example in section 2. Storage, scoring and the table then don't
   care which extractor produced a value.
2. **Conditions and min/typ/max in the spec names**, e.g. `rds_on_max@vgs_10v`. These become the
   registry's keys; renaming them later means migrating data and confirmed matches.
3. **Each step a separate module with a fixed input and output:**
   `classify(markdown) → type`, `extract(markdown, spec_list) → parts with specs`,
   `shortlist(requested_part) → candidates`, `label(requested, candidate, spec_list) → label`,
   `build_part_number(candidate, requested) → part number`, `render(result) → table`.
   Replacing the extractor then means swapping one module.
4. **Spec lists written in the registry's terms:** `unit`, `rule`, `critical`, optional `aliases`,
   so they load into the registry one to one.
5. **File hash and extractor version stored on every part**, so a new extractor re-processes only what changed.
6. **Measure from the first week:** rows extracted vs rows in the table (family datasheets), and whether the
   engineer's answer is in the top 10. These numbers say which upgrade, if any, is worth doing.

### What would force a rewrite

Breaking rules 1–3: specs stored as free-form JSON with varying names, or extraction, scoring and display
mixed in one script. Then everything after the extractor depends on how the extractor works, and replacing
it breaks the rest.

---

## 12. Decisions (review 2026-11-17)

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
| 13 | Chat tool | Demo data now; will call `find_alternatives` |
| 14 | Who is an engineer | A role in your own system |
| 15 | Drawings (mechanical / electromechanical) | Ignored for now; `visual_check` notes still shown |
| 16 | Scope of v1 | Simple version: key specs for shortlisting, side-by-side spec table, engineer decides |

### Still open

- The real priority list of component types, once request and sales data is available.
- How much engineer time is available for the spec lists and the starter test set.
