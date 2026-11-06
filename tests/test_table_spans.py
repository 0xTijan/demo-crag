"""Guard: merged table cells must be repeated in every row/column they cover.

The converter relies on docling-core's Markdown export for this. If a future
docling-core release stops repeating spanned text, this test fails.
Run: python tests/test_table_spans.py   (or pytest)
"""
from docling_core.types.doc import DoclingDocument, TableCell, TableData


def _cell(text, r0, r1, c0, c1, header=False):
    return TableCell(
        text=text,
        start_row_offset_idx=r0, end_row_offset_idx=r1,
        start_col_offset_idx=c0, end_col_offset_idx=c1,
        row_span=r1 - r0, col_span=c1 - c0, column_header=header,
    )


def test_rowspan_and_colspan_text_is_repeated():
    cells = [
        _cell("Type", 0, 3, 0, 1, header=True),      # spans 3 rows
        _cell("Size", 0, 1, 1, 3, header=True),      # spans 2 columns
        _cell("A", 1, 2, 1, 2), _cell("B", 1, 2, 2, 3),
        _cell("C", 2, 3, 1, 2), _cell("D", 2, 3, 2, 3),
    ]
    doc = DoclingDocument(name="t")
    doc.add_table(data=TableData(num_rows=3, num_cols=3, table_cells=cells))
    rows = [
        [c.strip() for c in line.strip("|").split("|")]
        for line in doc.export_to_markdown().splitlines()
        if line.startswith("|") and not set(line) <= set("|- ")
    ]
    assert rows == [["Type", "Size", "Size"], ["Type", "A", "B"], ["Type", "C", "D"]]


if __name__ == "__main__":
    test_rowspan_and_colspan_text_is_repeated()
    print("ok")
