# Indexing step 0: classify every datasheet by component type

Plan v1 ([`parts_alternatives_plan.md`](../../parts_alternatives_plan.md), section 2, "Step 0") needs, before
any spec lists are written, **which component types exist and how many datasheets each has**. This step
does that with a local LLM:

```
markdowns/suppliers/<supplier>/**/*.md
        │  first 1–2 pages
        ▼
1. run     LLM → free-text type label      ──► index_cache/<supplier>/<file_hash>.json  (classification)
2. group   LLM → labels merged into types  ──► index_cache/type_list.yaml   ← you check and edit this once
3. report  type list applied                ──► `type` in every cache file, counts per type, type_report.csv
```

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

| Option | Command | Default | Meaning |
|---|---|---|---|
| `--input DIR` | all | `markdowns/suppliers` | Folder with `<supplier>/**/*.md`; the supplier is the first folder name |
| `--supplier A B` | all | all | Only these supplier folders |
| `--workers N` | run | 1 | Parallel requests. Ollama serves them in parallel only with `OLLAMA_NUM_PARALLEL=N` set on the server |
| `--limit N` | run | — | Classify at most N datasheets (the speed pilot) |
| `--pages N` | run | 2 | Pages read per datasheet (by the converter's `<!-- page: N -->` markers) |
| `--max-chars N` | run | 12000 | Characters sent per datasheet (about 3k tokens) |
| `--force` | run | off | Classify again, also files already classified |
| `-v` | run | off | Print every result |
| `--chunk N` | group | 120 | Labels per grouping call |

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
- At the end it prints the measured speed: seconds per datasheet with the chosen workers, seconds, input
  and output tokens per request, and an estimate for all datasheets at that speed (the plan's pilot, section 8).

### 2. `group`: free-text labels → a type list

- Collects the labels of all classified datasheets. Spelling variants are merged in code first
  ("N-Channel MOSFETs" = "n-channel mosfet").
- Labels already in `type_list.yaml` are kept as they are; **only new labels go to the LLM**, 120 per call,
  together with the existing types, so keys stay consistent across calls and later runs. The prompt is
  [`prompts/group.txt`](prompts/group.txt): merge synonyms, keep apart what engineers would not swap
  (N- vs P-channel MOSFET, Schottky vs TVS…), snake_case keys like `mosfet_n`, `mlcc`.
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

| Option (`draft`) | Default | Meaning |
|---|---|---|
| `types` / `--top N` | – | Type keys from `type_list.yaml`, and/or the N types with the most datasheets |
| `--samples N` | 10 | Sample datasheets per type |
| `--min-share X` | 0.5 | Keep specs found in at least this share of the samples ("in common"); lower it for more specs |
| `--max-chars N` | 24000 | Characters of spec pages sent per sample (page 1 + pages with tables, without tape/reel, soldering, curves) |
| `--workers N` | 1 | Parallel inventory requests (`OLLAMA_NUM_PARALLEL` on the server) |
| `--force` | off | Replace an existing **draft**. A reviewed list is never replaced: a new draft goes to `<type>.draft.yaml` |
| `--force-inventory` | off | List the samples' specs again instead of using the cached inventory |

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

## Files

| File | Role |
|---|---|
| `classify.py` | `classify(markdown, llm)` (plan rule 3: a module with a fixed input and output) and the `run` / `group` / `report` commands |
| `common.py` | Finding the markdowns, file hashes, first pages, the index cache, the Ollama JSON call |
| `spec_lists.py` | Spec lists per type: `inventory(markdown, llm)`, `merge_specs(...)`, `validate(spec_list)` and the `list` / `draft` / `check` commands |
| `prompts/spec_inventory.txt`, `prompts/spec_merge.txt` | The two spec-list prompts; editing `spec_inventory.txt` in a way that needs a re-run → bump `INVENTORY_VERSION` |
| `prompts/classify.txt`, `prompts/group.txt` | The two prompts; editing `classify.txt` in a way that needs a re-run → bump `CLASSIFIER_VERSION` |

## Notes

- `index_cache/` is kept in git (plan section 9): small JSON files, so a new installation loads them
  without an LLM run, and a two-weekly run only changes the files of datasheets that changed. Commit it after a
  run: `git add index_cache && git commit -m "Index cache: ..."`.
- Thinking models (Qwen3…) are called with thinking switched off: classification needs a short answer, not
  reasoning, and the output length decides the speed.
- Tests: `python tests/test_index_classify.py` (fake LLM, no Ollama).
