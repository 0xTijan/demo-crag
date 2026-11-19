# Index cache

Results of the indexing steps (plan v1, `parts_alternatives_plan.md`, section 9), written by
`python -m dataPipeline.index.classify ...` (see `dataPipeline/index/README.md`).

| File | Content |
|---|---|
| `<supplier>/<file_hash>.json` | One file per markdown datasheet: `classification` (Step 0: type label, family, part numbers), `type` (key from the type list), later the extracted parts with their specs (Step 1) |
| `type_list.yaml` | Type list: type keys with their names and the labels that belong to them. Checked and edited by hand |
| `type_report.csv` | Per datasheet: supplier, file, type, label, family, confidence (rewritten by `classify report`) |

Kept in git so a new installation does not need to run the LLM again, and a two-weekly run only changes the
files of datasheets that changed. Commit after each run.
