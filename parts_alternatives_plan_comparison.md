# Simple plan vs full design: the differences

This file compares the two plans for finding alternatives from supplier datasheets:

- **Simple plan (v1):** [`parts_alternatives_plan.md`](parts_alternatives_plan.md)
- **Full design:** [`parts_alternatives_full_design.md`](parts_alternatives_full_design.md)

Both solve the same problem, follow the same 15 review decisions, and produce the same result for the user:
the spec table from the reference screenshot, with Drop-in / Functional / Partial labels, a datasheet page
for every value, and the sales → engineer confirmation flow. **The difference is how much of the work is done
by code and stored data, and how much is left to the LLM and the engineer.**

**Recommendation:** build the simple plan first. Add pieces of the full design only when testing shows they
are needed (section 10).

---

## 1. At a glance

| | Simple plan (v1) | Full design |
|---|---|---|
| Main idea | find candidates using 5–10 key specs, show them side by side, engineer decides | extract and standardize every spec, so the system can decide on its own |
| Specs stored per part | 5–10 key specs per type | all specs in the datasheet |
| Extraction | one LLM call per datasheet | table by table: the LLM decides how to read each table, code reads the rows, then a dictionary maps names |
| Spec names | spec-list file per type; LLM writes values under those names | registry + alias dictionary that grows over time |
| Unit conversion | done by the LLM, into the spec list's units | done in code, checked against the expected unit type |
| Component types | you write a spec list per type, starting with ~10 | type tree built automatically for all ~200 types |
| Families (e.g. MLCC) | one entry per availability row; LLM builds the exact part number | code expands rows and generates part numbers from the decoder |
| Storage | 3 tables + YAML files | 4 database schemas, ~15 tables, generated per-type views |
| Matching | Python filter + % of key specs met | SQL filter + weighted per-spec scoring + LLM review |
| Labels | set by code from the spec list | set by code from the registry |
| Proof per value | page + raw-text check + min ≤ max check | page + table + row + raw-text check + validation rules |
| First working version | ~5–8 weeks | ~15–24 weeks |
| Code size | small | several times larger |
| Engineer time | spec lists (~10 min per type) + test set | type tree review + spec-list review + alias review |

---

## 2. Extraction: getting specs out of the markdowns

### Simple plan
- **One LLM call per datasheet.** Input: the markdown and the list of spec lists. Output: the component
  type, every part (or availability row), and its 5–10 key specs in the spec list's units, with page and raw text.
- **Two checks in code:** the raw text of each value must appear on the cited page, and every range must have
  min ≤ max. Otherwise the value is marked *unverified*.

### Full design
- **Triage call** (first pages only): type, family or single part, headline specs.
- **Layer 1, by table shape:** every table is split out. The LLM looks at its header and a few rows once,
  says what kind of table it is (parameter table, variant table, decoder, availability matrix, irrelevant) and
  what each column means. **Code then reads every row** using that mapping. Mappings are cached per header,
  so repeated supplier layouts cost nothing.
- **Layer 2, names:** each raw spec name (e.g. `V(BR)DSS`, "Drain-Source Voltage") is mapped to a standard
  spec through an alias dictionary. The LLM is asked only about names not seen before, and its answer is saved.
- **Code** converts units (using the expected unit type, so `ms` on a transconductance row is mS), parses
  part-number codes (`104` = 100 nF, `3R9` = 3.9 pF) and runs validation rules.

### What this means

| | Simple | Full |
|---|---|---|
| Rows skipped in long tables | possible: one call over a long document can miss rows | not possible: code reads every row |
| Invented or broken values | raw-text check + min ≤ max check | raw-text check + full validation rules (units, plausible values, part-number round trip) |
| Unit mistakes | possible, the LLM converts | rare, code converts and checks the unit type |
| Specs outside the spec list | not stored | all stored, so they can be used later without re-extracting |
| LLM calls for 10k datasheets | ~10k long calls | many short calls, fewer over time thanks to caching |
| Re-running after a change | re-run the LLM on the affected type | change the dictionary and re-run Layer 2 only, no LLM over the documents again |

---

## 3. Spec names and component types

### Simple plan
- **A spec-list file per type** (`spec_lists/mosfet_n.yaml`): 5–10 specs, each with its unit, comparison rule
  (`=`, `>=`, `<=`, `≈`, `contains`) and whether it's critical. The LLM drafts it, and a person checks it in about 10 minutes.
- **Types are added by hand**, starting with the ~10 with the most datasheets. Datasheets of other types are stored
  with their type only, and requests answer "type not supported yet".

### Full design
- **An automatic type tree** for all ~200 types: triage every document, cluster the results, let the LLM name
  and merge the clusters, then one engineer review of 1–2 h.
- **A schema per type generated from 10–20 sample datasheets**, with all specs, their importance, conditions,
  min/typ/max, and comparison rules. Engineers review only the most important types.
- **Shared specs** used by every type (package, mounting, temperature range, qualifications, RoHS, lifecycle).

### What this means
- The simple plan **covers fewer types at first**, but each one is checked by a person.
- The full design **covers everything quickly**, but most types run on unreviewed schemas and show a warning.

---

## 4. Families (one datasheet, many part numbers)

Example: the Viking MLCC datasheet, with 8 sizes × 5 dielectrics × ~15 voltages × many capacitance values × 9 tolerances.

| | Simple | Full |
|---|---|---|
| How stored | one entry per availability row, with ranges (`0603 / X7R / 50 V → 150 pF – 470 nF`) | same idea, but rows produced by code from the table mapping |
| Exact part number | LLM reads the ordering table and builds it (e.g. `03KTB500104`) | code builds it from the extracted decoder, and checks it by decoding it back |
| Risk | the LLM can build a part number that doesn't exist | wrong only if the decoder itself was extracted wrong |
| Requested part from a family | the LLM decodes it from the external datasheet | code decodes it with the stored decoder; later requests from the same family are instant |

---

## 5. Storage

### Simple plan
```
catalog_parts   (supplier, file, type, part_number, family_row, specs jsonb)
external_parts  (manufacturer, part_number, file, type, specs jsonb)
matches         (external part ↔ catalogue part, label, status, reason, compared specs)
spec_lists/*.yaml
```

### Full design
```
vocab     component types, specs, aliases, package dictionary
catalog   suppliers, documents, families, offerings, flattened spec values, extraction issues, per-type views
external  manufacturers, documents, families, parts, spec values, value corrections, extraction issues
matching  matches
```
plus separate database users per pipeline.

### What this means

| | Simple | Full |
|---|---|---|
| Our parts and external parts kept apart | yes: separate tables and folders | yes: separate schemas, folders and database users |
| SQL reports per type ("all our MOSFETs above 100 V") | possible with JSON queries, but awkward | easy: generated per-type views |
| Corrections by engineers | not stored separately; fixed by fixing the spec list or re-indexing | stored as corrections that survive re-extraction |
| Effort to set up and maintain | low | higher |

---

## 6. Matching

### Simple plan
1. Saved confirmed or rejected matches first.
2. Python: same type only, score = share of key specs met (critical specs count more), top ~10.
3. Labels from the spec list. Short LLM call only to build exact part numbers for family candidates.

### Full design
1. Saved matches first.
2. SQL: same type, loose bounds on critical specs → ~200 candidates.
3. Python: a score for every spec. Worse values are penalized by how much worse, and differing test
   conditions or min/typ/max get a half score and a warning. Coverage (how many specs are known) is
   reported separately from the score.
4. Labels from the registry.
5. **LLM review** of the top 5–10: it reads the candidates' cited datasheet pages and adds warnings the specs
   didn't show ("not for new designs", a condition missing from the schema). It can't change scores or labels.

### What this means

| | Simple | Full |
|---|---|---|
| Specs compared | 5–10 | all known specs |
| Same input → same result | yes (scoring and labels are code) | yes |
| A good part missed because one key spec was extracted wrong | possible | less likely: more specs, better extraction |
| Problems hidden outside the key specs | the engineer has to spot them | the LLM review flags many of them |
| Time per new request | ~1–3 min | ~1–4 min |

---

## 7. How each plan handles what the sample datasheets showed

| Issue (from 2N7000 and Viking MLCC) | Simple | Full |
|---|---|---|
| RDS(on) given at VGS = 10 V and 4.5 V | separate specs in the spec list | test conditions are part of each spec's identity, parsed automatically |
| min / typ / max | the spec list says which one | stored for every value, compared like with like, warning if they differ |
| `ms` written instead of mS | depends on the LLM | code fixes it using the expected unit type |
| Typo `225~1.6` in the MLCC table | raw-text check passes it (the text is there); the min ≤ max check marks it unverified | validation rules catch it → review list for correction |
| `0603` inch vs metric | the spec list says which; the LLM writes the standard name | package dictionary with explicit systems |
| Docling moved subscripts (`V =10V, I =500mA GS D`) | the LLM reads it in context | dedicated condition-repair step |
| Pinout only in the drawing | `visual_check` note in the spec list | `requires_visual_check` per type |
| X7R can replace X5R | only if the spec list compares temperature range, not the dielectric name | dielectric turned into numbers (temperature range, ΔC) automatically |

---

## 8. Effort, cost and maintenance

| | Simple | Full |
|---|---|---|
| First working version | ~5–8 weeks | ~15–24 weeks |
| Part-time as a student (~20 h/week) | ~2–4 months | ~8–12 months |
| Fits the paid proof of concept | yes, it basically is the proof of concept | no, too large up front |
| Engineer time at the start | ~10 min per spec list + ~30–50 test cases | 1–2 h type-tree review + schema reviews + test cases |
| Engineer time per month later | new spec lists, confirmations | alias review, schema review, confirmations |
| Adding a new type | write one spec list, re-index that type | usually automatic; review if important |
| Code to understand and hand over | small | large: harder for the company to take over |

---

## 9. When each plan is the better choice

**The simple plan is better when:**
- there is no test set yet and nobody knows how messy the data is (your situation now),
- an engineer confirms every match anyway,
- the first ~10–20 types cover most requests,
- time and budget are limited, or one person builds it.

**The full design is better when:**
- the system has to make decisions on its own, without an engineer checking,
- many specs per part matter, not just a few,
- all ~200 types must work equally well from day one,
- long family datasheets are common and missed rows have proven to be a problem,
- the company wants SQL reports and analysis on the spec data itself, not just matching.

---

## 10. Moving from the simple plan to the full design

The simple plan is built so that moving up doesn't need a rewrite: the spec-list files become the full
design's registry, and the stored specs stay where they are. Each piece is added **only when a measurement
shows it's needed**:

| What you see in testing | Piece to add from the full design |
|---|---|
| Rows missing from long family tables (rows extracted < rows in the table) | table-by-table extraction (Layer 1) |
| The same spec comes out under different names or units across suppliers | alias dictionary + unit conversion in code (Layer 2) |
| LLM-built part numbers that don't exist | part-number generator from the decoder |
| The engineer's answer often not in the top 10 | more specs per type, weighted scoring |
| Engineers often find problems the key specs didn't show | LLM review of the candidates' datasheets |
| Wrong values get past the two simple checks and reach the table | full validation rules + review list |
| Many requests for types without a spec list | automatic type tree and generated spec lists |
| The company wants reports per type | generated per-type SQL views |

The two numbers to watch from the start: **rows extracted vs rows in the table** for family datasheets, and
**"is the engineer's answer in the top 10?"** on the test set. They show which of the first three pieces is
needed, if any.
