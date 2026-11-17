# Finding alternatives from supplier datasheets: plan

Written 2026-11-17. Input: ~10,000 datasheet markdowns from 110 suppliers, about 200 component types,
all in English, with tables already converted correctly. Everything runs locally; only web search reaches the internet.
Goal: for a part number we don't sell, return the closest offerings from our suppliers, labelled
**Drop-in / Functional / Partial**, with every compared value traceable to a datasheet page.

---

## 0. Overview: the four stages

```
 ONE-TIME + EVERY 2 WEEKS                          PER CUSTOMER REQUEST
┌───────────────┐   ┌───────────────┐        ┌───────────────┐   ┌───────────────┐
│ 1. EXTRACT    │──►│ 2. STORE      │◄──────►│ 3. MATCH      │──►│ 4. CONFIRM    │
│ supplier .md  │   │ one database, │        │ external part │   │ sales → eng.  │
│ → spec values │   │ standard spec │        │ vs. our parts │   │ → saved match │
└───────────────┘   │ names & units │        └───────────────┘   └───────┬───────┘
                    └───────▲───────┘                                    │
                            └────────── confirmed matches ───────────────┘
```

### Stage 1. Extract: turn supplier markdowns into spec values

**When:** once for all ~10k files, then every two weeks only for new or changed files.

1. **Classify each datasheet.** Which supplier, which component type (for example "N-channel MOSFET" or
   "MLCC capacitor"), and whether it describes one part or a whole family.
2. **Read the tables.** Keep the spec tables and skip packaging, reel and reliability-test tables. Each spec
   becomes one value: name, value, unit, min/typ/max, test condition, and the page it came from.
3. **Translate the names into one shared vocabulary.** `VDS`, `V(BR)DSS` and "Drain-Source Voltage" all
   become `drain_source_voltage`, in volts. Each component type has its own list of specs, generated
   automatically and checked by an engineer only for the important types.

**Result:** every supplier part is described the same way, whichever supplier wrote the datasheet.

### Stage 2. Store: one database, three separate areas

```
┌─────────────────────────────┐   ┌──────────────────────────────┐   ┌───────────────────────────────┐
│ CATALOGUE (our suppliers)   │   │ SHARED VOCABULARY            │   │ EXTERNAL (requested parts)    │
│ suppliers, datasheets,      │   │ component types, spec names, │   │ manufacturers, datasheets,    │
│ families, variants, specs   │   │ units, aliases, packages     │   │ parts, specs                  │
│ written only by Stage 1     │   │ (words only, no part data)   │   │ written only by Stage 3       │
└──────────────┬──────────────┘   └──────────────────────────────┘   └───────────────┬───────────────┘
               └────────────────────► MATCHES (one catalogue part ↔ one external part) ◄┘
```

- **Catalogue and external parts are never mixed.** They have separate tables, separate datasheet folders
  and separate writers. A catalogue search can't return an external part.
- **They share only the vocabulary:** the same spec names and units on both sides, so they can be compared.
- **Matches is the only place both sides meet:** one row links a catalogue part and an external part.

What the catalogue holds (the external area has the same structure, but for external parts):

- **Parts:** supplier → datasheet → family → variant (for example one MLCC size / voltage / dielectric).
- **Spec values:** one row per part per spec, in standard units, linked back to the datasheet page.
  That link is the proof shown to the engineer.
- **Spec list per component type:** which specs that type has and how each one is compared. For example:
  package must be the same, voltage rating must be equal or higher, resistance must be equal or lower.
- **Generated tables:** code builds one readable table per component type from the above. Nobody
  maintains 200 tables by hand.

### Stage 3. Match: one external part number against our parts

**Input:** the customer's part number plus its specs. The web pipeline finds its datasheet and
converts it to markdown.

1. **Put the external specs through the same translation as Stage 1, step 3**, so both sides use the same
   names and units. Without this the comparison doesn't work.
2. **Check for a saved, engineer-confirmed match.** If one exists, show it first.
3. **Narrow down:** keep only our parts of the **same component type**, and drop the clearly impossible
   ones, such as a much lower voltage rating.
4. **Compare spec by spec** using each spec's comparison rule, and score the result as the share of
   important specs met.
5. **Label each result:**
   - **Drop-in:** same footprint and all important specs met.
   - **Functional:** important specs met, but a different package or some minor specs are worse.
   - **Partial:** the best available, with the failing specs listed.
6. **Warn** when a spec is missing on either side, when test conditions differ, or when something can
   only be checked in a drawing, like pinout.

**Output:** the comparison table from the reference screenshot. Each value comes with its datasheet page.

### Stage 4. Confirm: saving a match as a known fact

```
proposed ──► sales confirms ──► engineer confirms ──► SAVED
             (only a note)      (now a fact)
```

A saved match:
- is shown first the next time anyone asks for that part number,
- becomes a test case for checking that system changes don't make matching worse,
- over time shows which specs engineers accept being worse, which improves the scoring.

If an engineer finds a wrongly extracted value, the correction is saved too, and it stays when the
datasheet is processed again.

### Example

A customer asks for **2N7000** (60 V N-channel MOSFET, TO-92 package).

1. Its datasheet is extracted into standard specs: drain_source_voltage 60 V, drain_current 0.2 A,
   RDS(on) max 5 Ω @ VGS=10 V, package TO-92.
2. The database returns our N-channel MOSFETs with at least about 50 V and at least about 0.2 A.
3. The comparison finds one supplier part in TO-92 that meets every important spec → **Drop-in**.
   A SOT-23 part with the same specs → **Functional**, because the package differs. A note says to check
   the pinout on the drawing.
4. A salesperson clicks confirm, then an engineer confirms. From then on, 2N7000 → that part is the
   saved answer.

The sections below give the details of each stage.

---

## 1. The core idea

Don't make one SQL table per category, and don't run vector search over the markdowns.

- **One generic store.** Categories and their attributes are *rows* in a registry. Part specs are
  stored as typed values (point, range, set, enum) with units converted to SI. A new category is new
  registry rows, not a migration.
- **Specs come from LLM extraction, guided by the category schema.** The LLM copies raw strings out of
  the tables. Code does the rest: converts units, expands part-number codes, checks values.
- **Matching is deterministic.** SQL filters, then Python scores against per-attribute comparison rules.
  An LLM only reviews the top results and adds warnings. It cannot change the numbers. This makes the
  results reproducible and lets every result show its evidence.
- **The requested part goes through the same extractor and schema as the catalogue.** That way both sides
  of the comparison use the same attribute keys, units and test conditions.

```
               (offline, every two weeks, hash-based like the report pipeline)
markdowns ─► triage ─► category ─► schema-guided extraction ─► normalize + validate ─► families / offerings
                         ▲                                                             │
           taxonomy + attribute registry (auto-generated, light review)                │
                                                                                       ▼
part number ─► catalogue? ─► cache? ─► web fetch → md → same extraction ─► SQL candidates ─► scoring ─► LLM review ─► table
                                                                                              ▲
                                                                     engineer-confirmed cross-refs
```

---

## 2. What the two sample datasheets show

| Observation | Sample | Design consequence |
|---|---|---|
| A family datasheet describes a **space of part numbers**: a part-number decoder plus an availability matrix, with no list of parts | IM Series MLCC: 8 sizes × 5 dielectrics × ~15 voltages × E-series values × 9 tolerances | Store an **offering** as one row of the availability matrix, with *range/set* values (`size=0603, V=50V, X7R, C∈[150pF,470nF], tol∈{F,G,J,K,M}`), and build the exact part number from the decoder. Don't expand into millions of rows. |
| The same parameter is given **under different test conditions** | 2N7000 RDS(on): 6 Ω @ VGS=4.5 V and 5 Ω @ VGS=10 V | Conditions are part of the attribute's identity: `rds_on_max@vgs_10v` and `rds_on_max@vgs_4v5` are separate keys. Comparing different conditions → warning, not a match. |
| min / typ / max columns | 2N7000 VGS(th) gives min 0.8 V and max 3 V, no typ | Store which statistic each value is. Compare max with max. If only typ exists on one side, show the value and warn. The reference screenshot compares "V(GS)th typ", which the 2N7000 sheet doesn't have. |
| Units written wrong | `ms` for transconductance (should be mS), `nS` | Convert units **using the attribute's expected dimension** from the schema: siemens → `ms` = mS. Never guess from the string alone. |
| Typos and broken values in the source | `225~1.6【1.6】` (X7S, 1206, 6V3); `NOP(COG)` | Validation rules (range order, code parse, value grounded in source) send these to a review list. The row is not silently dropped. |
| Codes stand for numbers | `104` = 100 nF, `3R9` = 3.9 pF, `6V3`, `【0.8】` = thickness | A small library of code parsers (EIA cap/res codes, voltage codes, tolerance letters). Code, not the LLM, expands them. |
| The package name is ambiguous | `0603` here is inch; metric 0603 is a much smaller part | Package **normalization dictionary** with explicit systems: `0603-imperial` = `1608-metric`; `SOT-23` ≈ `TO-236AB`. Footprint is the main must-match, so it needs its own table. |
| Same function, different package | 2N7000 (TO-92, through-hole) vs 2N7002 (SOT-23) | Becomes **Functional**, not Drop-in. This is exactly the case the label exists for. |
| Pinout lives only in a drawing | 2N7000 page 4 (images) | The category has `requires_visual_check` set: "Confirm pinout (G/D/S order) on the package drawing." |
| Enum values can be "better" than others | X7R (-55…125 °C, ±15%) can replace X5R (-55…85 °C, ±15%) | Turn enums into numbers wherever the datasheet defines them (dielectric → temperature range + ΔC). Compare the numbers instead of the names. |

---

## 3. Data model (Postgres + pgvector)

Four Postgres schemas. **`catalog` and `external` never reference each other**: they share only `vocab`,
and only `matching` points to both.

| Schema | Holds | Written by | Read by |
|---|---|---|---|
| `vocab` | component types, spec names and units, aliases, package dictionary | Stage 1 + engineer review (new aliases found in external datasheets may be added: words, not part data) | everyone |
| `catalog` | our suppliers' datasheets, families, variants, spec values; generated per-type views | supplier extraction (Stage 1) only | matching (read-only) |
| `external` | requested parts: their datasheets, families, parts, spec values | request pipeline (Stage 3) only | matching (read-only) |
| `matching` | proposed / confirmed matches | matching + confirmation flow | everyone |

Rules that keep them apart:
- Separate file folders: `markdowns/suppliers/…` and `markdowns/external/…`.
- Separate database users: the supplier pipeline can't write to `external`, and the request pipeline
  can't write to `catalog`. The matcher only reads both.
- The generated per-type views (e.g. `catalog.mosfet_n`) are built from `catalog` only.
- An engineer correcting an external value saves it in `external`, never in the catalogue.
- If an external manufacturer later becomes a supplier, their datasheets go through the supplier pipeline
  from scratch. External rows are never moved into the catalogue.

```sql
-- ── vocab: shared vocabulary (generated automatically, reviewed lightly) ──
categories (
  id, parent_id, key, name, description,
  requires_visual_check bool, visual_check_note text,    -- e.g. "confirm pinout"
  schema_status text,          -- auto | reviewed
  schema_version int
);

attributes (
  id, category_id,             -- NULL category_id = universal attribute
  key text,                    -- 'rds_on_max@vgs_10v'
  label text, symbol_aliases text[],   -- {'RDS(ON)','R DS(on)','Drain-Source On-Resistance'}
  dimension text,              -- 'resistance' -> canonical unit ohm
  value_kind text,             -- number | range | set | enum | bool | text
  stat text,                   -- min | typ | max | nominal
  conditions jsonb,            -- {"vgs":"10 V","ta":"25 °C"}
  comparison text,             -- gte | lte | eq | near | contains_range | superset | info
  tolerance jsonb,             -- for 'near': {"rel":0.0} for capacitance, {"rel":0.05} ...
  importance text,             -- critical | important | minor | info
  is_footprint bool,           -- package / pitch / pin count -> drop-in gate
  status text                  -- auto | reviewed
);

package_aliases (alias text, canonical text, system text, pin_count int, mounting text);

-- ── catalog: our suppliers only ──
suppliers  (id, name, summary, categories text[]);
documents  (id, supplier_id, path, sha256, extractor_version, extracted_at, status);
families   (id, document_id, category_id, series, title, description,
            pn_decoder jsonb,           -- segments -> attribute values
            shared_attrs jsonb,         -- values that hold for every offering
            embedding vector);          -- for triage / fallback search only
offerings  (id, family_id,
            part_number text NULL,      -- concrete PN when the datasheet lists one
            pn_template text NULL,      -- '03{tol}T B 500 {cap}' when generated
            attrs jsonb);               -- shared_attrs merged with row-specific values
offering_values (                       -- flattened copy of attrs, for SQL filtering
  offering_id, attr_key, num_lo, num_hi, text_val, set_val text[], stat, confidence);

extraction_issues (document_id, page, attr_key, raw, problem, resolved bool);

-- ── external: requested parts only (same structure, no links to catalog) ──
manufacturers (id, name);
documents     (id, manufacturer_id, source_url, path, sha256, extractor_version, fetched_at, status);
families      (id, document_id, category_id, series, pn_decoder jsonb, shared_attrs jsonb);
                                         -- kept so other part numbers of the same family decode instantly
parts         (id, family_id, part_number, manufacturer_id, category_id,
               decode_status, attrs jsonb, requested_count int, last_requested_at);
part_values   (part_id, attr_key, num_lo, num_hi, text_val, set_val text[], stat, confidence);
value_overrides (part_id, attr_key, corrected_value jsonb, corrected_by, corrected_at);
extraction_issues (document_id, page, attr_key, raw, problem, resolved bool);

-- ── matching: the only place both sides meet ──
matches (id,
         external_part_id,          -- → external.parts
         catalog_offering_id,       -- → catalog.offerings
         concrete_pn, label, score, coverage, diff jsonb, warnings jsonb,
         status text,     -- proposed | sales_confirmed | engineer_confirmed | rejected
         confirmed_by, confirmed_at, note);
```

**Format of one stored attribute value.** Every value carries its proof:

```json
"rds_on_max@vgs_10v": {
  "kind": "number", "value": 5.0, "unit": "ohm", "stat": "max",
  "conditions": {"vgs": "10 V", "id": "500 mA", "ta": "25 °C"},
  "source": {"doc": 4711, "page": 2, "table": 1, "row": "Drain-Source On-Resistance",
             "raw": "5 | Ω"},
  "confidence": "grounded"   // grounded | inferred | unverified
}
```

`grounded` means the raw string was found word for word on the cited page. Checking this is cheap and
catches most hallucinations.

---

## 4. Building the taxonomy and schemas once (keeping engineer time low)

1. **Triage every document** (local LLM, pages 1–2 only, JSON schema, temperature 0): supplier, series,
   free-text component type, family or single part, does it have an ordering/decoder table, and the
   headline specs from the front-page table. What manufacturers put on page 1 is the best available signal
   for which specs matter most.
2. **Cluster into a taxonomy**: embed the triage outputs and cluster them. The LLM names and merges
   clusters into a tree of about 200 leaves. An engineer reviews the tree once, in about 1–2 h.
3. **Generate a schema per category**: for each leaf, sample 10–20 datasheets from different suppliers.
   The LLM proposes attributes with key, aliases, dimension, stat, conditions, **comparison direction**
   and **importance** (headline specs → critical). Merge synonyms across suppliers
   (`V(BR)DSS` = `VDS` = "Drain-Source Voltage").
4. **Universal attributes** shared by all categories: package/case (normalized), mounting type,
   pin count / pitch, operating temperature range, qualifications (AEC-Q100/101/200, etc.), RoHS,
   lifecycle status.
5. **Review only where the volume is.** Engineers check the schemas of the top 10–20 categories by
   requests and sales. All other categories run on `schema_status = auto`, and their results show
   "category schema not reviewed". This fits within "a few hours per month".

Comparison semantics:

| comparison | meaning | examples |
|---|---|---|
| `eq` | must be equal | package, channel type (N/P), polarity, pin count |
| `gte` / `lte` | equal or better in one direction | VDS, ID ≥; RDS(on), leakage, tolerance ≤ |
| `near` | same nominal value, within a tolerance | capacitance, resistance, frequency, coil voltage |
| `contains_range` | candidate range must cover the requested range | operating temperature, input voltage range |
| `superset` | candidate must have everything requested; extras are fine | qualifications (AEC-Q ⊇ none), certifications |
| `info` | shown, not scored | packaging quantity, marking |

---

## 5. Extraction pipeline (offline, incremental)

Same pattern as `dataPipeline/convert/docling.py`: SHA-256 per file, only new or changed files,
`extractor_version` stored so that a schema change re-extracts only the categories it affects.

1. **Triage** (above). This assigns the category; low confidence goes to a review list.
2. **Schema-guided extraction**, per document or per page group. Prompt = the category's attribute list
   (keys, aliases, expected dimension) + markdown. Output JSON with **raw strings** + page + table + row label.
   Families are extracted in three parts: `shared_attrs`, `pn_decoder` (segments → values) and
   `availability rows`.
3. **Normalization in code**: unit conversion with `pint`, guided by dimension (`ms` + siemens → mS); EIA code
   parsers; package aliases; conditions parsed into canonical keys.
4. **Expansion in code**: availability rows → offerings with range/set values; the decoder → `pn_template`.
5. **Validation**: dimension check, min ≤ max, values grounded in the source, plausibility bounds per
   dimension, decoder round-trip (encode → decode gives the same values). Failures go to
   `extraction_issues` and the value becomes `unverified`, which is still usable but shown with a warning.
6. **Schema drift**: attributes the LLM finds that aren't in the schema are collected. A periodic job
   proposes new attributes or aliases.

Models: a strong local instruction model with structured JSON output (Ollama `format=SCHEMA`,
`temperature 0`, as the RAG code already does). The largest sample (693 lines) fits easily in 32k context.
Long documents are processed in page groups and merged.

---

## 6. Matching at query time

1. **Normalize the part number** and look it up in our catalogue, including decoder matches (it may
   be ours under a different code).
2. **Requested part**: from `external.parts` (already fetched before), or via the existing web pipeline → markdown →
   the **same** triage + extraction → **decode the specific part number** inside its family
   (e.g. Murata `GRM188R71H104KA93D` → 0603-imp, X7R, 50 V, 100 nF, ±10%).
3. **Category gate**: candidates must have the **same leaf category**. No cross-category matches
   (a Schottky never matches a standard rectifier). If the requested part's category is uncertain, say so.
4. **SQL candidate retrieval** on `offering_values`: critical attributes with loose bounds (e.g. VDS ≥ 0.8×,
   capacitance within ±20%) → about 200 candidates. Loose bounds let "Partial" results still appear.
5. **Scoring in Python, one score per attribute**:
   - meets the requirement → 1
   - worse → penalty that grows with the log ratio (slightly worse costs less than 10× worse)
   - missing on one side → 0 and the attribute counts as "not stated" (lowers *coverage*, not just the score)
   - different conditions or statistic → shown, scored at 0.5, warning
   - Score = weighted share of requested specs met (critical 5, important 2, minor 1). This is the "% of weighted specs met" in the screenshot.
6. **Labels**:
   - **Drop-in**: footprint attributes all `eq` AND every critical + important spec met.
   - **Functional**: every critical spec met; footprint differs and/or some important specs are worse.
   - **Partial**: everything else; ranked by score, with the failing specs listed.
   - Always return the best results. In addition, show a **confidence warning** when coverage is low,
     values are `unverified`, the category or decode is uncertain, the schema isn't reviewed, or
     `requires_visual_check` is set.
7. **For families: build the concrete part number** with the cheapest matching options
   (requested capacitance, tolerance ≤ requested, voltage ≥ requested), e.g. Viking `03KTB500104`
   for the Murata example above (0603, 50 V, X7R, 100 nF; available per the X7R table, 151~474【0.8】).
8. **LLM review of the top 5–10**: it gets the requested and candidate spec JSON plus the cited
   markdown pages. It can **only add** warnings and a one-line justification (pinout note,
   "not for new designs", a condition the schema missed). It cannot change scores or labels.
9. **Output JSON**: requested, candidates[label, score, coverage, per-attribute {req, cand, verdict,
   source page}], warnings, visual-check notes. The chat tool renders it as the table.
   **Engineer-confirmed** cross-references for this part number always appear first.

---

## 7. Performance: time for a part that hasn't been seen or saved

**Usually 1–4 minutes for a part nobody has asked about before.** Almost all of that is getting the
external part's datasheet and reading it. The matching itself takes seconds. These are estimates: the real
numbers depend on the GPU and the local model, so measure them on about 20 real requests.

### Time for one new part number

| Step | What happens | Estimated time |
|---|---|---|
| 1. Find the datasheet | web search, pick the right PDF, download it | 10–60 s |
| 2. PDF → markdown | Docling, typical 4–15 pages | 10–60 s (longer if the PDF is scanned and needs OCR) |
| 3. Extract its specs | classify the datasheet, read its ~5–15 spec tables, map names to the standard ones (mostly dictionary hits), decode the part number if it's from a family | 30 s – 2 min |
| 4. Find and score candidates | database query within one component type, compare spec by spec | under 1 s |
| 5. LLM check of the top 5–10 | read the cited pages, add warnings | 20–60 s (optional, can run after the table is shown) |
| **Total** | | **about 1–4 min** |

How the other cases compare:

| Case | Time |
|---|---|
| Asked before, not confirmed (specs already stored) | seconds, up to ~1 min with step 5 |
| Engineer-confirmed match saved | instant |
| Specs given to the system directly, no datasheet | seconds: only name mapping + steps 4–5 |

### How complex the per-request search is

- **There's little new code in it.** Steps 1–3 reuse the same extraction the supplier datasheets need
  anyway. The only new parts are the database query, the spec-by-spec comparison and the labels. That's
  a few hundred lines of ordinary code, with no AI in the scoring itself.
- **Search time doesn't grow with the 10k datasheets.** Filtering by component type first leaves a few
  hundred to a few thousand candidates, and scoring those is trivial.
- **The heavy work happens beforehand.** Extracting, standardizing and storing 10k datasheets is done
  offline. That's why a request can be fast.

### What makes a request slower or harder

- Scanned PDFs, which need OCR.
- Very large family datasheets (100+ pages). Fix: read only the part-numbering and spec pages.
- The manufacturer isn't given, and the part number matches several different parts. The system has to
  pick one or ask the user.
- The component type is rare and its spec list hasn't been reviewed. It still works, but it shows a warning.

### Ways to speed it up

- Store every external datasheet and its specs in the `external` area (never in the catalogue), so the second request for that part takes seconds.
- Show the comparison table as soon as step 4 finishes, and add the step-5 warnings when they're ready.
- Pre-process common competitor parts in the two-weekly run, for example parts from past requests.

---

## 8. Confirmation loop

`proposed` → sales clicks confirm → `sales_confirmed` (shown, but not treated as a fact) → engineer
confirms → `engineer_confirmed`. Only engineer-confirmed rows:
- are pinned as the top answer for that requested part number,
- go into the evaluation set,
- are used to tune importance weights per category later (which specs engineers actually accept as worse).

Corrections to extracted values (an engineer fixes "VDS = 600 V" to 60 V) are stored as overrides that
survive re-extraction.

---

## 9. Evaluation (set up before tuning anything)

- **Matching**: historical cross-references (requested part → accepted alternative). Metrics: recall@1,
  recall@5, label agreement, and the share of results that carry a warning. Run on every schema, extractor
  or scoring change.
- **Extraction**: hand-check ~50 datasheets across the top categories. Precision and recall of critical
  attributes, accuracy of unit conversion, decoder round-trip accuracy.
- Keep a regression folder of difficult datasheets: multi-condition specs, decoders, typos (like the X7S `225~1.6` row).

---

## 10. Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 | Evaluation set from historical cross-refs; pick the top 10 categories | ≥100 known pairs loaded |
| 1 | Triage all ~10k docs; taxonomy; package dictionary | every doc has a category or sits in the review list |
| 2 | Schemas + extraction + matching for the top 10 categories, end to end with the chat tool | recall@5 measured on the evaluation set |
| 3 | Auto schemas for the remaining ~190 categories; fallback path | every category returns results (with "unreviewed" warnings) |
| 4 | Confirmation workflow, two-weekly incremental runs, override handling | engineer-confirmed rows pinned and evaluated |

Phases 2→3 add no new code: a new category is registry rows plus a re-run of extraction.
This is what makes the system extendable rather than rewritten per category.

---

## 11. Open questions (for review)

**A. Change the design if the answer differs from what's assumed**

1. **Datasheet updates vs saved matches.** When a supplier or external datasheet changes, should matches
   confirmed against the old version be re-checked by an engineer, or stay valid unless a compared spec
   changed? *Assumed: stay valid unless a compared spec changed; otherwise flagged for re-check.*
2. **What a confirmation covers.** Does a confirmed match apply only to the exact part numbers
   (`GRM188R71H104KA93D` → `03KTB500104`), or also to other members of the same families?
   *Assumed: exact part numbers only.*
3. **Rejected matches.** When an engineer rejects a match, should that candidate be hidden for the same
   part number in the future, or only shown lower with the reason? *Assumed: shown lower, with the reason.*
4. **Specs not from a datasheet.** Will external specs ever come from somewhere other than a datasheet
   (customer email, distributor web page, typed in by sales)? *Assumed: always a datasheet, so every value
   has a page as proof. Other sources would need a "customer-provided" proof type and a warning.*
5. **Same part from several suppliers.** Can one part (same manufacturer part number) be available through
   more than one of our suppliers? Show it once with all suppliers listed, or once per supplier?
6. **Part number without a manufacturer** that matches several different parts: should the system ask
   the salesperson which one, or pick the most likely and warn? *Assumed: ask.*
7. **Partial results with a failing critical spec**: show them with a warning, or hide them?
   *Assumed: show, ranked last, with a warning.*

**B. Inputs needed to start**

8. Historical cross-references: format, how many, and do they say drop-in / functional?
   They become the evaluation set.
9. The top ~10 component types by requests and sales, to build and test first.
10. An existing package or footprint list in the ERP or CAD library, to start the package dictionary.
11. Which local model and context length will run extraction (sets how big each extraction chunk is).
12. Is the supplier visible from the datasheet folder structure (`markdowns/<supplier>/...`)?
13. How the chat tool (`partsfinder_find_alternatives`) calls the backend: a Python function, an HTTP
    API? This sets the interface of the matching service.
14. Who counts as an engineer for confirmation: a list of people, a role in an existing login system,
    one expert per component type?
15. For mechanical and electromechanical parts (bearings, relays, connectors): how much of the key
    information is only in drawings? Those types will mostly carry `requires_visual_check`.
