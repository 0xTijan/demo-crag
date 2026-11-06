"""Regression tests for the table logic of the conversion pipelines.

Synthetic inputs only (no PDF fixtures). Run: python tests/test_convert_tables.py
(or pytest). Needs the pipeline dependencies (pdfplumber, sqlalchemy, dotenv).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataPipeline.convert import ruled_tables  # noqa: E402
from dataPipeline.convert.llamaparse_pipeline import html_tables_to_markdown  # noqa: E402


def test_gutter_boundaries_are_merged():
    # header cells inset by white gutters (33.7/84.7) vs body columns (28.3/90.1):
    # no text between them, so they collapse into one boundary each
    boundaries = [28.3, 33.7, 84.7, 90.1, 94.2, 154.3]
    char_centers = [50.0, 60.0, 120.0]
    assert ruled_tables._merge_boundaries(boundaries, char_centers) == [28.3, 89.66666666666667, 154.3]


def test_merged_cells_repeat_and_nested_table_is_folded():
    # Item | Specification | Test Method (3 sub-columns for the nested table)
    texts = {0: "Item", 1: "Specification", 2: "Test Method",
             3: "Shock", 4: "No damage", 5: "10 cycles",
             6: "Cycle", 7: "One cycle:", 8: "Step", 9: "Temp", 10: "Time",
             11: "1", 12: "-25", 13: "30", 14: "Total: 100",
             15: "Humidity", 16: "40C"}
    grid = [
        [0, 1, 2, 2, 2],
        [3, 4, 5, 5, 5],
        [6, 4, 7, 7, 7],
        [6, 4, 8, 9, 10],
        [6, 4, 11, 12, 13],
        [6, 4, 14, 14, 14],
        [15, 4, 16, 16, 16],
    ]
    rows = ruled_tables._fill(ruled_tables._flatten_nested(grid, texts), texts)
    assert rows == [
        ["Item", "Specification", "Test Method"],
        ["Shock", "No damage", "10 cycles"],
        ["Cycle", "No damage", "One cycle:<br>Step: 1, Temp: -25, Time: 30<br>Total: 100"],
        ["Humidity", "No damage", "40C"],
    ]


def test_plain_column_span_is_kept():
    texts = {0: "Group", 1: "A", 2: "B", 3: "1", 4: "2"}
    grid = [[0, 0], [1, 2], [3, 4]]
    rows = ruled_tables._fill(ruled_tables._flatten_nested(grid, texts), texts)
    assert rows == [["Group", "Group"], ["A", "B"], ["1", "2"]]


def test_html_tables_spans_and_nested():
    html = ("<table><tr><th>Item</th><th>Spec</th></tr>"
            "<tr><td>A</td><td rowspan='2'>Same</td></tr>"
            "<tr><td>B<table><tr><th>Step</th></tr><tr><td>1</td></tr></table></td></tr>"
            "<tr><td colspan='2'>Wide</td></tr></table>")
    md, count = html_tables_to_markdown("x\n" + html + "\ny")
    assert count == 1
    assert "| A | Same |" in md
    assert "| B Step: 1 | Same |" in md
    assert "| Wide | Wide |" in md
    assert md.startswith("x\n") and md.endswith("\ny")


def test_rows_to_markdown_escapes_pipes():
    md = ruled_tables.rows_to_markdown([["a|b", "c"], ["1", "2"]])
    assert md.splitlines()[0] == "| a\\|b | c |"


class _FakePage:
    """Just enough of a pdfplumber page for _rule_tables(): edges and chars."""

    def __init__(self, rules, words):
        self.edges = [{"orientation": "h", "top": y, "x0": a, "x1": b} for y, segs in rules for a, b in segs]
        self.chars = []
        for x, top, text in words:  # one fake char per word is enough for the checks
            self.chars.append({"x0": x, "x1": x + 6 * len(text), "top": top, "bottom": top + 8,
                               "text": text, "upright": True, "doctop": top, "size": 8,
                               "fontname": "F", "adv": 6})


def _form_rules(ys):
    # rules split at x=185 (the column split of a key/value form)
    return [(y, [(100, 185), (185, 500)]) for y in ys]


def test_rules_form_is_detected():
    words = [(101, 172, "Customer:"), (101, 196, "Product:"), (190, 196, "DRGR"),
             (101, 220, "Sizes:"), (190, 220, "664"), (101, 244, "Edition:"), (190, 244, "B5")]
    tables = ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236, 260]), words), [], 1)
    assert [t.rows for t in tables] == [[["Customer:", ""], ["Product:", "DRGR"], ["Sizes:", "664"], ["Edition:", "B5"]]]


def test_rules_with_several_lines_per_band_are_not_a_form():
    # section separators: each band holds several text lines -> not a table
    words = [(101, 172, "A"), (101, 196, "B"), (101, 202, "B2"), (101, 220, "C"), (101, 244, "D")]
    assert ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236, 260]), words), [], 1) == []


def test_ruled_bottom_rows_of_unruled_table_are_rejected():
    # the row just above the ruled rows has text in both columns: fragment
    words = [(101, 148, "Above"), (190, 148, "1.0"), (101, 172, "Total"), (190, 172, "2.0"),
             (101, 196, "AUM"), (190, 196, "3.0"), (101, 220, "Sum"), (190, 220, "5.0")]
    assert ruled_tables._rule_tables(_FakePage(_form_rules([188, 212, 236]), words), [], 1) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
