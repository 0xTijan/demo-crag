# Tests

Automated checks for the table logic of the PDF → Markdown pipeline (`dataPipeline/convert/`).

They exist to catch regressions. The pipeline's table rules are subtle (merged cells, nested tables, when to trust a table drawn with lines and when to leave it to Docling), and a small change to a rule or an upgrade of a library can silently break one of them. These tests fail loudly when that happens.

- [1. What the tests are, and what they are not](#1-what-the-tests-are-and-what-they-are-not)
- [2. Running them](#2-running-them)
- [3. Files](#3-files)
- [4. `test_table_spans.py`: merged cells in docling-core](#4-test_table_spanspy-merged-cells-in-docling-core)
- [5. `test_convert_tables.py`: the table rules in `ruled_tables.py`](#5-test_convert_tablespy-the-table-rules-in-ruled_tablespy)
- [6. How the rule tests fake a PDF page](#6-how-the-rule-tests-fake-a-pdf-page)
- [7. Adding a test](#7-adding-a-test)
- [8. What the tests do not cover](#8-what-the-tests-do-not-cover)
- [9. Troubleshooting](#9-troubleshooting)

---

## 1. What the tests are, and what they are not

**They are unit tests on synthetic data.** Every input is written directly in the test file: a small grid of numbers, a list of fake lines and words. There are no PDF files, no database, no Ollama and no network. The whole suite runs in well under a second and writes nothing to disk.

**They are not a quality check for a given PDF.** No test takes a PDF, and none converts one. A passing run means "the rules still behave the way they were written down", not "your next PDF will convert well". The only way to know that is to run `dataPipeline/convert/docling.py` on the PDF and read the Markdown. See `dataPipeline/convert/README.md`.

```
tests/ ──imports──► dataPipeline/convert/ruled_tables.py    (the rules under test)
       ──imports──► docling_core                            (checks a library behaviour we rely on)
```

---

## 2. Running them

From anywhere, using the Python environment that has the pipeline packages installed:

```bash
python tests/test_table_spans.py
python tests/test_convert_tables.py
```

Each file is also a script: its `if __name__ == "__main__":` block runs every function whose name starts with `test_` and prints one line per test.

```
$ python tests/test_table_spans.py
ok

$ python tests/test_convert_tables.py
ok test_gutter_boundaries_are_merged
ok test_merged_cells_repeat_and_nested_table_is_folded
ok test_plain_column_span_is_kept
ok test_rows_to_markdown_escapes_pipes
ok test_rules_form_is_detected
ok test_rules_with_several_lines_per_band_are_not_a_form
ok test_ruled_bottom_rows_of_unruled_table_are_rejected
```

Behaviour of the script runner: exit code `0` if all pass. On the first failing assertion Python stops with a traceback (exit code `1`) and the remaining tests in that file do not run.

The functions follow pytest's naming convention (`test_*` in `test_*.py`), so `pytest tests/` should also work, and it would report every failure instead of stopping at the first. pytest is not a project dependency and the runs documented here were done with the plain `python` commands above.

**Requirements**

| File | Needs |
|---|---|
| `test_table_spans.py` | `docling-core` (installed with `docling`) |
| `test_convert_tables.py` | `pdfplumber` (imported by `ruled_tables.py`) |

`test_convert_tables.py` adds the repository root to `sys.path` itself, so it works from any directory:

```python
ROOT = Path(__file__).resolve().parents[1]     # tests/test_convert_tables.py -> repo root
sys.path.insert(0, str(ROOT))
from dataPipeline.convert import ruled_tables
```

---

## 3. Files

| File | Tests | Covers |
|---|---|---|
| `test_table_spans.py` | 1 | A behaviour of `docling-core` that the pipeline relies on |
| `test_convert_tables.py` | 7 | The table-building and table-rejecting rules in `ruled_tables.py` |
| `test_extract_numbers.py` | 6 | Number parsing for `dataPipeline/extract` (`1.234,5`, `(12)`, dashes, ambiguous `1.234`) |
| `test_extract_locate.py` | 8 | Page splitting, statement locating (group vs company, table of contents), period/unit/currency detection, industry |
| `test_extract_derive.py` | 4 | Derived `financials` / `valuation_ratios` columns and NUMERIC overflow |
| `test_extract_validate.py` | 4 | The consistency checks that set `needs_review` |
| `test_extract_llm.py` | 6 | Model mapping with a fake model: which calls are made, numbers not on the page are dropped, signs come from the cell |
| `extract_sample.py` | – | Shared synthetic report (Markdown in the `convert/` format) and the fake model's answers |

The `test_extract_*` files need only `sqlalchemy` and `python-dotenv` (imported through `dataPipeline/convert/common.py`). No database, no Ollama.

---

## 4. `test_table_spans.py`: merged cells in docling-core

**Why it exists.** The pipeline has no code of its own that repeats a merged cell's text into every row it covers when Docling's table is exported to Markdown. It relies on docling-core's Markdown export doing so. If a future docling-core release stopped doing that, merged cells would silently come out blank in Docling-sourced tables. This test fails first.

**How it works.** It builds a 3×3 table by hand with two merged cells:

```python
def _cell(text, r0, r1, c0, c1, header=False):          # r0..r1 / c0..c1 = rows/columns the cell covers
    return TableCell(text=text,
                     start_row_offset_idx=r0, end_row_offset_idx=r1,
                     start_col_offset_idx=c0, end_col_offset_idx=c1,
                     row_span=r1 - r0, col_span=c1 - c0, column_header=header)

cells = [
    _cell("Type", 0, 3, 0, 1, header=True),             # spans 3 rows
    _cell("Size", 0, 1, 1, 3, header=True),             # spans 2 columns
    _cell("A", 1, 2, 1, 2), _cell("B", 1, 2, 2, 3),
    _cell("C", 2, 3, 1, 2), _cell("D", 2, 3, 2, 3),
]
```

It puts the table in a `DoclingDocument`, exports Markdown, parses the pipe rows back into lists (skipping the `|---|` separator line), and asserts:

```python
assert rows == [["Type", "Size", "Size"],      # "Size" is repeated in both columns it spans
                ["Type", "A", "B"],            # "Type" is repeated in all three rows
                ["Type", "C", "D"]]
```

---

## 5. `test_convert_tables.py`: the table rules in `ruled_tables.py`

Seven tests. The first four test pure functions with plain Python values. The last three test the horizontal-rule detector with a fake page (section 6).

### 5.1 `test_gutter_boundaries_are_merged`

Protects `_merge_boundaries`. The real case was a table header drawn with white gutters, so its cell edges (`33.7`, `84.7`) sat slightly inside the body column edges (`28.3`, `90.1`). Without merging, every gutter becomes an empty extra column.

```python
boundaries   = [28.3, 33.7, 84.7, 90.1, 94.2, 154.3]    # candidate column edges from the cells
char_centers = [50.0, 60.0, 120.0]                       # x position of the text
assert ruled_tables._merge_boundaries(boundaries, char_centers) == [28.3, 89.66666666666667, 154.3]
```

Why that result: two neighbouring boundaries are merged when **no character centre lies between them**.

- `28.3` and `33.7`: nothing between them, so they merge. The outermost boundary is kept exactly, so the result starts at `28.3`.
- `33.7` and `84.7`: characters at `50` and `60` lie between them, so they stay separate.
- `84.7`, `90.1`, `94.2`: nothing between any of them, so they merge into their mean, `89.666…`.
- `154.3` is the last boundary and is kept exactly.

Three columns' worth of boundaries instead of six.

### 5.2 `test_merged_cells_repeat_and_nested_table_is_folded`

Protects `_flatten_nested` and `_fill`, using the shape of the real DRGR datasheet table. The grid holds cell ids; `texts` maps an id to its text. Id `4` ("No damage") appears in several rows, which is how a merged cell looks in the grid. The right-hand column is a header spanning three sub-columns (ids `2`, `5`, `7`, `14`, `16` span all three) with a small nested table in the middle (ids `8, 9, 10` is its header, `11, 12, 13` a data row).

```python
grid = [
    [0, 1, 2, 2, 2],        # Item | Specification | Test Method (spans 3 sub-columns)
    [3, 4, 5, 5, 5],        # Shock   | No damage | 10 cycles
    [6, 4, 7, 7, 7],        # Cycle   | No damage | "One cycle:"
    [6, 4, 8, 9, 10],       #                      | Step | Temp | Time   <- nested header
    [6, 4, 11, 12, 13],     #                      | 1    | -25  | 30     <- nested row
    [6, 4, 14, 14, 14],     #                      | "Total: 100"
    [15, 4, 16, 16, 16],    # Humidity | No damage | 40C
]
```

Expected output:

```python
[["Item", "Specification", "Test Method"],
 ["Shock", "No damage", "10 cycles"],
 ["Cycle", "No damage", "One cycle:<br>Step: 1, Temp: -25, Time: 30<br>Total: 100"],   # nested table folded
 ["Humidity", "No damage", "40C"]]                                                       # "No damage" repeated
```

Two behaviours are asserted at once: the merged "No damage" cell is repeated in every row it covers, and the four rows of the "Cycle" block (id `6` spans them) collapse into one row whose last cell holds the nested table as `Header: value` lines separated by `<br>`.

### 5.3 `test_plain_column_span_is_kept`

The counterweight to 5.2: an ordinary header that spans two columns must **not** be mistaken for a nested table.

```python
grid = [[0, 0], [1, 2], [3, 4]]          # "Group" spans both columns; the rows below are normal
assert rows == [["Group", "Group"], ["A", "B"], ["1", "2"]]
```

`_flatten_nested` only folds a header span when the rows below split it into different cells *and* some column outside the span covers those rows, which defines the single outer row to fold them into. Here every column is inside the span, so there is no outer row, nothing is folded, and the span is kept and simply repeated.

### 5.4 `test_rows_to_markdown_escapes_pipes`

```python
md = ruled_tables.rows_to_markdown([["a|b", "c"], ["1", "2"]])
assert md.splitlines()[0] == "| a\\|b | c |"
```

A `|` inside a cell would otherwise start a new column in the Markdown. The function must write it as `\|`.

### 5.5 `test_rules_form_is_detected`

Protects the positive case of `_rule_tables`: a key/value form drawn with horizontal lines only (the Customer/Product/Sizes/Edition block on the DRGR cover).

```python
words = [(101, 172, "Customer:"), (101, 196, "Product:"), (190, 196, "DRGR"),
         (101, 220, "Sizes:"),    (190, 220, "664"),
         (101, 244, "Edition:"),  (190, 244, "B5")]                  # (x, top, text)
tables = ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236, 260]), words), [], 1)
assert [t.rows for t in tables] == [[["Customer:", ""], ["Product:", "DRGR"],
                                     ["Sizes:", "664"], ["Edition:", "B5"]]]
```

Four horizontal rules at y = 188, 212, 236, 260 form a chain; every gap between rules holds exactly one line of text; the rules are broken at x = 185, which is the column split. Note that `Customer:` sits *above* the first rule, in the extra "header band" that the detector adds, and has an empty value.

### 5.6 `test_rules_with_several_lines_per_band_are_not_a_form`

Protects the **rejection** that fixed a real bug on a press release. Horizontal lines that are just section separators must not be turned into a table.

```python
words = [(101, 172, "A"), (101, 196, "B"), (101, 202, "B2"), (101, 220, "C"), (101, 244, "D")]
assert ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236, 260]), words), [], 1) == []
```

The band between the first two rules contains two lines of text (`B` at 196 and `B2` at 202, six points apart). A key/value form has one line per row, so two lines means "not a form" and nothing is returned, which leaves that area to Docling.

### 5.7 `test_ruled_bottom_rows_of_unruled_table_are_rejected`

Protects the second rejection from the same bug: a table whose **only last rows** have lines under them.

```python
words = [(101, 148, "Above"), (190, 148, "1.0"),      # a row with text in BOTH columns, just above the rules
         (101, 172, "Total"), (190, 172, "2.0"),
         (101, 196, "AUM"),   (190, 196, "3.0"),
         (101, 220, "Sum"),   (190, 220, "5.0")]
assert ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236]), words), [], 1) == []
```

Every band here has exactly one line, so the "several lines per band" rule passes. But the line just above the table has words in two columns, which means the table continues upward without lines and the ruled part is only a fragment. Returning it as a table would insert the bottom rows separately and lose the top ones, so the detector returns nothing and Docling keeps the whole table.

---

## 6. How the rule tests fake a PDF page

`_rule_tables(page, taken, page_no)` needs a pdfplumber page, but only touches two attributes: `page.edges` (the drawn lines) and `page.chars` (the characters with coordinates). The test file defines a minimal stand-in:

```python
class _FakePage:
    def __init__(self, rules, words):
        self.edges = [{"orientation": "h", "top": y, "x0": a, "x1": b}      # one horizontal line segment each
                      for y, segs in rules for a, b in segs]
        self.chars = []
        for x, top, text in words:                                          # one fake "character" per word
            self.chars.append({"x0": x, "x1": x + 6 * len(text),            # width grows with the text
                               "top": top, "bottom": top + 8,               # 8pt tall
                               "text": text, "upright": True, "doctop": top,
                               "size": 8, "fontname": "F", "adv": 6})
```

and a helper that draws a rule as **two segments that meet at x = 185**:

```python
def _form_rules(ys):
    return [(y, [(100, 185), (185, 500)]) for y in ys]
```

That meeting point is what the detector reads as the column split. Using one "character" per word is enough because the rules under test only look at where text sits, not at individual letters.

The tuples in `words` are `(x, top, text)` in PDF points with a top-left origin, so a larger `top` is lower on the page.

---

## 7. Adding a test

When a PDF converts wrongly, the routine is:

1. Run `ruled_tables.extract_tables(pdf)` on its own and look at the tables it returns. This tells you whether the problem is in this module or in Docling.
2. Decide the kind of fix: a new **rejection** rule (the lines should not have been trusted) or a better **grid** rule.
3. Reduce the situation to a few numbers and write a test that fails before your fix and passes after it.

Template for a rule test (no PDF needed):

```python
def test_my_case():
    # (x, top, text) words and the y positions of the horizontal rules
    words = [(101, 172, "Label"), (190, 172, "Value"), ...]
    tables = ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236]), words), [], 1)
    assert tables == []        # or the exact expected rows
```

For a grid rule, write the id grid and `texts` dictionary by hand as in 5.2 and compare `_fill(_flatten_nested(grid, texts), texts)` with the expected rows.

Conventions:

- File name `test_*.py`, function name `test_*`, so both the script runner and pytest find it.
- Keep inputs inline and tiny. Do not commit third-party PDFs.
- Test one behaviour per function, and say in a comment which real-world case it came from.

---

## 8. What the tests do not cover

- **No end-to-end conversion.** Nothing converts a real PDF and compares the Markdown.
- **`docling.py` and `common.py`** (hashing, skipping, database rows, error rows, writing files) have no tests. They were verified by running the pipeline against a real database.
- **`vision_ocr.py`**: the sparse-page check and the Ollama call are untested.
- **`extract_tables` and `apply_to_docling_document`** run on real pdfplumber pages and Docling documents, which the synthetic tests do not construct. Only the helper rules they call are tested.
- **Layouts beyond the two the rules were tuned on** (a datasheet and a press release).

If you want a stronger safety net, the next step would be a small end-to-end test: a short PDF kept in the repo whose expected Markdown is stored next to it, converted with `docling.py` and compared. That needs a PDF you are free to commit.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: pdfplumber` | Run with the environment that has the pipeline installed (the same one used for `docling.py`). |
| `ModuleNotFoundError: docling_core` | `test_table_spans.py` needs `docling` (which installs `docling-core`). |
| `ModuleNotFoundError: dataPipeline` | You ran a file other than the ones in `tests/`, or copied a test out of the repo. The test adds the repo root itself only when it lives in `tests/`. |
| `test_table_spans.py` fails after upgrading Docling | docling-core changed how merged cells are exported. Merged-cell text is now blank in Docling-sourced tables; this needs a fix in the pipeline, not in the test. |
| A rule test fails after you edited `ruled_tables.py` | Read the failing assertion: it prints the rows the code actually produced. Either the change broke the rule, or the rule was meant to change, in which case update the test and the matching section of `dataPipeline/convert/README.md`. |
