"""Find the pages that hold the financial statements, so only those go to the LLM.

A page is a candidate for a statement when it has
  * a numeric table: at least MIN_NUMERIC_ROWS table rows with a number of 3+ digits, and
  * the statement's heading (izkaz poslovnega izida, bilanca stanja, ...) or,
    failing that, at least MIN_LABEL_HITS of the statement's typical row labels.
Table-of-contents pages are rejected: they mention every statement but have no
numeric table. A statement table that continues on the next page brings that
page along when it has a numeric table and no heading of another statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .metrics import ITEMS, SCOPE_KEYWORDS, STATEMENT_KEYWORDS, keyword_pattern, normalise
from .pages import table_rows

MIN_NUMERIC_ROWS = 5
MIN_LABEL_HITS = 3
BIG_NUMBER = re.compile(r"\d[\d.,\s]{2,}\d|\d{3,}")

_HEADINGS = {s: keyword_pattern(k) for s, k in STATEMENT_KEYWORDS.items()}
_LABELS = {
    s: keyword_pattern([label for v in ITEMS.values() if v["statement"] == s for label in v["labels"]])
    for s in ("income", "balance")
}
_SCOPES = {s: keyword_pattern(k) for s, k in SCOPE_KEYWORDS.items()}


@dataclass
class PageInfo:
    page: int
    numeric_rows: int
    heading_hits: dict[str, int] = field(default_factory=dict)  # statement -> hits in headings / first lines
    text_hits: dict[str, int] = field(default_factory=dict)     # statement -> hits anywhere
    label_hits: dict[str, int] = field(default_factory=dict)
    scope: str | None = None                                    # consolidated | separate | None


@dataclass
class Located:
    statement: str
    pages: list[int]
    scope: str | None
    score: float


def numeric_rows(text: str) -> int:
    return sum(1 for row in table_rows(text) if any(BIG_NUMBER.search(c) for c in row[1:] or row))


def page_scope(text_norm: str) -> str | None:
    cons = len(_SCOPES["consolidated"].findall(text_norm))
    sep = len(_SCOPES["separate"].findall(text_norm))
    if cons == sep:
        return None
    return "consolidated" if cons > sep else "separate"


def analyse_page(page: int, text: str) -> PageInfo:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # headings: Markdown headings, or the first 6 lines (titles above a table are often plain text)
    head = normalise("\n".join([ln for ln in lines if ln.lstrip().startswith("#")] + lines[:6]))
    body = normalise(text)
    info = PageInfo(page=page, numeric_rows=numeric_rows(text))
    for statement, pattern in _HEADINGS.items():
        info.heading_hits[statement] = len(pattern.findall(head))
        info.text_hits[statement] = len(pattern.findall(body))
    for statement, pattern in _LABELS.items():
        info.label_hits[statement] = len(set(pattern.findall(body)))
    info.scope = page_scope(head) or page_scope(body)
    return info


def score(info: PageInfo, statement: str) -> float:
    """0 when the page cannot hold this statement, else higher = more likely."""
    if info.numeric_rows < MIN_NUMERIC_ROWS:
        return 0.0
    heading = info.heading_hits.get(statement, 0)
    anywhere = info.text_hits.get(statement, 0)
    labels = info.label_hits.get(statement, 0)
    if not heading and not anywhere and labels < MIN_LABEL_HITS:
        return 0.0
    # a page titled with another statement is not this one (e.g. "bilanca" in a P&L note)
    other = max((v for s, v in info.heading_hits.items() if s not in (statement, "highlights")), default=0)
    if other > heading and labels < MIN_LABEL_HITS:
        return 0.0
    return 3.0 * min(heading, 2) + 1.0 * min(anywhere, 3) + 0.5 * labels + 0.05 * min(info.numeric_rows, 40)


def locate(pages: dict[int, str]) -> tuple[dict[str, Located], str | None, list[PageInfo]]:
    """Best pages per statement for one report.

    Returns ({statement: Located}, scope, page infos). The scope is consolidated
    whenever the report has consolidated income statement or balance sheet pages
    (decision: group figures win), else separate, else None (no group: the report
    does not say). Missing statements are absent from the dict.
    """
    infos = {n: analyse_page(n, t) for n, t in sorted(pages.items())}
    candidates: dict[str, list[tuple[float, PageInfo]]] = {}
    for statement in STATEMENT_KEYWORDS:
        scored = [(score(i, statement), i) for i in infos.values()]
        candidates[statement] = sorted([c for c in scored if c[0] > 0], key=lambda c: (-c[0], c[1].page))

    main = candidates["income"] + candidates["balance"]
    scopes = {i.scope for _, i in main}
    scope = "consolidated" if "consolidated" in scopes else ("separate" if "separate" in scopes else None)

    found: dict[str, Located] = {}
    for statement, cands in candidates.items():
        # same scope first; pages that do not say their scope are acceptable too
        preferred = [c for c in cands if c[1].scope == scope] or [c for c in cands if c[1].scope is None]
        if statement == "highlights":
            preferred = cands  # key-figure pages often mix group and company
        if not preferred:
            continue
        best_score, best = preferred[0]
        chosen = [best.page]
        nxt = infos.get(best.page + 1)
        if (nxt and statement != "highlights" and nxt.numeric_rows >= MIN_NUMERIC_ROWS
                and nxt.scope in (None, best.scope)  # not the other scope's copy of the same statement
                and not any(v for s, v in nxt.heading_hits.items() if s not in (statement, "highlights"))):
            chosen.append(nxt.page)
        found[statement] = Located(statement, chosen, scope if statement != "highlights" else best.scope,
                                   best_score)
    return found, scope, list(infos.values())
