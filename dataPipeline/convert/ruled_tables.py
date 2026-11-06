"""Table extraction from a PDF's own vector lines and text layer (pdfplumber).

Why this exists
---------------
Docling's layout model sometimes crops a table (losing columns), and its table
model misreads merged cells. For digital PDFs the tables are usually drawn with
real lines, and the exact text is in the text layer, so we can rebuild every
ruled table geometrically instead of guessing it from an image.

What it handles
---------------
* Grid tables (vertical + horizontal lines). Merged cells are detected from the
  geometry and their text is repeated in every row/column they cover.
* Nested tables inside a cell (e.g. a "Step / Temperature / Time" table inside a
  "Test Method" cell): flattened into the parent cell, one line per nested row.
* Rows of framed boxes that together form a table (e.g. a part-number legend).
* "Form" tables drawn with horizontal rules only (key/value blocks).

What it ignores (left to Docling / OCR)
---------------------------------------
Scanned pages (no text layer) and tables drawn without lines.

Public API
----------
extract_tables(pdf_path) -> list[ExtractedTable]
rows_to_markdown(rows) -> str
apply_to_docling_document(doc, tables) -> int   (only imports docling_core when called)
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import pdfplumber
from pdfplumber.utils import extract_text as chars_to_text

# pdfplumber line-detection settings. Snapping is page-wide (all vertical lines
# within the tolerance get the same x, even far apart on the page), so it is
# kept small: it only merges double lines. Joining and intersection are local
# and bridge the hairline gaps between line segments.
FINDER_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 1,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
    "edge_min_length": 3,
}


@dataclass
class ExtractedTable:
    page_no: int                                  # 1-based
    bbox: tuple[float, float, float, float]       # x0, top, x1, bottom (top-left origin, PDF points)
    rows: list[list[str]]                         # filled grid; rows[0] is the header row
    source: str                                   # "grid" | "boxes" | "rules"


# --------------------------------------------------------------------------- #
# Graphics filtering
# --------------------------------------------------------------------------- #

def _is_white(color) -> bool:
    """True for white in gray (1), RGB (1,1,1) or CMYK (0,0,0,0)."""
    if color is None:
        return False
    vals = list(color) if isinstance(color, (list, tuple)) else [color]
    try:
        vals = [float(v) for v in vals]
    except (TypeError, ValueError):  # pattern / named colours: treat as visible
        return False
    if len(vals) in (1, 3):
        return all(v >= 0.95 for v in vals)
    if len(vals) == 4:
        return all(v <= 0.05 for v in vals)
    return False


def _area(o) -> float:
    return max(0.0, o["x1"] - o["x0"]) * max(0.0, o["bottom"] - o["top"])


def _visible_graphics_page(page):
    """Return a filtered page without graphics that cannot be seen.

    Removed: white/invisible frames (common behind drawings, they otherwise
    create fake cells over real tables) and filled rectangles that sit inside a
    bigger filled rectangle of the same colour (their edges are seamless, e.g.
    one rectangle per text line inside a coloured header box).
    """
    # only real area fills (not hairline rectangles used to draw table lines)
    fills = [r for r in page.rects
             if r.get("fill") and not r.get("stroke")
             and r["x1"] - r["x0"] > 2 and r["bottom"] - r["top"] > 2]
    seamless = set()
    for r in fills:
        for p in fills:
            if p is r or _area(p) <= _area(r):
                continue
            if (p["x0"] - 0.5 <= r["x0"] and r["x1"] <= p["x1"] + 0.5
                    and p["top"] - 0.5 <= r["top"] and r["bottom"] <= p["bottom"] + 0.5
                    and p.get("non_stroking_color") == r.get("non_stroking_color")):
                seamless.add(id(r))
                break

    def keep(obj) -> bool:
        kind = obj.get("object_type")
        if kind not in ("rect", "line", "curve"):
            return True  # text etc. is never filtered here
        if id(obj) in seamless:
            return False
        stroked = obj.get("stroke", kind == "line") and not _is_white(obj.get("stroking_color"))
        filled = obj.get("fill") and not _is_white(obj.get("non_stroking_color"))
        return bool(stroked or filled)

    return page.filter(keep)


# --------------------------------------------------------------------------- #
# Cells -> groups -> grid
# --------------------------------------------------------------------------- #

def _group_cells(cells):
    """Group cells that share a corner (like pdfplumber, but keeps 1-cell boxes)."""
    def corners(c):
        x0, t, x1, b = (round(v) for v in c)
        return {(x0, t), (x0, b), (x1, t), (x1, b)}

    groups: list[tuple[set, list]] = []
    for c in sorted(cells, key=lambda c: (c[1], c[0])):
        cc = corners(c)
        hits = [g for g in groups if g[0] & cc]
        if not hits:
            groups.append((set(cc), [c]))
            continue
        base = hits[0]
        base[0].update(cc)
        base[1].append(c)
        for other in hits[1:]:  # merge groups bridged by this cell
            base[0].update(other[0])
            base[1].extend(other[1])
            groups.remove(other)
    return [g[1] for g in groups]


def _merge_boundaries(values, centers, tol=1.0):
    """Collapse grid lines that do not separate any text.

    ``values`` are candidate x (or y) boundaries from cell edges, ``centers`` the
    x (or y) centres of every character in the table. Two neighbouring lines with
    no character between them bound an empty sliver (a gutter, a double line, an
    inset header fill) and are merged into one boundary.
    """
    vals = sorted(values)
    snapped: list[float] = []
    for v in vals:
        if not snapped or v - snapped[-1] > tol:
            snapped.append(v)
    runs: list[list[float]] = [[snapped[0]]]
    for a, b in zip(snapped, snapped[1:]):
        if any(a + 0.5 < c < b - 0.5 for c in centers):
            runs.append([b])
        else:
            runs[-1].append(b)
    reps = [statistics.fmean(r) for r in runs]
    reps[0], reps[-1] = runs[0][0], runs[-1][-1]  # keep the outer table edges
    return reps


def _nearest(reps, v) -> int:
    return min(range(len(reps)), key=lambda i: abs(reps[i] - v))


def _cell_text(chars) -> str:
    if not chars:
        return ""
    text = chars_to_text(chars)
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _build_grid(cells, chars):
    """Turn cell rectangles into a grid of cell ids plus the text of each id."""
    cx = [(c["x0"] + c["x1"]) / 2 for c in chars]
    cy = [(c["top"] + c["bottom"]) / 2 for c in chars]
    xs = _merge_boundaries([v for c in cells for v in (c[0], c[2])], cx)
    ys = _merge_boundaries([v for c in cells for v in (c[1], c[3])], cy)
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    if n_rows < 1 or n_cols < 1:
        return None, {}

    grid = [[None] * n_cols for _ in range(n_rows)]
    texts: dict[int, str] = {}
    area_of: dict[int, float] = {}
    for idx, (x0, t, x1, b) in enumerate(cells):
        c0, c1 = _nearest(xs, x0), _nearest(xs, x1)
        r0, r1 = _nearest(ys, t), _nearest(ys, b)
        if c1 <= c0 or r1 <= r0:
            continue  # sliver cell that vanished when gutters were merged
        inside = [ch for ch in chars
                  if x0 <= (ch["x0"] + ch["x1"]) / 2 <= x1 and t <= (ch["top"] + ch["bottom"]) / 2 <= b]
        texts[idx] = _cell_text(inside)
        area_of[idx] = (x1 - x0) * (b - t)
        for r in range(r0, r1):
            for c in range(c0, c1):
                cur = grid[r][c]
                if cur is None or area_of[idx] < area_of[cur]:  # most specific cell wins
                    grid[r][c] = idx

    # drop rows / columns that contain no text at all
    def row_has_text(row):
        return any(i is not None and texts.get(i) for i in row)

    keep_rows = [r for r in range(n_rows) if row_has_text(grid[r])]
    grid = [grid[r] for r in keep_rows]
    if not grid:
        return None, {}
    keep_cols = [c for c in range(n_cols) if any(row[c] is not None and texts.get(row[c]) for row in grid)]
    grid = [[row[c] for c in keep_cols] for row in grid]
    # bbox of the rebuilt grid (used to replace / remove overlapping Docling items)
    return grid, texts


def _flatten_nested(grid, texts):
    """Fold tables nested inside a cell into that cell.

    A header cell that spans several grid columns is a column *group*. Rows in
    which that group is split into several cells are a nested table. The nested
    rows (plus the rows around them that belong to the same outer row) are
    collapsed into one outer row, and the nested cells become text lines like
    "Step: 1, Temperature (℃): -25±3, Time (min): 30".
    """
    if len(grid) < 3:
        return grid
    n_cols = len(grid[0])
    groups, c = [], 0
    while c < n_cols:
        hid, c1 = grid[0][c], c
        while hid is not None and c1 + 1 < n_cols and grid[0][c1 + 1] == hid:
            c1 += 1
        if c1 > c:
            groups.append((c, c1 + 1))
        c = c1 + 1

    for g0, g1 in reversed(groups):  # right to left so column indices stay valid
        split_rows = [r for r in range(1, len(grid)) if len(set(grid[r][g0:g1])) > 1]
        if not split_rows:
            continue  # ordinary column span (e.g. a group header), keep as is
        blocks, start = [], split_rows[0]
        for prev, cur in zip(split_rows, split_rows[1:] + [None]):
            if cur != prev + 1:
                blocks.append((start, prev))
                start = cur
        others = [c for c in range(len(grid[0])) if not g0 <= c < g1]
        for s, e in reversed(blocks):
            # the outer row = smallest cell outside the group that spans the block
            band = None
            for c in others:
                cid = grid[s][c]
                if cid is None:
                    continue
                rows_c = [r for r in range(len(grid)) if grid[r][c] == cid]
                a, b = min(rows_c), max(rows_c)
                if a <= s and b >= e and (band is None or b - a < band[1] - band[0]):
                    band = (a, b)
            if band is None:
                continue
            a, b = band
            if any(len({grid[r][c] for r in range(a, b + 1)}) > 1 for c in others):
                continue  # other columns change inside the band: not a nested table
            parts, header, prev_id = [], None, object()
            for r in range(a, b + 1):
                sub = grid[r][g0:g1]
                ids = list(dict.fromkeys(sub))
                if len(ids) == 1:
                    if ids[0] != prev_id and texts.get(ids[0]):
                        parts.append(texts[ids[0]])
                    prev_id, header = ids[0], None
                    continue
                values = [(sub.index(i), texts.get(i, "")) for i in ids]
                if header is None:  # first split row = nested header
                    header = {pos: txt for pos, txt in values}
                else:
                    parts.append(", ".join(
                        f"{header.get(pos, '')}: {txt}" if header.get(pos) else txt
                        for pos, txt in values))
                prev_id = None
            new_id = max(texts) + 1
            texts[new_id] = "<br>".join(parts)
            new_row = list(grid[a])
            for c in range(g0, g1):
                new_row[c] = new_id
            grid = grid[:a] + [new_row] + grid[b + 1:]
        if all(len(set(row[g0:g1])) == 1 for row in grid):  # group no longer split anywhere
            grid = [row[:g0 + 1] + row[g1:] for row in grid]
    return grid


def _fill(grid, texts) -> list[list[str]]:
    """Expand ids to text: a merged cell's text appears in every position it covers."""
    return [[texts.get(i, "") if i is not None else "" for i in row] for row in grid]


# --------------------------------------------------------------------------- #
# Special layouts
# --------------------------------------------------------------------------- #

def _box_tables(boxes, page_no):
    """Combine >=3 side-by-side stacks of framed boxes into one table.

    ``boxes`` are single-column groups: (bbox, [(top, text), ...]). Boxes that are
    vertically stacked and horizontally overlapping form one column; the columns
    must be side by side at the same height. Rows are aligned by their top edge.
    """
    stacks: list[list] = []
    for box in sorted(boxes, key=lambda b: (b[0][1], b[0][0])):
        (x0, t, x1, b), _ = box
        for st in stacks:
            sx0, st_, sx1, sb = st[0]
            overlap = min(x1, sx1) - max(x0, sx0)
            if overlap >= 0.5 * min(x1 - x0, sx1 - sx0) and 0 <= t - sb <= 20:
                st[0] = (min(x0, sx0), st_, max(x1, sx1), b)
                st[1].append(box)
                break
        else:
            stacks.append([(x0, t, x1, b), [box]])

    tables, used = [], set()
    for i, st in enumerate(stacks):
        if i in used:
            continue
        row = [j for j, o in enumerate(stacks)
               if j not in used and abs(o[0][1] - st[0][1]) <= 4 and abs(o[0][3] - st[0][3]) <= 45]
        if len(row) < 3:
            continue
        row.sort(key=lambda j: stacks[j][0][0])
        used.update(row)
        cols = [[cell for box in stacks[j][1] for cell in box[1]] for j in row]
        tops = sorted({round(t) for col in cols for t, _ in col})
        bands: list[int] = []
        for t in tops:
            if not bands or t - bands[-1] > 4:
                bands.append(t)
        rows = [[""] * len(cols) for _ in bands]
        for c, col in enumerate(cols):
            for t, txt in col:
                r = min(range(len(bands)), key=lambda k: abs(bands[k] - t))
                rows[r][c] = (rows[r][c] + " " + txt).strip()
        bbox = (min(stacks[j][0][0] for j in row), min(stacks[j][0][1] for j in row),
                max(stacks[j][0][2] for j in row), max(stacks[j][0][3] for j in row))
        tables.append(ExtractedTable(page_no, bbox, rows, "boxes"))
    return tables


def _rule_tables(page, taken, page_no):
    """Key/value blocks drawn with horizontal rules only (no vertical lines).

    Rules with the same left/right extent and regular spacing form a table.
    Each band between rules is a row, plus the band just above the first rule
    when it contains text. Columns come from the points where every rule is
    broken into segments (the column split), else from a large text gap.
    """
    h = [e for e in page.edges if e["orientation"] == "h"]
    by_y: list[list] = []
    for e in sorted(h, key=lambda e: e["top"]):
        if by_y and abs(e["top"] - by_y[-1][0]) <= 1.5:
            by_y[-1][1].append(e)
        else:
            by_y.append([e["top"], [e]])

    rules = []
    for y, segs in by_y:
        segs.sort(key=lambda e: e["x0"])
        x0, x1, breaks = segs[0]["x0"], segs[0]["x1"], []
        for s in segs[1:]:
            if s["x0"] - x1 <= 3:
                if s["x0"] > x0 + 1 and abs(s["x0"] - x1) <= 3 and s["x1"] > x1 + 0.1:
                    breaks.append(s["x0"])
                x1 = max(x1, s["x1"])
            else:
                if x1 - x0 >= 40:
                    rules.append((y, x0, x1, breaks))
                x0, x1, breaks = s["x0"], s["x1"], []
        if x1 - x0 >= 40:
            rules.append((y, x0, x1, breaks))

    def inside_taken(y, x0, x1):
        return any(tx0 - 3 <= x0 and x1 <= tx1 + 3 and tt - 3 <= y <= tb + 3 for tx0, tt, tx1, tb in taken)

    rules = [r for r in rules if not inside_taken(*r[:3])]
    tables, chain = [], []
    for r in rules + [None]:
        if r is not None and chain:
            last = chain[-1]
            gap = r[0] - last[0]
            same_extent = abs(r[1] - last[1]) <= 6 and abs(r[2] - last[2]) <= 6
            if same_extent and 8 <= gap <= 80 and (len(chain) < 2 or abs(gap - (chain[-1][0] - chain[-2][0])) <= 0.4 * gap):
                chain.append(r)
                continue
        if len(chain) >= 3:
            t = _rules_to_table(page, chain, page_no)
            if t:
                tables.append(t)
        chain = [r] if r is not None else []
    return tables


def _rules_to_table(page, chain, page_no):
    ys = [r[0] for r in chain]
    x0 = min(r[1] for r in chain)
    x1 = max(r[2] for r in chain)
    spacing = statistics.median(b - a for a, b in zip(ys, ys[1:]))
    all_breaks = sorted((b, i) for i, r in enumerate(chain) for b in r[3])
    splits: list[list[tuple[float, int]]] = []
    for b, i in all_breaks:
        if splits and abs(b - splits[-1][0][0]) <= 3:
            splits[-1].append((b, i))
        else:
            splits.append([(b, i)])
    # a column split must break (almost) every rule; count each rule once
    cols = [statistics.fmean(b for b, _ in s) for s in splits
            if len({i for _, i in s}) >= 0.6 * len(chain)]
    bounds = [x0] + cols + [x1]
    bands = [(ys[0] - spacing, ys[0])] + list(zip(ys, ys[1:]))
    chars = page.chars
    rows = []
    for top, bottom in bands:
        row = []
        for a, b in zip(bounds, bounds[1:]):
            inside = [c for c in chars if a <= (c["x0"] + c["x1"]) / 2 < b and top < (c["top"] + c["bottom"]) / 2 <= bottom]
            row.append(_cell_text(inside))
        rows.append(row)
    if rows and not any(rows[0]):
        rows = rows[1:]
        top_y = ys[0]
    else:
        top_y = ys[0] - spacing
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    return ExtractedTable(page_no, (x0, top_y, x1, ys[-1]), rows, "rules")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def extract_tables(pdf_path) -> list[ExtractedTable]:
    """All ruled tables in the PDF that have text in their text layer."""
    out: list[ExtractedTable] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, raw_page in enumerate(pdf.pages, 1):
            page = _visible_graphics_page(raw_page.dedupe_chars())
            if not page.chars:
                continue  # scanned page: nothing to read, Docling OCR handles it
            finder = page.debug_tablefinder(FINDER_SETTINGS)
            taken, boxes = [], []
            for cells in _group_cells(finder.cells):
                gx0 = min(c[0] for c in cells); gt = min(c[1] for c in cells)
                gx1 = max(c[2] for c in cells); gb = max(c[3] for c in cells)
                chars = [ch for ch in page.chars
                         if gx0 <= (ch["x0"] + ch["x1"]) / 2 <= gx1 and gt <= (ch["top"] + ch["bottom"]) / 2 <= gb]
                if not chars:
                    continue  # drawing / decoration
                grid, texts = _build_grid(cells, chars)
                if grid is None:
                    continue
                if len(grid[0]) == 1:  # single column: a framed box, maybe part of a box row
                    col = []
                    for r, row in enumerate(grid):
                        if r and row[0] == grid[r - 1][0]:
                            continue
                        cell = cells[row[0]] if row[0] is not None else None
                        if cell is not None and texts.get(row[0]):
                            col.append((cell[1], texts[row[0]]))
                    if col:
                        boxes.append(((gx0, gt, gx1, gb), col))
                    continue
                grid = _flatten_nested(grid, texts)
                rows = _fill(grid, texts)
                if sum(1 for row in rows for v in row if v) < 2:
                    continue
                out.append(ExtractedTable(page_no, (gx0, gt, gx1, gb), rows, "grid"))
                taken.append((gx0, gt, gx1, gb))
            for t in _box_tables(boxes, page_no):
                out.append(t)
                taken.append(t.bbox)
            out.extend(_rule_tables(page, taken, page_no))
    return out


def rows_to_markdown(rows: list[list[str]]) -> str:
    """Render a filled grid as a GitHub Markdown table (first row = header)."""
    def esc(v: str) -> str:
        return v.replace("|", "\\|").replace("\n", " ")
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(esc(v) for v in rows[0]) + " |",
             "|" + "---|" * width]
    lines += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _overlap_fraction(a, b) -> float:
    """Share of rectangle a (x0, top, x1, bottom) that lies inside rectangle b."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return (w * h) / area


def apply_to_docling_document(doc, tables: list[ExtractedTable]) -> int:
    """Put the ruled tables into a Docling document in place of Docling's own.

    For each extracted table: a Docling table that overlaps it gets the new cell
    data; other overlapping Docling tables and every text item whose centre lies
    inside the table area (loose cell text, headings Docling made from cells) are
    deleted. If Docling found no table there, the table is inserted before the
    first item below it on the page. Returns the number of tables applied.
    """
    from docling_core.types.doc import (BoundingBox, CoordOrigin, ProvenanceItem,
                                        TableCell, TableData, TableItem)

    def data_for(rows):
        cells = [TableCell(text=v, start_row_offset_idx=r, end_row_offset_idx=r + 1,
                           start_col_offset_idx=c, end_col_offset_idx=c + 1,
                           column_header=(r == 0))
                 for r, row in enumerate(rows) for c, v in enumerate(row)]
        return TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells)

    def item_box(item, page_no, height):
        for prov in getattr(item, "prov", None) or []:
            if prov.page_no == page_no:
                b = prov.bbox.to_top_left_origin(height)
                return (b.l, b.t, b.r, b.b)
        return None

    applied = 0
    for t in tables:
        page = doc.pages.get(t.page_no)
        if page is None:
            continue
        height = page.size.height
        region = (t.bbox[0] - 2, t.bbox[1] - 2, t.bbox[2] + 2, t.bbox[3] + 2)
        on_page = [(item, item_box(item, t.page_no, height))
                   for item, _ in doc.iterate_items(traverse_pictures=True)]
        on_page = [(i, b) for i, b in on_page if b is not None]
        tables_hit = [i for i, b in on_page if isinstance(i, TableItem) and _overlap_fraction(b, region) > 0.3]
        texts_hit = [i for i, b in on_page
                     if not isinstance(i, TableItem) and hasattr(i, "text")
                     and region[0] <= (b[0] + b[2]) / 2 <= region[2] and region[1] <= (b[1] + b[3]) / 2 <= region[3]]
        prov = ProvenanceItem(
            page_no=t.page_no,
            bbox=BoundingBox(l=t.bbox[0], t=t.bbox[1], r=t.bbox[2], b=t.bbox[3],
                             coord_origin=CoordOrigin.TOPLEFT).to_bottom_left_origin(height),
            charspan=(0, 0),
        )
        data = data_for(t.rows)
        if tables_hit:
            keep = tables_hit[0]
            keep.data = data
            keep.prov = [prov]
            remove = tables_hit[1:] + texts_hit
            if remove:
                doc.delete_items(node_items=remove)
        else:
            if texts_hit:
                doc.delete_items(node_items=texts_hit)
            remaining = [(i, item_box(i, t.page_no, height)) for i, _ in doc.iterate_items(traverse_pictures=False)]
            remaining = [(i, b) for i, b in remaining if b is not None]
            below = [i for i, b in remaining if b[1] >= t.bbox[1] - 1]
            if below:
                doc.insert_table(sibling=below[0], data=data, prov=prov, after=False)
            elif remaining:
                doc.insert_table(sibling=remaining[-1][0], data=data, prov=prov, after=True)
            else:
                doc.add_table(data=data, prov=prov)
        applied += 1
    return applied
