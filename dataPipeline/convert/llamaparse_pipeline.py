"""PDF -> Markdown with LlamaParse (LlamaCloud API).

Usage:  LLAMA_CLOUD_API_KEY=llx-... python dataPipeline/convert/llamaparse_pipeline.py [--force]

Same bookkeeping as docling.py (hashing, DB tracking, retries; see common.py),
but each PDF is uploaded to LlamaParse and parsed in the cloud. Note: the PDF
leaves your machine and parsing costs LlamaCloud credits.

Tables are requested as HTML (not Markdown) because HTML keeps merged cells
(rowspan/colspan); html_tables_to_markdown() then turns them into Markdown with
the same rules as the other pipelines: a merged cell's text is repeated in every
row/column it covers, and a table nested inside a cell is folded into that cell.

Settings ("llamaParse" in config.json): tier ("fast" | "cost_effective" |
"agentic" | "agentic_plus", default "agentic") and version (default "latest").
Output goes to markdowns_llamaparse/ with a `<!-- page: N -->` marker per page.
"""
import os
import re
import sys
import time
from html.parser import HTMLParser
from importlib.metadata import version as pkg_version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root importable

from dataPipeline.convert import vision_ocr  # noqa: E402
from dataPipeline.convert.common import ROOT, Conversion, log, run_pipeline  # noqa: E402
from dataPipeline.convert.ruled_tables import rows_to_markdown  # noqa: E402
from helpers.config import read_config  # noqa: E402

CONVERTER = "llamaparse"
OUTPUT_DIR = ROOT / "markdowns_llamaparse"
TABLE_TAG = re.compile(r"<(/?)table\b[^>]*>", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# HTML table -> filled grid -> Markdown
# --------------------------------------------------------------------------- #

class _TableParser(HTMLParser):
    """Parse one <table> (with nested tables) into rows of (text, rowspan, colspan).

    A nested <table> inside a cell is parsed recursively and folded into the
    parent cell's text with fold_nested().
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # one entry per open table: {"rows": [...], "row": [...] | None, "cell": {...} | None}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.stack.append({"rows": [], "row": None, "cell": None})
        elif not self.stack:
            return
        elif tag == "tr":
            self.stack[-1]["row"] = []
        elif tag in ("td", "th"):
            if self.stack[-1]["row"] is None:  # cell without <tr>
                self.stack[-1]["row"] = []
            self.stack[-1]["cell"] = {"text": [], "rowspan": int(attrs.get("rowspan") or 1),
                                      "colspan": int(attrs.get("colspan") or 1)}
        elif tag == "br" and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"]["text"].append(" ")

    def handle_endtag(self, tag):
        if not self.stack:
            return
        top = self.stack[-1]
        if tag in ("td", "th") and top["cell"] is not None:
            c = top["cell"]
            top["row"].append((" ".join("".join(c["text"]).split()), c["rowspan"], c["colspan"]))
            top["cell"] = None
        elif tag == "tr" and top["row"] is not None:
            top["rows"].append(top["row"])
            top["row"] = None
        elif tag == "table":
            if top["row"]:  # unclosed last row
                top["rows"].append(top["row"])
            done = self.stack.pop()
            grid = fill_spans(done["rows"])
            if self.stack and self.stack[-1]["cell"] is not None:
                self.stack[-1]["cell"]["text"].append(" " + fold_nested(grid) + " ")
            else:
                self.result = grid

    def handle_data(self, data):
        if self.stack and self.stack[-1]["cell"] is not None:
            self.stack[-1]["cell"]["text"].append(data)


def fill_spans(rows) -> list[list[str]]:
    """Expand (text, rowspan, colspan) rows into a full grid, repeating merged text."""
    grid: dict[tuple[int, int], str] = {}
    for r, row in enumerate(rows):
        c = 0
        for text, rs, cs in row:
            while (r, c) in grid:  # skip positions taken by a rowspan from above
                c += 1
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = text
            c += cs
    if not grid:
        return []
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    return [[grid.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]


def fold_nested(grid) -> str:
    """A nested table as text: one line per row, 'header: value' pairs."""
    if not grid:
        return ""
    header, lines = grid[0], []
    for row in grid[1:]:
        pairs = [f"{h}: {v}" if h else v for h, v in zip(header, row) if v]
        lines.append(", ".join(pairs))
    return "<br>".join(lines) if lines else " | ".join(header)


def html_tables_to_markdown(markdown: str) -> tuple[str, int]:
    """Replace every outermost <table>...</table> with a Markdown table.

    Returns (new text, number of tables converted).
    """
    out, depth, start, pos, count = [], 0, None, 0, 0
    for m in TABLE_TAG.finditer(markdown):
        closing = m.group(1) == "/"
        if not closing:
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0 and start is not None:
                parser = _TableParser()
                parser.result = []
                parser.feed(markdown[start:m.end()])
                rows = parser.result
                out.append(markdown[pos:start])
                out.append("\n" + rows_to_markdown(rows) + "\n" if rows else "")
                count += 1 if rows else 0
                pos = m.end()
                start = None
    out.append(markdown[pos:])
    return "".join(out), count


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

_client = None


def settings() -> tuple[str, str]:
    cfg = read_config().get("llamaParse") or {}
    return cfg.get("tier", "agentic"), cfg.get("version", "latest")


def convert(pdf: Path) -> Conversion:
    """Upload one PDF to LlamaParse, wait for the result, convert its tables."""
    global _client
    from llama_cloud import LlamaCloud

    if _client is None:
        _client = LlamaCloud()  # reads LLAMA_CLOUD_API_KEY (and optional LLAMA_CLOUD_BASE_URL)
    tier, version = settings()
    log(f"  uploading to LlamaParse (tier {tier}, version {version}) ...")
    t0 = time.time()
    with pdf.open("rb") as f:
        result = _client.parsing.parse(
            tier=tier,
            version=version,
            upload_file=(pdf.name, f, "application/pdf"),
            expand=["markdown"],
            # HTML tables keep merged cells; they are converted to Markdown below
            output_options={"markdown": {"tables": {"output_tables_as_markdown": False}}},
            client_name="demo-crag",
            timeout=1800,
        )
    if result.job.status != "COMPLETED":
        raise RuntimeError(f"LlamaParse job {result.job.id} ended {result.job.status}: {result.job.error_message}")
    log(f"  LlamaParse done in {time.time() - t0:.1f}s (job {result.job.id})")

    pages, failed, table_count = {}, [], 0
    for page in result.markdown.pages if result.markdown else []:
        if not page.success:
            failed.append(page.page_number)
            pages[page.page_number] = ""
            continue
        pages[page.page_number], n_tables = html_tables_to_markdown(page.markdown)
        table_count += n_tables
    if failed:
        log(f"  LlamaParse could not parse page(s) {failed}")

    warnings = [("llamaparse_page", RuntimeError(f"LlamaParse failed on pages {failed}"))] if failed else []
    pages, fallback_pages, fallback_model, fb_warnings = vision_ocr.apply_fallback(pdf, pages)
    return Conversion(
        markdown=vision_ocr.join_pages(pages),
        pages=len(pages),
        tables=table_count,
        ocr_fallback_pages=fallback_pages,
        ocr_fallback_model=fallback_model,
        warnings=warnings + fb_warnings,
    )


if __name__ == "__main__":
    if not os.getenv("LLAMA_CLOUD_API_KEY"):
        sys.exit("Set LLAMA_CLOUD_API_KEY (your LlamaCloud API key) to use this pipeline.")
    tier, _ = settings()
    run_pipeline(CONVERTER, f"llama-cloud {pkg_version('llama-cloud')} / tier {tier}", OUTPUT_DIR, convert,
                 "Convert new/changed PDFs in reports/ to Markdown with LlamaParse.")
