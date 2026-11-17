# Finding alternatives from supplier datasheets: plan

Written 2026-11-17. Input: ~10,000 datasheet markdowns from 110 suppliers, about 200 component types,
all in English, with tables already converted correctly. Everything runs locally; only web search reaches the internet.
Goal: for a part number we don't sell, return the closest offerings from our suppliers, labelled
**Drop-in / Functional / Partial**, with every compared value traceable to a datasheet page.

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

```sql
-- taxonomy & registry (generated automatically, reviewed lightly)
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

-- catalogue
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

-- requested (external) parts
requested_parts (id, part_number, manufacturer, document_id, category_id, attrs jsonb,
                 decode_status, fetched_at);

-- results & feedback
matches (id, requested_id, offering_id, concrete_pn, label, score, coverage,
         diff jsonb, warnings jsonb,
         status text,     -- proposed | sales_confirmed | engineer_confirmed | rejected
         confirmed_by, confirmed_at, note);

extraction_issues (document_id, page, attr_key, raw, problem, resolved bool);
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
2. **Requested part**: from the `requested_parts` cache, or via the existing web pipeline → markdown →
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

## 7. Confirmation loop

`proposed` → sales clicks confirm → `sales_confirmed` (shown, but not treated as a fact) → engineer
confirms → `engineer_confirmed`. Only engineer-confirmed rows:
- are pinned as the top answer for that requested part number,
- go into the evaluation set,
- are used to tune importance weights per category later (which specs engineers actually accept as worse).

Corrections to extracted values (an engineer fixes "VDS = 600 V" to 60 V) are stored as overrides that
survive re-extraction.

---

## 8. Evaluation (set up before tuning anything)

- **Matching**: historical cross-references (requested part → accepted alternative). Metrics: recall@1,
  recall@5, label agreement, and the share of results that carry a warning. Run on every schema, extractor
  or scoring change.
- **Extraction**: hand-check ~50 datasheets across the top categories. Precision and recall of critical
  attributes, accuracy of unit conversion, decoder round-trip accuracy.
- Keep a regression folder of difficult datasheets: multi-condition specs, decoders, typos (like the X7S `225~1.6` row).

---

## 9. Phases

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

## 10. Open questions

1. What do the historical cross-references look like (format, how many, do they include the label
   drop-in / functional)? They decide the evaluation set and the weight tuning.
2. Is there an existing package or footprint list in your ERP or CAD library (land patterns)? That would
   seed `package_aliases`.
3. Which local model and GPU: does a ~30B+ model with 32k context fit? This sets how big the extraction chunks are.
4. Does the datasheet folder structure encode the supplier (`markdowns/<supplier>/...`)?
5. How are mechanical and electromechanical parts (bearings, relays, connectors) specified in your
   datasheets? Connectors and mechanical parts depend much more on drawings, so most of them will
   carry `requires_visual_check`.
6. Should "Partial" results show parts with a failing **critical** spec at all, or only with a warning?
