# PDF → Markdown conversion pipeline

`python dataPipeline/convert/docling.py` turns every new or changed PDF in `reports/` into a Markdown file in `markdowns/`, with exact tables, and records everything in PostgreSQL.

This document explains the pipeline from the first line to the last, so that an engineer who has never seen it can understand what each part does and how the parts connect.

- [1. What it does, in one picture](#1-what-it-does-in-one-picture)
- [2. Files and who calls whom](#2-files-and-who-calls-whom)
- [3. Running it](#3-running-it)
- [4. Database](#4-database)
- [5. `common.py`: bookkeeping around every conversion](#5-commonpy-bookkeeping-around-every-conversion)
- [6. `docling.py`: the conversion itself](#6-doclingpy-the-conversion-itself)
- [7. `ruled_tables.py`: exact tables from the PDF's own lines](#7-ruled_tablespy-exact-tables-from-the-pdfs-own-lines)
- [8. `vision_ocr.py`: fallback for pages with no text](#8-vision_ocrpy-fallback-for-pages-with-no-text)
- [9. Worked example](#9-worked-example)
- [10. Failure handling](#10-failure-handling)
- [11. Every threshold in one place](#11-every-threshold-in-one-place)
- [12. Tests](#12-tests)
- [13. Extending and known limits](#13-extending-and-known-limits)

---

## 1. What it does, in one picture

```
reports/*.pdf
     │
     ▼
┌─────────────────────────── common.py ─────────────────────────────┐
│  for each PDF:                                                      │
│    SHA-256 hash ──► already converted (same path + hash)? ──► skip  │
│          │ no                                                       │
│          ▼                                                          │
│   ┌──────────────── docling.py : convert(pdf) ─────────────────┐    │
│   │ 1. Docling reads the PDF visually (layout, OCR, tables)    │    │
│   │ 2. ruled_tables.py rebuilds tables drawn with lines        │    │
│   │      from the PDF's own vector graphics (pdfplumber)       │    │
│   │      and swaps them into Docling's document                │    │
│   │ 3. export Markdown page by page                            │    │
│   │ 4. vision_ocr.py re-reads pages with almost no text        │    │
│   │      using a local vision model (Ollama)                   │    │
│   └────────────────────────────────────────────────────────────┘    │
│          │ Conversion(markdown, pages, tables, ...)                 │
│          ▼                                                          │
│   write markdowns/<same path>.md  ──►  upsert row in report_files   │
│   any failure ──► row status 'failed' + row in report_file_errors   │
└─────────────────────────────────────────────────────────────────────┘
```

**Why two table readers?** Docling looks at a rendered page image and uses trained models. That works on almost any layout but can crop a table (losing columns) or misread merged cells. The PDF itself usually contains the exact answer: the line segments that draw the table and the text with its coordinates. `ruled_tables.py` reads those directly. It is exact when lines enclose the text, and it steps aside (leaving Docling's table) when they do not.

---

## 2. Files and who calls whom

| File | Role | Calls |
|---|---|---|
| `docling.py` | The script you run. Defines `convert(pdf)`: Docling → table fix-up → Markdown → fallback. | `common`, `ruled_tables`, `vision_ocr` |
| `common.py` | Everything around a conversion: database, hashing, skip logic, writing the file, error logging, the command-line loop. Knows nothing about Docling. | none of the others |
| `ruled_tables.py` | Reads ruled tables from the PDF with pdfplumber; merges them into a Docling document. | `pdfplumber`, `docling_core` (lazily) |
| `vision_ocr.py` | Detects near-empty pages and transcribes them with an Ollama vision model. | `common.log`, `helpers.config`, `pypdfium2` and `ollama` (lazily) |

```
docling.py ──► common.run_pipeline(...)  ──► process_pdf ──► docling.convert  (callback)
     │                                                           │
     │                          ┌────────────────────────────────┤
     │                          ▼                                ▼
     └── imports ──► ruled_tables.extract_tables /        vision_ocr.apply_fallback /
                     apply_to_docling_document            join_pages
```

The key design point: **`common.py` calls `docling.convert` as a callback.** `run_pipeline(converter, version, output_dir, convert, description)` receives the function, so a different converter could reuse all the bookkeeping (see [13](#13-extending-and-known-limits)).

---

## 3. Running it

```bash
python dataPipeline/convert/docling.py            # convert new/changed PDFs
python dataPipeline/convert/docling.py --force    # reconvert everything
```

Requirements:

- `reports/` exists at the repo root (gitignored; you put PDFs there, subfolders allowed).
- PostgreSQL reachable through `DATABASE_URL` (environment variable or `.env`). Default: `postgresql+psycopg2://username:postgres@localhost:5432/postgres`.
- Python packages: `docling`, `pdfplumber`, `sqlalchemy`, `psycopg2`, `python-dotenv`; `ollama` and `pypdfium2` only if the vision fallback is needed.
- Ollama running with `qwen2.5vl:7b` pulled, only if some page triggers the fallback. The model is configured in `config.json`:

```json
"ocrFallbackModel": { "model": "qwen2.5vl:7b", "temperature": 0 }
```

Output:

- `markdowns/<same path as the PDF>.md`. `markdowns/` is gitignored. `reports/a/b.pdf` becomes `markdowns/a/b.md`.
- Rows in `report_files` and `report_file_errors`.
- Exit code `0` if nothing failed, `1` if any PDF failed.

A typical log, with what each line means:

```
[docling] connecting to db                      # run_pipeline: opening the DB connection
database ready (tables checked)                 # DDL executed
Found 3 PDF(s) in .../reports                   # rglob("*.pdf")
[1/3] 2N7000.pdf                                # process_pdf starts
  hashed: 947f8407... (0.6 MB)                  # stage "hash"
  will convert: new file                        # skip check said no; this is the reason
  loading Docling models (first conversion ...) # build_converter(), once per run
  converting with Docling (OCR + table ...)
  Docling done in 4.5s: 4 pages, 5 tables
  ruled tables: 5 rebuilt from PDF lines in 0.3s (0 left from Docling)
  RapidOCR empty-result messages: 0             # a hint only, see section 8
  OCR check: 0 sparse page(s) under 100 chars   # vision_ocr.apply_fallback
  writing markdowns/2N7000.md ...               # stage "write"
  saved to database. -> markdowns/2N7000.md (pages: 4, tables: 5) in 4.9s   # stage "db"
Done: 3 converted, 0 skipped, 0 failed
```

`(N left from Docling)` is `len(doc.tables) - applied`, an approximation of how many tables still come from Docling.

---

## 4. Database

Both tables are created (and old databases upgraded) every time `run_pipeline` starts, by executing the idempotent `DDL` list in `common.py`.

### `report_files`: one row per (PDF path, PDF content, converter)

| Column | Meaning |
|---|---|
| `pdf_path` | Path relative to `reports/`, always with `/` separators. |
| `file_hash` | SHA-256 of the PDF's bytes (64 hex chars). |
| `converter` | `'docling'` (leaves room for other pipelines). |
| `status` | `'converted'` or `'failed'`. |
| `markdown_path` | Output path relative to the repo root; `NULL` when failed. |
| `pages`, `tables` | Counts from the final document. |
| `converter_version` | Installed Docling version, so you can find files to redo after an upgrade. |
| `ocr_fallback_pages`, `ocr_fallback_model` | How many pages the vision model replaced and with which model. |
| `ocr_empty_events` | Number of RapidOCR "empty result" log messages (a hint). |
| `error` | `"<stage>: <message>"` when `status = 'failed'`. |
| `converted_at` | Time of the last upsert. |

A **unique index on `(pdf_path, file_hash, converter)`** is what makes it work: the same name with new content gets a new row (the old one stays as history), and identical content updates its row in place.

### `report_file_errors`: append-only failure log

`pdf_path`, `file_hash` (NULL if the file could not even be hashed), `stage`, `error`, `traceback`, `converter`, `occurred_at`. Nothing ever updates or deletes these rows, so a retry that overwrites `report_files` cannot erase earlier errors.

Stages you will see: `hash`, `convert`, `write`, `db` (fatal for the file), and `ruled_tables`, `ocr_fallback` (non-fatal warnings, the file still converts).

Useful queries:

```sql
-- latest state of every file
SELECT DISTINCT ON (pdf_path) pdf_path, status, converted_at, error
FROM report_files WHERE converter = 'docling'
ORDER BY pdf_path, converted_at DESC;

-- which conversions used the vision model?
SELECT pdf_path, ocr_fallback_pages, ocr_fallback_model FROM report_files WHERE ocr_fallback_pages > 0;

-- recent errors with their stage
SELECT occurred_at, pdf_path, stage, error FROM report_file_errors ORDER BY occurred_at DESC LIMIT 20;
```

---

## 5. `common.py`: bookkeeping around every conversion

### 5.1 Setup

```python
load_dotenv()                                   # read .env (DATABASE_URL ...) if present
ROOT = Path(__file__).resolve().parents[2]      # repo root: dataPipeline/convert/common.py -> up two levels
INPUT_DIR = ROOT / "reports"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://username:postgres@localhost:5432/postgres")
```

`DDL` is the list of SQL statements from section 4. The `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` lines upgrade databases created by earlier versions of the script, because `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched. The line that drops `report_files_pdf_path_file_hash_key` removes the old two-column unique key before the three-column index is created.

`UPSERT` is one statement for insert and update:

```sql
INSERT INTO report_files (...) VALUES (...)
ON CONFLICT (pdf_path, file_hash, converter) DO UPDATE SET status = EXCLUDED.status, ..., converted_at = now()
```

All values are named bind parameters (`:pdf_path`, ...), never formatted into the SQL string, so file names cannot inject SQL.

### 5.2 Data types

```python
@dataclass
class Conversion:                 # what a pipeline's convert() returns for one PDF
    markdown: str
    pages: int | None = None
    tables: int | None = None
    ocr_fallback_pages: int = 0
    ocr_fallback_model: str | None = None
    ocr_empty_events: int | None = None
    warnings: list[tuple[str, BaseException]] = ...   # non-fatal problems: (stage, exception)

class StageError(Exception):      # carries WHICH stage failed and the REAL exception
    def __init__(self, stage, original): ...
```

`StageError` exists so `run_pipeline` can store the right stage name and the original traceback no matter where inside `process_pdf` the failure happened.

### 5.3 Helpers

```python
def sha256_file(path):                       # 1 MiB chunks: constant memory for huge reports
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def write_atomic(path, content):             # never leave a half-written .md
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)                        # atomic rename on the same filesystem
```

The hash depends only on the file's bytes: renaming or touching a file does not change it, editing it does. Because the path is part of the identity too, the same content under a new name is converted again.

### 5.4 `Tracker`: all SQL for one converter

| Method | What it does |
|---|---|
| `already_converted(path, hash)` | `True` only if a row with `status='converted'` exists **and** `ROOT / markdown_path` still exists. Deleting an output file therefore forces regeneration. A `failed` row never counts, so failures retry automatically. |
| `conversion_reason(rel, hash, force)` | Log text only: `--force`, `retrying after an earlier failure`, `markdown file is missing`, `content changed (previous hash …)`, or `new file`. Never changes behaviour. |
| `upsert(params)` | Runs `UPSERT` in one transaction (`engine.begin()` commits on exit, rolls back on error). |
| `record_failure(params, stage, exc)` | Upserts a `failed` row (`markdown_path`, `pages`, `tables` set to `NULL`, `error = "<stage>: <message>"`). Never raises: if the DB is down it prints to stderr rather than hiding the original error. |
| `log_error(path, hash, stage, exc)` | Appends to `report_file_errors` with the full formatted traceback. Never raises, for the same reason. |

### 5.5 `process_pdf`: the per-file state machine

```python
rel     = pdf.relative_to(INPUT_DIR).as_posix()              # e.g. "a/b.pdf": the file's identity
md_path = output_dir / Path(rel).with_suffix(".md")          # markdowns/a/b.md: output mirrors the input layout
```

Then five stages. Each is wrapped so a failure becomes a `StageError` with the stage name:

| # | Stage | Code | On failure |
|---|---|---|---|
| 1 | `hash` | `file_hash = sha256_file(pdf)`; `file_size = pdf.stat().st_size` | Only an error row (no hash, so no `report_files` row can be keyed). |
| 2 | `db` (skip check) | `if not force and tracker.already_converted(rel, file_hash): return "skipped"` | Error row. |
| 3 | `convert` | `result = convert(pdf)` (the callback; for Docling see section 6) | `failed` row + error row. |
| 4 | `write` | `write_atomic(md_path, result.markdown)` | `failed` row + error row. |
| 5 | `db` (success) | `tracker.upsert({... "status": "converted", ...})` | Error row. |

Between 3 and 4, every non-fatal warning from the converter is stored:

```python
for stage, exc in result.warnings:
    tracker.log_error(rel, file_hash, stage, exc)
```

**Ordering matters.** The Markdown file is written *before* the success row. If the database write fails afterwards, the file exists without a row and is simply converted again next run. The reverse order could record a success for a file that does not exist.

### 5.6 `run_pipeline`: the command-line loop

1. Parses `--force`.
2. `sys.exit` if `reports/` is missing.
3. Opens the engine and executes every `DDL` statement in one transaction. On failure it exits and prints only `DATABASE_URL.split('@')[-1]` (host and database), so the password is never shown.
4. `pdfs = sorted(INPUT_DIR.rglob("*.pdf"))`: recursive, deterministic order.
5. For each PDF, calls `process_pdf`. On `StageError` it counts a failure, logs it, re-hashes the file for the error row (skipped if hashing was what failed) and **continues with the next PDF**. One bad PDF never stops the batch.
6. Prints `Done: N converted, N skipped, N failed`; `sys.exit(1)` if any failed.

---

## 6. `docling.py`: the conversion itself

### 6.1 Why the first lines look odd

```python
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != SCRIPT_DIR]   # drop this folder
sys.path.insert(0, str(SCRIPT_DIR.parents[1]))                                # add the repo root
```

This file is named `docling.py`. When you run it as a script, Python puts the script's own folder first on `sys.path`, so `import docling` would load *this file* instead of the installed library. The first line removes that folder (`p or "."` handles the empty "current directory" entry). The second line makes the repo root importable, so `dataPipeline.convert.*` and `helpers.*` resolve. The library imports after that carry `# noqa: E402` because they intentionally come after executable code.

### 6.2 `build_converter()`

```python
opts = PdfPipelineOptions()
opts.do_ocr = True                                            # OCR scanned pages / image-only regions (RapidOCR)
opts.do_table_structure = True                                # run the TableFormer table model
opts.table_structure_options.mode = TableFormerMode.ACCURATE  # slower, better on complex / merged cells
opts.table_structure_options.do_cell_matching = True          # copy cell text from the PDF's own text layer
return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
```

Building the converter loads the ML models (slow), so `convert()` creates it lazily:

```python
_converter = None            # module-level cache
...
if _converter is None:
    _converter = build_converter()
```

A run where every PDF is skipped therefore never loads a model.

### 6.3 `convert(pdf)`, step by step

**Step 1: Docling parses the PDF.**

```python
counter = OcrEmptyCounter()
logging.getLogger().addHandler(counter)       # listen to all log records while Docling runs
try:
    doc = _converter.convert(pdf).document    # layout model + OCR + table model
finally:
    logging.getLogger().removeHandler(counter)
```

`doc` is a `DoclingDocument`: pages (`doc.pages`, a dict keyed by 1-based page number), text items, tables (`doc.tables`) and their bounding boxes. `OcrEmptyCounter` counts log records containing `"returned empty result"` (RapidOCR's message for a region with no text). It is only a **hint** stored in `ocr_empty_events`. It also fires on harmless blank areas, so it never triggers the vision fallback.

**Step 2: replace Docling's tables with exact ones where possible.**

```python
found   = ruled_tables.extract_tables(pdf)                      # read lines + text with pdfplumber
applied = ruled_tables.apply_to_docling_document(doc, found)    # swap them into `doc`
```

This is wrapped in `try/except`: if it fails, Docling's own tables are kept and `("ruled_tables", exc)` is added to `warnings`. Details in section 7.

**Step 3: clean the cells.**

```python
BULLET_PREFIX = re.compile(r"^[－•·▪●◦]\s*")
for table in doc.tables:
    for cell in table.data.table_cells:
        cell.text = BULLET_PREFIX.sub("", cell.text)
```

Docling leaves list markers at the start of table cells (`－DRGR664`). This strips them.

**Step 4: export Markdown one page at a time.**

```python
pages = {n: doc.export_to_markdown(page_no=n, escape_html=False) for n in sorted(doc.pages)}
```

`escape_html=False` keeps `&` and `<` as plain characters instead of `&amp;`/`&lt;`. Exporting per page gives a `{page_no: markdown}` dict, which both the fallback and the page markers need.

**Step 5: vision fallback.** `vision_ocr.apply_fallback(pdf, pages)` returns the (possibly updated) pages. See section 8.

**Step 6: assemble the result.**

```python
return Conversion(
    markdown=vision_ocr.join_pages(pages),     # "<!-- page: 1 -->\n\n...\n\n<!-- page: 2 -->..."
    pages=len(doc.pages), tables=len(doc.tables),
    ocr_fallback_pages=..., ocr_fallback_model=..., ocr_empty_events=counter.count,
    warnings=warnings + fb_warnings)
```

The last lines of the file hand control back to `common.py`:

```python
run_pipeline(CONVERTER, pkg_version("docling"), OUTPUT_DIR, convert, "Convert new/changed PDFs ...")
```

---

## 7. `ruled_tables.py`: exact tables from the PDF's own lines

Digital PDFs are not images. They contain drawing instructions (lines, rectangles, fills) and text with exact coordinates. pdfplumber exposes both. This module rebuilds tables from them geometrically.

All coordinates are in PDF points (1/72 inch), **origin top-left** inside this module (pdfplumber's convention). Docling uses bottom-left origin, which `apply_to_docling_document` converts.

### 7.1 Output type

```python
@dataclass
class ExtractedTable:
    page_no: int            # 1-based
    bbox: tuple             # (x0, top, x1, bottom) of the table on the page
    rows: list[list[str]]   # fully filled grid; rows[0] is the header
    source: str             # "grid" | "boxes" | "rules": which detector produced it
```

### 7.2 `extract_tables(pdf_path)`: the per-page driver

For every page:

```python
page = _visible_graphics_page(raw_page.dedupe_chars())     # (a) clean the page
if not page.chars: continue                                # (b) no text layer = scanned: leave to OCR
finder = page.debug_tablefinder(FINDER_SETTINGS)           # (c) pdfplumber finds cell rectangles
for cells in _group_cells(finder.cells):                   # (d) group touching cells = candidate tables
    ...
```

Then for each group of cells:

1. Take every character whose centre is inside the group's bounding box. If there are none, it is a drawing: skip.
2. **Coverage check.** Count characters whose centre is inside *some* cell. If less than 98% (`MIN_TEXT_IN_CELLS`), the lines do not enclose the text, so they are decoration and the group is skipped (Docling keeps that table). This is the guard that fixed a press release whose "table" was only a dotted highlight frame plus section rules.
3. `_build_grid(cells, chars)` makes the grid.
4. If the grid has one column it is a framed box: it is remembered for `_box_tables` and not emitted as a table.
5. Otherwise `_flatten_nested` folds nested tables, `_fill` expands merged cells, and a table with fewer than 2 non-empty cells is dropped.
6. After all groups: `_box_tables(boxes)` combines rows of framed boxes, and `_rule_tables(page, taken, page_no)` looks for key/value forms drawn with horizontal rules only (skipping areas already taken by a grid).

`FINDER_SETTINGS` are tuned deliberately:

```python
"vertical_strategy": "lines", "horizontal_strategy": "lines",   # use only drawn lines, never guess from text
"snap_tolerance": 1,        # page-wide: lines within 1pt get the same coordinate (merges double lines only)
"join_tolerance": 3,        # local: bridge hairline gaps between segments of one line
"intersection_tolerance": 3,
"edge_min_length": 3,
```

`snap_tolerance` is kept at 1 because snapping is **page-wide**: a larger value moves unrelated lines far apart on the page and destroys cells.

### 7.3 Graphics filtering: `_visible_graphics_page`

PDFs often contain invisible shapes. White frames behind drawings, for example, would otherwise become fake cells over a real table. The function removes:

- shapes that are neither stroked nor filled with a visible colour (`_is_white` understands gray, RGB and CMYK white);
- filled rectangles lying entirely inside a bigger filled rectangle of the same colour. These are typically one rectangle per text line inside a coloured header band, whose edges are seamless and not real borders. Only *area* fills count here (more than 2pt in both directions); hairline rectangles used to draw table lines are kept.

### 7.4 From cells to a grid: `_group_cells`, `_merge_boundaries`, `_build_grid`

**`_group_cells`:** two cells belong together if they share a corner (coordinates rounded to whole points). It also merges groups bridged by a later cell. Unlike pdfplumber's own grouping, it keeps single-cell boxes, which `_box_tables` needs.

**`_merge_boundaries(values, centers)`:** collapses grid lines that do not separate any text. The motivating case is a header drawn with inset white gutters:

```
body column edges:    28.3 .............. 90.1
header cell edges:         33.7 ... 84.7
```

Naively that is four vertical boundaries, hence three columns, two of them empty slivers. The function snaps boundaries within 1pt, then looks at each pair of neighbours. If **no character centre lies between them** (`a + 0.5 < c < b - 0.5`), they belong to the same run and are replaced by their mean. The outermost boundaries are kept exactly, so the table's outer frame does not move.

**`_build_grid(cells, chars)`:**

1. Compute merged x boundaries `xs` and y boundaries `ys` (same function, with the characters' x or y centres).
2. Create `grid[row][col]`, initially `None`. Each cell gets an integer id (its index in `cells`).
3. For every cell, find its first/last column and row with `_nearest` (closest merged boundary), collect the text of the characters inside it with `_cell_text`, and write the cell's id into **every grid position it covers**. If two cells overlap, the **smaller one wins** ("most specific cell wins"), because a small cell drawn on top of a big background rectangle is the real cell.
4. A cell whose first and last column (or row) are equal vanished in the merge and is skipped.
5. Rows and columns with no text at all are removed.

The result is `(grid, texts)`: a grid of ids plus `{id: text}`. Using ids is what makes merged cells trivial: a cell spanning three rows simply has the same id in three rows.

`_fill(grid, texts)` finally replaces each id with its text, so **a merged cell's text is repeated in every row and column it covers**:

```
grid (ids)                  filled
[[0, 1, 2],                 [["Item",   "Spec",      "Method"],
 [3, 4, 5],                  ["Shock",  "No damage", "10 cycles"],
 [6, 4, 7]]                  ["Cycle",  "No damage", "One cycle"]]     # id 4 spans rows 1-2
```

### 7.5 Nested tables: `_flatten_nested(grid, texts)`

Some cells contain a small table. The DRGR datasheet's "Test Method" cell holds a `Step / Temperature / Time` table. In the grid this appears as a header cell spanning several columns, with rows underneath where that span is split into separate cells.

```
Item | Specification | Test Method (spans 3 columns)
                       Step | Temperature | Time        <- the span is split here
                       1    | -25±3       | 30
```

Algorithm:

1. Find *column groups*: header cells (row 0) whose id repeats across consecutive columns.
2. For each group (right to left, so column indices stay valid), find the rows where the group is split into more than one distinct id. No such rows means it is an ordinary column span: leave it.
3. Group consecutive split rows into blocks. For each block, find the **outer row band**: the smallest cell outside the group that spans the whole block. If the other columns change inside that band, it is not a nested table: skip.
4. Collapse the band into one row. The first split row is the nested header. Each later split row becomes `"Header1: v1, Header2: v2, ..."`. Unsplit rows inside the band contribute their text as plain lines. Lines are joined with `<br>`.
5. If the group is no longer split anywhere, collapse its columns into one.

Result for the example: `One cycle:<br>Step: 1, Temperature (℃): -25±3, Time (min): 30<br>Step: 2, ...` in a single cell.

### 7.6 Rows of framed boxes: `_box_tables(boxes, page_no)`

The "Product Identification" diagram is not a table in the PDF: it is five blue boxes (`DRGR`, `664`, `M`, `B`, `100`), each with stacked boxes below it (`Product Type`, `Dimensions…`, and so on).

Each single-column group from step 4 of `extract_tables` arrives as `(bbox, [(top, text), ...])`.

1. **Stacks:** boxes that overlap horizontally by at least half of the narrower width and are at most 20pt below the previous box belong to one vertical stack.
2. **Rows of stacks:** stacks whose tops are within 4pt and whose bottoms are within 45pt are side by side. At least **3** such stacks form a table (fewer is ordinary text).
3. Rows are built by aligning cells on their top coordinate (grouping tops within 4pt into bands). Each stack becomes a column.

### 7.7 Horizontal-rule forms: `_rule_tables`, `_rules_to_table`

The Customer/Product/Sizes block on the DRGR cover is separated by horizontal lines only.

1. Collect horizontal edges, group those at the same height (within 1.5pt), and merge their segments into continuous **rules**, remembering where segments meet (`breaks`, the column splits). Rules narrower than 40pt are ignored. Rules lying inside a grid table already found are ignored.
2. **Chain** rules into a table: same left/right extent (±6pt), vertical gap between 8 and 80pt, and a regular spacing (within 40% of the previous gap). A chain of **at least 3 rules** is a candidate.
3. `_rules_to_table` takes column boundaries from break points present in at least 60% of the rules, builds one band per gap between rules (plus one band above the first rule for a header), and collects the text in each band and column.

It is rejected, leaving the area to Docling, if:

- any band has **more than one line of text**: a key/value form has one line per row, so several lines mean the rules are section separators of some other layout;
- the line of text just above the table has words in **two or more columns**: the table continues upward without rules and only its last rows are ruled, so this would be a fragment;
- it has fewer than 2 rows or 2 columns.

### 7.8 `rows_to_markdown(rows)`

Pads rows to equal width, escapes `|` as `\|` and newlines as spaces, writes the first row as the header and `|---|---|` as the separator. Merged cells need no special syntax because `_fill` already repeated their text.

### 7.9 `apply_to_docling_document(doc, tables)`: the swap

Runs once per extracted table, and returns how many were applied.

```python
region = (x0 - 2, top - 2, x1 + 2, bottom + 2)        # the table's area, padded 2pt
tables_hit = [Docling tables whose box overlaps region by more than 30%]
texts_hit  = [Docling text items whose CENTRE lies inside region]   # loose cell text, headings made from cells
```

Then:

- **Docling found a table there:** its `data` and `prov` are replaced by the new table (`TableCell`s built from `rows`; row 0 is flagged `column_header`). Any *other* overlapping Docling tables and all `texts_hit` are deleted with `doc.delete_items(...)`, because they would duplicate the table.
- **Docling found no table:** delete `texts_hit`, then insert a new table **before the first remaining item below the table's top** (`doc.insert_table(sibling=..., after=False)`), or after the last item, or as the only item.

Coordinates are converted with `BoundingBox(..., TOPLEFT).to_bottom_left_origin(page_height)` when writing and `to_top_left_origin(height)` when reading.

**There is deliberately no "which version is better" comparison.** If lines enclose the text (the 98% check) and the grid is valid, the ruled table wins. The guards in 7.2 and 7.7 are what protect Docling's correct tables.

---

## 8. `vision_ocr.py`: fallback for pages with no text

Some pages come out of Docling nearly empty: a scanned page whose OCR failed, or an image-only page. These are re-read by a local vision model through Ollama.

```python
MIN_PAGE_CHARS = int(os.getenv("OCR_FALLBACK_MIN_PAGE_CHARS", "100"))
```

### 8.1 Deciding which pages

```python
def text_length(markdown):
    return len(re.sub(r"[\s|#*_>\-]", "", markdown))     # drop whitespace and Markdown symbols
```

A page whose Markdown has fewer than `MIN_PAGE_CHARS` real characters is *sparse*. Symbols are dropped so an empty table skeleton (`| --- | --- |`) or a lone heading does not count as content. Note that the check is on the text length, **not** on the RapidOCR log hint.

### 8.2 `apply_fallback(pdf, page_markdown)`

```python
sparse = [n for n, md in sorted(page_markdown.items()) if text_length(md) < MIN_PAGE_CHARS]
log(f"  OCR check: {len(sparse)} sparse page(s) under {MIN_PAGE_CHARS} chars ...")     # ALWAYS printed
if not sparse:
    return page_markdown, 0, None, []                  # nothing to do; Ollama is never touched
```

So the line `OCR check: 0 sparse page(s)` means the check ran and the model did **not**. If there are sparse pages it calls `vision_ocr_pages`; on an exception it returns the original pages plus a warning `("ocr_fallback", exc)`, so the file still converts. Returns `(pages, number replaced, model name or None, warnings)`.

### 8.3 `vision_ocr_pages(pdf, page_numbers)`

For each sparse page:

```python
image = pdf_doc[page_no - 1].render(scale=2.0).to_pil()        # pypdfium2 renders the page (~144 dpi)
image.save(buf, format="PNG")
reply = client.chat(model=model,
        messages=[{"role": "user", "content": FALLBACK_PROMPT, "images": [buf.getvalue()]}],
        options=options)                                        # model + options come from config.json
```

`FALLBACK_PROMPT` asks for Markdown, tables as Markdown tables, **merged-cell text repeated in every row and column**, `[unreadable]` instead of guessing, and nothing but the Markdown. An empty answer leaves that page out of the result (the original text is kept). `ollama` and `pypdfium2` are imported inside the function, so a run that never needs the fallback does not need them installed.

### 8.4 `join_pages(page_markdown)`

```python
"\n\n".join(f"<!-- page: {n} -->\n\n{md.strip()}" for n, md in sorted(page_markdown.items())) + "\n"
```

Every page gets a `<!-- page: N -->` marker. These are what later steps (citations, `page_reference`) rely on.

---

## 9. Worked example

`DRGR_Series.pdf` (a 5-page datasheet), **Dimensions** table on page 2.

| | Result |
|---|---|
| Docling alone | `Type`, `A` columns only; columns B to P appear as 30 loose lines of numbers. A drawing next to the table made its layout model crop the table. |
| After `ruled_tables` | complete 8-column table |

What happened inside `extract_tables`:

1. `_visible_graphics_page` dropped two white frames behind the drawings (they would have produced a giant fake cell).
2. pdfplumber returned dozens of cell rectangles. Header cells were inset by white gutters (`33.7`, `84.7`) relative to the body columns (`28.3`, `90.1`).
3. `_merge_boundaries` merged each gutter with its neighbour because no character lies between them, giving 8 real columns.
4. The coverage check passed (all characters lie inside cells).
5. `apply_to_docling_document` found Docling's cropped 2-column table overlapping the region, replaced its `data` with the 4×8 grid, and deleted the loose number lines (`texts_hit`).

Final Markdown:

```
| Type    | A        | B        | C       | D       | D1      | E        | P        |
|---|---|---|---|---|---|---|---|
| DRGR664 | 6.0±0.5  | 6.5 max. | 4.0±1.0 | 4.0±0.3 | –       | 0.50±0.1 | –        |
```

The opposite case, the **H1 2026 press release**: its tables have no cell borders, only a dotted frame around one column and some section rules. pdfplumber found a "grid" whose only cells were the framed column. The coverage check (the cells contained less than 98% of the text in the table's area) rejected it, `0 rebuilt from PDF lines (8 left from Docling)` was logged, and Docling's correct tables stayed.

---

## 10. Failure handling

| What fails | Where | Effect |
|---|---|---|
| File unreadable / hashing fails | `hash` | Error row (hash `NULL`). File counted as failed. |
| Database error while checking or saving | `db` | Error row. Counted as failed. |
| Docling raises (corrupt PDF, model download error) | `convert` | `failed` row + error row. Retried next run. |
| Disk write fails | `write` | `failed` row + error row. |
| `ruled_tables` raises | warning `ruled_tables` | File still converts with Docling's own tables. |
| Ollama unreachable / model missing | warning `ocr_fallback` | File converts with Docling's text for sparse pages. |
| Database down at start | `run_pipeline` | Exits immediately; prints host only. |

Failed PDFs are retried automatically on the next run, because only `converted` rows count as "done".

---

## 11. Every threshold in one place

| Name | Value | File | Meaning |
|---|---|---|---|
| `MIN_PAGE_CHARS` | 100 (`OCR_FALLBACK_MIN_PAGE_CHARS`) | `vision_ocr.py` | A page with fewer real characters goes to the vision model |
| `RENDER_SCALE` | 2.0 | `vision_ocr.py` | Page render scale for the vision model |
| `MIN_TEXT_IN_CELLS` | 0.98 | `ruled_tables.py` | Share of the text in a table's area that must lie inside drawn cells |
| `FINDER_SETTINGS` | snap 1, join 3, intersection 3, min edge 3 | `ruled_tables.py` | pdfplumber line detection (see 7.2) |
| `_merge_boundaries` `tol` | 1.0 pt | `ruled_tables.py` | Boundaries closer than this are one boundary |
| area fill threshold | > 2 pt | `ruled_tables.py` | Only bigger fills can be "seamless" background rectangles |
| box stacking | ≥ 50% overlap, gap 0 to 20 pt | `ruled_tables.py` | Boxes in one vertical stack |
| box row | tops ±4 pt, bottoms ±45 pt, ≥ 3 stacks | `ruled_tables.py` | Stacks that form one table |
| rule width | ≥ 40 pt | `ruled_tables.py` | Shorter rules are ignored |
| rule chain | extent ±6 pt, gap 8 to 80 pt, spacing ±40%, ≥ 3 rules | `ruled_tables.py` | Rules that form one table |
| column split | present in ≥ 60% of rules | `ruled_tables.py` | A real column boundary |
| overlap to replace | > 30% of the Docling table inside the region | `ruled_tables.py` | Which Docling tables get replaced |

---

## 12. Tests

```bash
python tests/test_table_spans.py        # docling-core repeats merged-cell text in every row/column
python tests/test_convert_tables.py     # boundary merging, nested-table folding, column spans,
                                        # pipe escaping, key/value form detection and rejection
```

Both use synthetic data only, with no PDF fixtures. The tests that matter most for regressions:

- `test_gutter_boundaries_are_merged`: the inset-header case from section 7.4.
- `test_merged_cells_repeat_and_nested_table_is_folded`: the nested-table case from 7.5.
- `test_rules_with_several_lines_per_band_are_not_a_form` and `test_ruled_bottom_rows_of_unruled_table_are_rejected`: the two rules that protect Docling's correct tables.

When a new PDF converts wrongly, first reproduce it with `ruled_tables.extract_tables(pdf)` alone and look at the tables it returns. Decide whether the fix is a new rejection rule (the lines should not have been trusted) or a better grid rule, and add a regression test.

---

## 13. Extending and known limits

**Adding another converter.** Everything in `common.py` is reusable. A new pipeline only needs:

```python
from dataPipeline.convert.common import Conversion, run_pipeline, ROOT

def convert(pdf):                                   # PDF in, Markdown out
    return Conversion(markdown="...", pages=3, tables=1)

run_pipeline("my_converter", "1.0", ROOT / "markdowns_mine", convert, "description")
```

It then gets hashing, skipping, atomic writes, database tracking and error logging for free, with its own rows (`converter = 'my_converter'`).

**Known limits**

- Only tables **drawn with lines** are rebuilt exactly. Tables without lines (common in financial statements) rely on Docling's table model.
- Scanned pages have no text layer, so `ruled_tables` ignores them; Docling's OCR and, if needed, the vision model handle them.
- Table rules were tuned on two PDFs (a datasheet and a press release). A new layout may need a rejection rule or a threshold change.
- A table continued on the next page is two tables.
- The vision fallback is triggered by *page text length* only. A page that is mostly text but has one badly read table is not detected.
- Every PDF is re-hashed on each run (cheap compared with conversion).
- No locking: two simultaneous runs could convert the same file twice. The unique index keeps the table consistent; the output file is last-writer-wins.
- PDFs deleted from `reports/` keep their rows and Markdown.
