# Indexing: classify datasheets by type (Step 0), spec lists per type, extract specs (Step 1)

Plan v1 ([`parts_alternatives_plan.md`](../../parts_alternatives_plan.md), section 2, "Step 0") needs, before
any spec lists are written, **which component types exist and how many datasheets each has**. This step
does that with a local LLM:

```
datasheets/<supplier>/**/*.pdf ──convert──► markdowns/<supplier>/**/*.md
        │  first 1–2 pages
        ▼
1. run     LLM → free-text type label      ──► index_cache/<supplier>/<file_hash>.json  (classification)
2. group   LLM → labels merged into types  ──► index_cache/type_list.yaml   ← you check and edit this once
3. report  type list applied                ──► `type` in every cache file, counts per type, type_report.csv
```

## Command reference

All commands run from the repository root. Every command copies its output to `logs/index_<command>_<time>.log`,
and `-h` / `--help` shows its options.

| Command | What it does | LLM |
|---|---|---|
| `python -m dataPipeline.index.classify run` | Type label for every datasheet not classified yet → `index_cache/<supplier>/<hash>.json` | 1 call per datasheet |
| `python -m dataPipeline.index.classify group` | Merges new labels into types → `index_cache/type_list.yaml` | 1 call per 120 new labels |
| `python -m dataPipeline.index.classify report` | Applies `type_list.yaml`: `type` in every cache file, counts per type, `type_report.csv` | none |
| `python -m dataPipeline.index.spec_lists list` | Types, datasheet counts and spec-list status | none |
| `python -m dataPipeline.index.spec_lists draft …` | Drafts spec lists → `spec_lists/<type>.yaml` | ~10 per type + 1 merge |
| `python -m dataPipeline.index.spec_lists check …` | Validates spec lists after editing | none |
| `python -m dataPipeline.index.extract run …` | Step 1: specs of every datasheet whose type has a spec list → `extraction` in its cache file | 1 call per datasheet |
| `python -m dataPipeline.index.extract show FILE` | Prints what was extracted from one datasheet (spot-check) | none |
| `python -m dataPipeline.index.extract report` | Extraction state and quality per type, `extract_report.csv` | none |
| `python -m dataPipeline.index.load_cache` | Syncs Postgres `catalog_parts` with the index cache | none |

### `classify run`

| Option | Default | Meaning |
|---|---|---|
| `--input DIR` | `markdowns/` | Folder with `<supplier>/**/*.md`; the supplier is the first folder name |
| `--supplier A B …` | all | Only these supplier folders |
| `--workers N` | 1 | Parallel requests. Ollama serves them in parallel only with `OLLAMA_NUM_PARALLEL=N` set on the server |
| `--limit N` | – | Classify at most N datasheets (the speed pilot) |
| `--pages N` | 2 | Pages read per datasheet (by the converter's `<!-- page: N -->` markers) |
| `--max-chars N` | 12000 | Characters sent per datasheet (about 3k tokens) |
| `--force` | off | Classify again, also datasheets already classified |
| `-v`, `--verbose` | off | Print every result |

Already classified datasheets (same file hash, same `CLASSIFIER_VERSION`) are skipped, so a rerun resumes.
Exception: a datasheet whose saved type is one word ("rectifier") and was never asked again is classified
again (see "Types have at least two words" below).
Exit code 1 if any datasheet failed (it is retried by the next run).

### `classify group`

| Option | Default | Meaning |
|---|---|---|
| `--input DIR` | `markdowns/` | As for `run` |
| `--supplier A B …` | all | Only labels of these supplier folders |
| `--chunk N` | 120 | Labels per LLM call |

Only labels not yet in `type_list.yaml` go to the LLM; your edits are kept.

### `classify report`

| Option | Default | Meaning |
|---|---|---|
| `--input DIR` | `markdowns/` | As for `run` |
| `--supplier A B …` | all | Only these supplier folders |

Run it after every edit of `type_list.yaml`. It recounts and rewrites the `datasheets` numbers, so you never
edit those by hand. When `group` or `report` rewrite `type_list.yaml`, they keep only `name`, `datasheets`
and `labels` of each type: your own comments, extra fields and ordering are not kept (types are sorted by count).

### `spec_lists list`

No options. Prints every type of `type_list.yaml` (except `_…` keys) with its datasheet count and its spec list:
`-`, or `draft v1, 41 specs`, `reviewed v3, …`, and `(+ new draft)` when a `<type>.draft.yaml` exists.

### `spec_lists draft [TYPE …]`

| Option | Default | Meaning |
|---|---|---|
| `TYPE …` | – | Type keys from `type_list.yaml` to draft |
| `--top N` | – | Also the N types with the most datasheets that have no spec list yet |
| `--all` | off | Every type that has no spec list yet. Rerun the same command to resume after Ctrl+C or a crash |
| `--min-datasheets N` | 3 | With `--top` / `--all`: skip types with fewer datasheets (too few samples) |
| `--samples N` | 10 | Sample datasheets per type (spread over suppliers, families and single parts) |
| `--min-share X` | 0.5 | Keep specs found in at least this share of the samples ("in common"); lower it for more specs. The rest go to `rare_specs` |
| `--max-chars N` | 24000 | Characters of spec pages sent per sample (page 1 + pages with tables, without tape/reel, soldering, curves) |
| `--workers N` | 1 | Parallel inventory requests (`OLLAMA_NUM_PARALLEL=N` on the server) |
| `--input DIR` | `markdowns/` | Folder with `<supplier>/**/*.md` |
| `--supplier A B …` | all | Only take samples from these supplier folders |
| `--force` | off | Replace an existing **draft** (and with `--top` / `--all`, redo types that have one). A reviewed list is never replaced: a new draft goes to `<type>.draft.yaml` |
| `--force-inventory` | off | List the samples' specs again instead of using the inventory cached in the index cache |
| `-v`, `--verbose` | off | Print every sample (number of specs listed, seconds, or "cached") |

Exit code 1 if any type got no draft.

### `spec_lists check [TYPE …]`

| Option | Default | Meaning |
|---|---|---|
| `TYPE …` | all | Only `spec_lists/<type>.yaml` of these types. Without types: every `spec_lists/*.yaml` except `*.draft.yaml` |
| `-v`, `--verbose` | off | Print every spec: key, rule, unit, critical, found_in |

Errors (exit code 1): missing `type` / `name`, `version` not a whole number ≥ 1, `status` not draft or
reviewed, empty `specs`, keys not in snake_case (with at most one `@condition`), unknown `rule` or `stat`,
`critical` not true/false, `must_match` keys that are not in `specs`, file name not matching `type`.
Notes (no error): numeric rule without a unit, no critical spec, every spec `info`, type not in `type_list.yaml`.

### `extract run [TYPE …]`

| Option | Default | Meaning |
|---|---|---|
| `TYPE …` | every spec list | Only these types (keys of `spec_lists/<type>.yaml`) |
| `--drafts` | off | Also use spec lists with `status: draft` (for a pilot before review); otherwise only `reviewed` lists |
| `--limit N` | all | Extract at most N datasheets (pilot) |
| `--workers N` | 1 | Parallel requests (also set `OLLAMA_NUM_PARALLEL` on the Ollama server) |
| `--max-chars N` | 48000 | Characters of spec pages sent per datasheet |
| `--force` | off | Extract again, also datasheets already extracted |
| `--input DIR`, `--supplier A B …` | `markdowns/`, all | As for `classify run` |
| `-v`, `--verbose` | off | Print every datasheet: parts, values, unverified, coverage, seconds, dropped keys |

Skips a datasheet whose cache already has an extraction with the same `EXTRACTOR_VERSION`, type and spec-list
`version`, so a rerun resumes, and raising a spec list's `version` re-extracts only that type. Spec lists with
errors (see `spec_lists check`) are skipped. Exit code 1 if any datasheet failed (it is retried by the next run;
the error is kept in its cache file under `extraction_error`).

### `extract show FILE`

| Option | Default | Meaning |
|---|---|---|
| `FILE` | – | Markdown path (relative to `--input` or absolute) or a file-hash prefix of the cache file |
| `--limit N` | 20 | Parts to print; 0 = all |
| `--input DIR` | `markdowns/` | |

Prints every part / family row with each value, its page, the raw text and, for unverified values, why.

### `extract report`

| Option | Default | Meaning |
|---|---|---|
| `--input DIR`, `--supplier A B …` | `markdowns/`, all | |

Per type with a spec list: datasheets, extracted, outdated (older spec-list version), failed, parts, values,
share unverified, coverage (share of the spec list found per part). Per datasheet: `index_cache/extract_report.csv`.

### `load_cache`

| Option | Default | Meaning |
|---|---|---|
| `--cache DIR` | `index_cache/` | The index cache |
| `--supplier A B …` | all | Only these supplier folders (rows of other suppliers are left alone) |
| `--input DIR` | `markdowns/` | Markdown folder: a cache file whose markdown now has another hash (changed, reconverted) is not loaded and its rows are removed |
| `--dry-run` | off | Print what would change, write nothing |
| `--database-url URL` | `DATABASE_URL` from the environment / `.env` | SQLAlchemy URL, same as the converter and `db_gui.py` |

Creates `catalog_parts` if missing. A datasheet whose rows came from the same extraction is skipped; a
re-extracted one gets its rows replaced; rows of datasheets no longer in the cache are removed. Exit code 2 if
the database is not reachable.

### Settings outside the command line

| Setting | Where | Default |
|---|---|---|
| Models and their options (temperature, `num_ctx`) | `config.json`: `datasheetClassifierModel`, `typeGroupingModel`, `specInventoryModel`, `specListModel`, `specExtractionModel` | `qwen3:30b-a3b` |
| Longest answer per call (a looping model stops here) | `num_predict` in each `config.json` model entry | 8192 tokens (`specExtractionModel`: 16384) |
| Time limit per call | `timeout` (seconds) in each `config.json` model entry | 1800 |
| Parallel requests on the server | `OLLAMA_NUM_PARALLEL` environment variable of the Ollama server | 1 |
| Re-run every classification / inventory / extraction after a prompt or code change | `CLASSIFIER_VERSION` in `classify.py`, `INVENTORY_VERSION` in `spec_lists.py`, `EXTRACTOR_VERSION` in `extract.py` | `"1"` |
| Database | `DATABASE_URL` (environment or `.env`) | `postgresql+psycopg2://username:postgres@localhost:5432/postgres` |
| Index cache and spec-list folders | `CACHE_DIR` in `common.py`, `SPEC_LIST_DIR` in `spec_lists.py` | `index_cache/`, `spec_lists/` |

## Running it

Requirements: Ollama running locally with the model from `config.json` (`datasheetClassifierModel`,
`typeGroupingModel`; default `qwen3:30b-a3b`, any capable local model works), and `pip install -r requirements.txt`.

```bash
ollama pull qwen3:30b-a3b

# pilot: 50 datasheets, prints seconds and tokens per datasheet and an estimate for all of them
python -m dataPipeline.index.classify run --limit 50 -v

# all of them (resumes where it stopped; run it again after Ctrl+C or a crash)
python -m dataPipeline.index.classify run --workers 4     # with OLLAMA_NUM_PARALLEL=4 on the server

python -m dataPipeline.index.classify group               # merge labels into types -> type_list.yaml
# open index_cache/type_list.yaml, check it, edit it
python -m dataPipeline.index.classify report              # counts per type -> pick the first ~10 types
```

All options: [Command reference](#command-reference).

## What each step does

### 1. `run`: one short LLM call per datasheet

- Input: the first 2 pages (title, features, first table: enough to tell the type), at most 12,000 characters.
  Inline base64 images are removed.
- The prompt is [`prompts/classify.txt`](prompts/classify.txt). The model answers JSON:
  `type` (e.g. "N-channel MOSFET", no brand, series or rating), `is_datasheet` (false for application notes,
  brochures, certificates…), `family` (several part numbers or a code system), up to 5 `part_numbers`,
  `confidence`.
- The result goes into `index_cache/<supplier>/<file_hash>.json` under `classification`, with the model,
  time, seconds and tokens. This is the same cache file that Step 1 (spec extraction) will add its parts to
  (plan section 9).
- **Skips** a datasheet whose cache file already has a classification of the same `CLASSIFIER_VERSION`.
  A changed markdown has a new file hash, so it is classified again. So the run can be stopped at any
  time and resumed, and the two-weekly run only classifies new or changed datasheets.
- A failed call (model error, invalid JSON) is printed and skipped; the next run tries it again.
- **Types have at least two words**: the kind and what sets it apart, "standard rectifier" or "fast recovery
  rectifier", not "rectifier"; "N-channel MOSFET", not "MOSFET". The prompt says so; if the model still
  answers one word, it is asked once more with a note (2 calls for that datasheet, both counted in the
  tokens). If the second answer is again one word, the first one is kept with `asked_again: true` and the
  datasheet is not asked again on later runs. Datasheets classified before this rule with a one-word type
  are classified again by the next `run`, once.
- At the end it prints the measured speed: seconds per datasheet with the chosen workers, seconds, input
  and output tokens per request, and an estimate for all datasheets at that speed (the plan's pilot, section 8).

### 2. `group`: free-text labels → a type list

- Collects the labels of all classified datasheets. Spelling variants are merged in code first
  ("N-Channel MOSFETs" = "n-channel mosfet").
- Labels already in `type_list.yaml` are kept as they are; **only new labels go to the LLM**, 120 per call,
  together with the existing types, so keys stay consistent across calls and later runs. The prompt is
  [`prompts/group.txt`](prompts/group.txt): merge synonyms, keep apart what engineers would not swap
  (N- vs P-channel MOSFET, Schottky vs TVS…), snake_case keys like `mosfet_n`, `schottky_diode`.
  Keys and names have at least two words (`standard_rectifier`, not `rectifier`).
- Labels the LLM leaves out are asked again once; still missing ones get a type of their own (to merge by hand).
- Documents marked "not a datasheet" go to `_not_a_datasheet`, without an LLM call.

`index_cache/type_list.yaml`, **check it once and edit it freely**:

```yaml
types:
  mosfet_n:
    name: N-channel MOSFET
    datasheets: 412
    labels:
    - N-channel MOSFET
    - N-channel enhancement mode MOSFET
  schottky_rectifier:
    ...
```

Rename keys or names, move a label to another type, merge two types by moving their labels. `datasheets` is
only a count. Rerunning `group` never changes what you edited; it only adds new labels.

### 3. `report`: apply the list, count per type

- Writes each datasheet's `type` (the key from the list) into its cache file, so Step 1 knows which spec
  list to use.
- Prints datasheets, families and suppliers per type, sorted by count, and lists labels that are not in the
  type list yet (`_unclassified`). Per-datasheet results: `index_cache/type_report.csv`.
- `group` and `report` list the types whose key or name is a single word; rename those in
  `type_list.yaml` (e.g. `rectifier` → `standard_rectifier`, name "standard rectifier").
- Use the counts to pick the first ~10 types for spec lists (plan decision 9: the types with the most datasheets).

```
type                             name                          datasheets families suppliers
mosfet_n                         N-channel MOSFET                     412      120        23
mlcc                             Multilayer ceramic capacitor         388      380        14
...
```

## Next: spec lists per type (`spec_lists.py`)

After `report`, draft one spec list per component type: every specification that most datasheets of the type
have in common, with a comparison rule for each (plan v1, "Spec lists").

```bash
python -m dataPipeline.index.spec_lists list                  # types, datasheet counts, spec-list status
python -m dataPipeline.index.spec_lists draft mosfet_n mlcc   # draft these types (or: draft --top 10)
python -m dataPipeline.index.spec_lists draft --all -v         # every type without a spec list (rerun to resume)
# open spec_lists/<type>.yaml, review it, set status: reviewed
python -m dataPipeline.index.spec_lists check                 # validate every spec list (-v: every spec)
```

```
index_cache (type from `report`)
        │  ~10 sample datasheets per type: different suppliers, families and single parts
        ▼
1. inventory  LLM per sample → every spec it gives (name, symbol, unit, min/typ/max, conditions)
              cached as `spec_inventory` in the datasheet's index-cache file
2. merge      LLM → items of all samples grouped into specs (synonyms merged): key, unit, rule, critical
              code → found_in per spec; ≥ half of the samples → `specs`, the rest → `rare_specs`
        ▼
spec_lists/<type>.yaml  (status: draft)  ← you review it, set status: reviewed, run `check`
```

All options: [`spec_lists draft`](#spec_lists-draft-type-).

What to check when reviewing a draft:
- **rule** of each spec: `>=` for ratings, `<=` for on-resistance/leakage/tolerance, `≈` for nominal values
  (capacitance, resistance), `=` for package / channel type / dielectric, `contains` for ranges, `info` for
  marking, MSL, packing.
- **critical**: only the few specs that make a part unusable when worse.
- **must_match**: what must be equal for a drop-in (package, pin count…).
- **keys** with conditions: `rds_on_max@vgs_10v` and `rds_on_max@vgs_4v5` are different specs.
- **rare_specs**: move one up to `specs` if engineers always look at it.
- After review: `status: reviewed`; raise `version` on every later change (Step 1 re-indexes that type).

Models (config.json): `specInventoryModel` (num_ctx 16384) and `specListModel` (num_ctx 32768, the merge
sees all samples at once). Tests: `python tests/test_index_spec_lists.py` (fake LLMs, no Ollama).

## Step 1: extract the specs (`extract.py`, `values.py`, `load_cache.py`)

```bash
python -m dataPipeline.index.extract run mosfet_n --limit 20 --drafts -v   # pilot on one type
python -m dataPipeline.index.extract show goodark/2n7000.md                # spot-check one datasheet
python -m dataPipeline.index.extract run --workers 4                       # every reviewed spec list
python -m dataPipeline.index.extract report                                # quality per type
python -m dataPipeline.index.load_cache                                    # -> Postgres catalog_parts
```

**One LLM call per datasheet.** The model gets the datasheet's spec pages (page 1 and pages with tables;
tape-and-reel, soldering and curve pages dropped, like the spec-list inventory) and the type's spec list
(`key | label | unit | stat | conditions | also printed as`). It answers short lines instead of nested JSON,
because output is the slow part (plan section 8):

```json
{"common": ["operating_temp_range | -55..150 | °C | 1 | -55 to +150 |"],
 "parts": [{"part_number": "2N7000", "family_row": "", "ordering_page": 0,
            "values": ["vds_max | 60 | V | 1 | 60 |", "id_max | 200 | mA | 1 | 200 | TA=25C"]}]}
```

`key | value | unit | page | raw | conditions`; `common` holds the values shared by every part, written once. A row with several part numbers (unidirectional `1.5KE6.8` and bidirectional `1.5KE6.8C`) is written once, the other part numbers under `variants` with only the lines that differ; code expands them into full parts (`variant_of` names the first). Tables are compacted before they are sent (the converter pads every cell to the column width: ~55,000 characters of a TVS table become ~6,500), so long ordering tables fit.
A family with a code system and availability tables (MLCC) gives one entry per availability row,
`part_number: ""` and `family_row: "0603 / X7R / 50 V"`, with ranges (`150..470000`) and option lists
(`1; 2; 5; 10`).

**Code then** (`values.py`) turns every line into the full value format
(`value` or `min`/`max` or `set`, `unit`, `stat`, `conditions`, `page`, `raw`), converts the unit to the spec
list's unit (200 mA → 0.2 A, nF → pF, Ω → ohm, °C → degC), drops keys that are not in the spec list
(counted under `unknown_keys`), and runs the two checks of the plan:

1. **raw text on the cited page**: numbers must be whole numbers on the page ("5" is not found in "-55"),
   short text a whole word, other text anywhere (case, spaces and table bars ignored). If the text is on
   another page, the page is corrected (`page_given` keeps the model's page).
2. **min ≤ max** for ranges (catches the `225~1.6` typo in the MLCC table).

A value that fails keeps `verified: false` and its `issues`; it is still stored and shown with a warning.

**Stored** in the cache file under `extraction`: version, type, spec-list version and status, model, time,
`parts` (`part_number`, `family_row`, `ordering_page`, `specs`) and `stats` (parts, values, unverified,
coverage, unknown keys, seconds, tokens). `load_cache` writes one `catalog_parts` row per part or family
row: supplier, file, file hash, versions, type, part number, family row, ordering page, `specs` (JSONB),
number of unverified values. Indexes on `type` and `upper(part_number)` for Step 2 (shortlist).

Our suppliers' parts only; external parts will get their own cache folder and table (`external_parts`).

Tests: `python -m pytest tests/test_index_extract.py` (fake LLM). The database test runs when
`TEST_DATABASE_URL` points to an empty Postgres database.

## Files

| File | Role |
|---|---|
| `classify.py` | `classify(markdown, llm)` (plan rule 3: a module with a fixed input and output) and the `run` / `group` / `report` commands |
| `common.py` | Finding the markdowns, file hashes, first pages, the index cache, the Ollama JSON call |
| `spec_lists.py` | Spec lists per type: `inventory(markdown, llm)`, `merge_specs(...)`, `validate(spec_list)` and the `list` / `draft` / `check` commands |
| `prompts/spec_inventory.txt`, `prompts/spec_merge.txt` | The two spec-list prompts; editing `spec_inventory.txt` in a way that needs a re-run → bump `INVENTORY_VERSION` |
| `extract.py` | Step 1: `extract(markdown, spec_list, llm)` and the `run` / `show` / `report` commands |
| `values.py` | The full value format: `parse_line`, `parse_value`, unit conversion, the raw-text and min ≤ max checks |
| `load_cache.py` | `catalog_parts` table and the sync from the index cache (no LLM) |
| `prompts/extract.txt` | The extraction prompt; a change that needs a re-run → bump `EXTRACTOR_VERSION` |
| `prompts/classify.txt`, `prompts/group.txt` | The two prompts; editing `classify.txt` in a way that needs a re-run → bump `CLASSIFIER_VERSION` |

## Notes

- `index_cache/` is kept in git (plan section 9): small JSON files, so a new installation loads them
  without an LLM run, and a two-weekly run only changes the files of datasheets that changed. Commit it after a
  run: `git add index_cache && git commit -m "Index cache: ..."`.
- Thinking models (Qwen3…) are called with thinking switched off: classification needs a short answer, not
  reasoning, and the output length decides the speed.
- Tests: `python tests/test_index_classify.py` (fake LLM, no Ollama).
