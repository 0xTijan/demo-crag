"""Split converted Markdown into pages, and read the cells of its tables."""
from __future__ import annotations

import re

PAGE_MARKER = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
SEPARATOR_ROW = re.compile(r"^\|?[\s:\-|]+\|?$")  # | --- | :---: |


def split_pages(markdown: str) -> dict[int, str]:
    """{page_no: text} from the `<!-- page: N -->` markers written by convert/.

    Text before the first marker is dropped (convert/ never writes any). A file
    without markers is treated as one page, so hand-made Markdown still works.
    """
    parts = PAGE_MARKER.split(markdown)
    if len(parts) == 1:
        return {1: markdown}
    pages: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        no = int(parts[i])
        pages[no] = (pages.get(no, "") + "\n" + parts[i + 1]).strip()
    return pages


def table_rows(text: str) -> list[list[str]]:
    """Every Markdown table row on a page as a list of cell strings (separator rows skipped)."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or SEPARATOR_ROW.match(line):
            continue
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def join_pages(pages: dict[int, str], numbers: list[int]) -> str:
    """The given pages with their markers, for the prompt (the model cites these numbers)."""
    return "\n\n".join(f"<!-- page: {n} -->\n{pages[n]}" for n in numbers if n in pages)
