"""PDF -> Markdown with pdfplumber only (no ML models).

Usage:  python dataPipeline/convert/pdfplumber_pipeline.py [--force]

Same bookkeeping as docling.py (hashing, DB tracking, retries; see common.py),
but the conversion uses only the PDF's own text layer and vector graphics:

* Tables: ruled_tables.py rebuilds every table drawn with lines (merged cells
  repeated in every row/column they cover, nested tables folded into their cell).
* Text outside tables: words -> lines -> cells (split at wide gaps). Column
  gutters are detected, so side-by-side columns are read one at a time.
  Lines whose cells line up vertically become a table (tables aligned with
  spaces instead of lines). Larger-than-body font = heading. Headers/footers
  repeating on most pages (incl. page numbers) are dropped.
* Pages without a text layer (scanned) have no text here, so they always go to
  the vision OCR fallback (vision_ocr.py).

Output goes to markdowns_pdfplumber/ with a `<!-- page: N -->` marker per page.
"""
import re
import statistics
import sys
import time
from importlib.metadata import version as pkg_version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root importable

import pdfplumber  # noqa: E402

from dataPipeline.convert import ruled_tables, vision_ocr  # noqa: E402
from dataPipeline.convert.common import ROOT, Conversion, log, run_pipeline  # noqa: E402

CONVERTER = "pdfplumber"
OUTPUT_DIR = ROOT / "markdowns_pdfplumber"
LINE_TOLERANCE = 3     # words whose tops differ by less than this are on one line
HEADING_RATIO = 1.15   # font size this much above the page's body size = heading
COLUMN_GUTTER = 15     # min empty vertical strip (pt) between two text columns
ALIGN_TOLERANCE = 4    # cells of a whitespace table start within this many pt
PROSE_LINE_CHARS = 45  # median line length above which side-by-side text is prose columns
SECTION_GAP = 2.5      # vertical gap (x median line spacing) that ends a column region
BULLET_PREFIX = re.compile(r"^[－•·▪●◦]\s*")  # list markers at the start of table cells
FURNITURE_ZONE = 0.09  # top/bottom share of the page searched for headers/footers


def _cells(words):
    """Group words into lines, then split each line into cells at wide gaps.

    A gap wider than 1.2x the font size (about three word spaces) separates
    cells of a whitespace-aligned table or two side-by-side columns.
    Returns cells as dicts: x0, x1, top, bottom, size, text.
    """
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    cells = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        group = [line[0]]
        for w in line[1:] + [None]:
            if w is not None and w["x0"] - group[-1]["x1"] <= max(6.0, 1.2 * w["size"]):
                group.append(w)
                continue
            cells.append({
                "x0": group[0]["x0"], "x1": max(g["x1"] for g in group),
                "top": min(g["top"] for g in group), "bottom": max(g["bottom"] for g in group),
                "size": statistics.fmean(g["size"] for g in group),
                "text": " ".join(g["text"] for g in group),
            })
            group = [w] if w is not None else []
    return cells


def _row_aligned(left, right) -> bool:
    """True if two side-by-side groups are columns of ONE table, not text columns.

    Table cells in a row share the exact same top, so most lines of the smaller
    side have a partner line on the other side at the same height. Two columns
    of running text are excluded by line length (prose lines are long).
    """
    def lines_of(blocks):
        return [ln for ln in _lines(sorted(blocks, key=lambda b: (round(b["top"]), b["x0"])))
                if ln[0].get("table") is None]

    lt, rt = lines_of(left), lines_of(right)
    if not lt or not rt:
        return False

    def median_len(lines):
        return statistics.median(len(" ".join(c["text"] for c in ln)) for ln in lines)

    if min(median_len(lt), median_len(rt)) > PROSE_LINE_CHARS:
        return False
    small, big = (lt, rt) if len(lt) <= len(rt) else (rt, lt)
    hits = sum(1 for a in small if any(abs(a[0]["top"] - b[0]["top"]) <= 1.0 for b in big))
    return hits >= 0.7 * len(small)


def _free_intervals(line, lo, hi):
    """Empty horizontal intervals of one line between the page's text edges."""
    out, x = [], lo
    for b in sorted(line, key=lambda b: b["x0"]):
        if b["x0"] > x:
            out.append((x, b["x0"]))
        x = max(x, b["x1"])
    if hi > x:
        out.append((x, hi))
    return out


def _intersect(a, b):
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi > lo:
                out.append((lo, hi))
    return out


def _split_columns(blocks):
    """Find a column gutter: an empty vertical strip shared by >=3 consecutive lines.

    Returns (before, left, right, after) block lists, or None. A strip whose two
    sides are row-aligned short cells is a whitespace table, not columns, and is
    not split.
    """
    lines = _lines(sorted(blocks, key=lambda b: (round(b["top"]), b["x0"])))
    lo = min(b["x0"] for b in blocks)
    hi = max(b["x1"] for b in blocks)

    def interior(ivs):
        return [(a, b) for a, b in ivs if b - a >= COLUMN_GUTTER and a > lo + 1 and b < hi - 1]

    tops = [ln[0]["top"] for ln in lines]
    spacing = statistics.median([b - a for a, b in zip(tops, tops[1:])] or [12.0])

    for i in range(len(lines)):
        cur = interior(_free_intervals(lines[i], lo, hi))
        j = i
        while cur and j + 1 < len(lines):
            if tops[j + 1] - tops[j] > SECTION_GAP * max(spacing, 6.0):
                break  # big vertical gap = new section, the column region ends here
            nxt = interior(_intersect(cur, _free_intervals(lines[j + 1], lo, hi)))
            if not nxt:
                break
            cur, j = nxt, j + 1
        if not cur or j - i + 1 < 3:
            continue
        a, b = max(cur, key=lambda iv: iv[1] - iv[0])
        region = [blk for ln in lines[i:j + 1] for blk in ln]
        left = [blk for blk in region if blk["x1"] <= a + 0.5]
        right = [blk for blk in region if blk["x0"] >= b - 0.5]
        if len(_lines(left)) < 2 or len(_lines(right)) < 2 or _row_aligned(left, right):
            continue
        before = [blk for ln in lines[:i] for blk in ln]
        after = [blk for ln in lines[j + 1:] for blk in ln]
        return before, left, right, after
    return None


def _order(blocks):
    """Reading order: top to bottom, but each side-by-side column region is read
    column by column (recursively, so 3+ columns and nested columns work)."""
    if len(blocks) <= 1:
        return blocks
    split = _split_columns(blocks)
    if split is None:
        return sorted(blocks, key=lambda b: (round(b["top"]), b["x0"]))
    before, left, right, after = split
    return _order(before) + _order(left) + _order(right) + _order(after)


def _lines(blocks):
    """Group ordered blocks back into lines (same top), keeping order."""
    lines: list[list[dict]] = []
    for b in blocks:
        if lines and b.get("table") is None and lines[-1][0].get("table") is None \
                and abs(b["top"] - lines[-1][0]["top"]) <= LINE_TOLERANCE:
            lines[-1].append(b)
        else:
            lines.append([b])
    return lines


def _whitespace_table(lines, i):
    """Rows of a table aligned with spaces, starting at lines[i], or None.

    Column anchors come from the first line (>=2 cells). Following lines belong
    to the table while every cell starts (or ends) at an anchor; missing cells
    stay empty. Needs >=2 rows with >=2 cells; trailing 1-cell lines are dropped
    (they are usually the paragraph after the table).
    """
    first = lines[i]
    if len(first) < 2 or first[0].get("table") is not None:
        return None
    anchors = [(c["x0"], c["x1"]) for c in sorted(first, key=lambda c: c["x0"])]

    def column_of(c):
        for k, (x0, x1) in enumerate(anchors):
            if abs(c["x0"] - x0) <= ALIGN_TOLERANCE or abs(c["x1"] - x1) <= ALIGN_TOLERANCE:
                return k
        return None

    rows, j = [], i
    while j < len(lines) and lines[j][0].get("table") is None:
        cols = [column_of(c) for c in lines[j]]
        if None in cols or len(set(cols)) != len(cols):
            break
        row = [""] * len(anchors)
        for c, k in zip(lines[j], cols):
            row[k] = BULLET_PREFIX.sub("", c["text"])
        rows.append(row)
        j += 1
    while rows and sum(1 for v in rows[-1] if v) < 2:
        rows.pop()
    if sum(1 for r in rows if sum(1 for v in r if v) >= 2) < 2:
        return None
    return rows, i + len(rows)


def _furniture(pages_cells, page_heights):
    """Texts that repeat at the top/bottom of most pages (headers, footers, page numbers)."""
    def key(text):
        return re.sub(r"\d+", "#", text.strip())
    seen: dict[str, int] = {}
    for cells, height in zip(pages_cells, page_heights):
        keys = {key(c["text"]) for c in cells
                if c["top"] < FURNITURE_ZONE * height or c["bottom"] > (1 - FURNITURE_ZONE) * height}
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
    needed = max(2, int(0.5 * len(pages_cells) + 0.5))
    return {k for k, n in seen.items() if n >= needed}, key


def page_markdown(cells, tables, height, furniture, key) -> str:
    """Markdown for one page: text, whitespace tables and ruled tables in reading order."""
    regions = [(t.bbox[0] - 2, t.bbox[1] - 2, t.bbox[2] + 2, t.bbox[3] + 2) for t in tables]

    def keep(c):
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        if any(x0 <= cx <= x1 and t <= cy <= b for x0, t, x1, b in regions):
            return False  # text of a ruled table
        edge = c["top"] < FURNITURE_ZONE * height or c["bottom"] > (1 - FURNITURE_ZONE) * height
        return not (edge and key(c["text"]) in furniture)

    cells = [c for c in cells if keep(c)]
    body = statistics.median([c["size"] for c in cells]) if cells else 0
    blocks = cells + [{"x0": t.bbox[0], "x1": t.bbox[2], "top": t.bbox[1], "bottom": t.bbox[3],
                       "table": t} for t in tables]

    out: list[str] = []
    lines = _lines(_order(blocks))
    i = 0
    while i < len(lines):
        line = lines[i]
        if line[0].get("table") is not None:
            out.append(ruled_tables.rows_to_markdown(line[0]["table"].rows))
            i += 1
            continue
        found = _whitespace_table(lines, i)
        if found:
            rows, i = found
            out.append(ruled_tables.rows_to_markdown(rows))
            continue
        text = " ".join(c["text"] for c in line)
        size = statistics.fmean(c["size"] for c in line)
        if body and size >= body * HEADING_RATIO and len(text) < 120:
            out.append(f"## {text}")
        else:
            out.append(text)
        i += 1

    # blank line around tables and headings; plain text lines stay together
    result: list[str] = []
    for md in out:
        if result and (md.startswith(("|", "## ")) or result[-1].startswith(("|", "## "))):
            result.append("")
        result.append(md)
    return "\n".join(result)


def convert(pdf: Path) -> Conversion:
    """Convert one PDF with pdfplumber (tables + text) and the vision fallback."""
    log("  extracting ruled tables from PDF lines ...")
    t0 = time.time()
    tables = ruled_tables.extract_tables(pdf)
    log(f"  {len(tables)} table(s) in {time.time() - t0:.1f}s")

    by_page: dict[int, list] = {}
    for t in tables:
        by_page.setdefault(t.page_no, []).append(t)
    with pdfplumber.open(str(pdf)) as doc:
        page_cells = [_cells(p.dedupe_chars().extract_words(extra_attrs=["size"])) for p in doc.pages]
        heights = [p.height for p in doc.pages]
    furniture, key = _furniture(page_cells, heights)
    pages = {n: page_markdown(page_cells[n - 1], by_page.get(n, []), heights[n - 1], furniture, key)
             for n in range(1, len(page_cells) + 1)}
    log(f"  text extracted from {len(pages)} page(s); {len(furniture)} repeated header/footer text(s) removed")

    pages, fallback_pages, fallback_model, warnings = vision_ocr.apply_fallback(pdf, pages)
    return Conversion(
        markdown=vision_ocr.join_pages(pages),
        pages=len(pages),
        tables=len(tables),
        ocr_fallback_pages=fallback_pages,
        ocr_fallback_model=fallback_model,
        warnings=warnings,
    )


if __name__ == "__main__":
    run_pipeline(CONVERTER, pkg_version("pdfplumber"), OUTPUT_DIR, convert,
                 "Convert new/changed PDFs in reports/ to Markdown with pdfplumber.")
