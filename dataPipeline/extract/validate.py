"""Consistency checks on one extracted report.

Every failed check is returned as {"check": name, "detail": text}. Any failure
makes the report `needs_review`: the rows are still loaded (nothing is lost) and
the reasons are stored in report_extractions.checks.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .metrics import CORE_ITEMS

BALANCE_TOLERANCE = Decimal("0.005")   # 0.5%
PRIOR_TOLERANCE = Decimal("0.005")
MAX_YOY_FACTOR = Decimal(10)
MIN_CORE_ITEMS = 4
CUMULATIVE_ORDER = ["Q1", "1H", "Q3", "FY"]


def _close(a: Decimal, b: Decimal, tolerance: Decimal, unit: int) -> bool:
    """|a - b| within tolerance of the larger value, or within one rounding unit."""
    return abs(a - b) <= max(tolerance * max(abs(a), abs(b)), Decimal(unit))


def validate(current: dict, prior: dict, *, industry: str, unit_scale: int,
             period_end: date | None, period_type: str | None, period_votes: dict[str, tuple],
             fiscal_year_hint: int | None, stored_prior: dict | None, stored_prior_balance: dict | None,
             siblings: dict[str, Decimal], dropped: list[str]) -> list[dict]:
    """Run all checks.

    current / prior: raw items (Decimal, full units) for the report's period and its comparative column.
    period_votes: {"regex": (end, type), "llm": (end, type)} the sources the period was decided from.
    stored_prior: the `financials` row already stored for the same period one year earlier, if any
        (what the income statement's comparative column shows).
    stored_prior_balance: the row stored for the previous financial year end (what the balance
        sheet's comparative column shows, also in interim reports).
    siblings: {period_type: revenue} of other stored reports for the same company and fiscal year.
    """
    out: list[dict] = []

    def fail(check: str, detail: str) -> None:
        out.append({"check": check, "detail": detail})

    if period_end is None or period_type is None:
        fail("period", "period_end or period_type could not be determined")
    votes = {k: v for k, v in period_votes.items() if v and v[0]}
    if len({v[0] for v in votes.values()}) > 1:
        fail("period", "sources disagree on period_end: " + ", ".join(f"{k}={v[0]}" for k, v in votes.items()))
    types = {v[1] for v in votes.values() if v[1]}
    if len(types) > 1:
        fail("period", "sources disagree on period_type: " + ", ".join(f"{k}={v[1]}" for k, v in votes.items()))
    if period_end and fiscal_year_hint and abs(period_end.year - fiscal_year_hint) > 1:
        fail("period", f"period_end {period_end} does not match the download's fiscal_year {fiscal_year_hint}")

    found = [k for k in CORE_ITEMS if current.get(k) is not None]
    if len(found) < MIN_CORE_ITEMS:
        fail("coverage", f"only {len(found)} of the core items found ({', '.join(found) or 'none'})")
    for d in dropped:
        fail("not_on_page", d)

    assets, equity, liabilities = current.get("total_assets"), current.get("total_equity"), current.get("total_liabilities")
    if assets is not None and equity is not None and liabilities is not None:
        if not _close(assets, equity + liabilities, BALANCE_TOLERANCE, unit_scale):
            fail("balance", f"total_assets {assets} != total_equity {equity} + total_liabilities {liabilities}")
    if assets is not None and assets <= 0:
        fail("sign", f"total_assets is {assets}")

    revenue = current.get("revenue")
    if industry == "general" and revenue is not None:
        if revenue < 0:
            fail("sign", f"revenue is negative ({revenue})")
        ebit = current.get("operating_profit")
        if ebit is not None and revenue > 0 and ebit > revenue:
            fail("plausibility", f"ebit {ebit} is larger than revenue {revenue}")
        ni = current.get("net_income")
        if ni is not None and revenue > 0 and abs(ni) > 2 * revenue:
            fail("plausibility", f"|net_income| {ni} is more than twice revenue {revenue}")

    # comparative columns: income items vs the same period last year, balance items vs last year end
    for stored, pairs in ((stored_prior, {"revenue": "revenue", "net_income": "net_income", "operating_profit": "ebit"}),
                          (stored_prior_balance, {"total_assets": "total_assets", "total_equity": "total_equity"})):
        if not stored:
            continue
        for item, column in pairs.items():
            mine, value = prior.get(item), stored.get(column)
            if mine is not None and value is not None and not _close(mine, Decimal(value), PRIOR_TOLERANCE, unit_scale):
                fail("comparative", f"{item}: comparative column {mine} != stored value {value} for the prior period")
            mine = current.get(item)
            if mine and value and item in ("revenue", "total_assets", "total_equity"):
                ratio = abs(mine / Decimal(value))
                if ratio > MAX_YOY_FACTOR or ratio < 1 / MAX_YOY_FACTOR:
                    fail("yoy", f"{item} changed {ratio:.1f}x against the prior period ({value} -> {mine})")

    if revenue is not None and period_type in CUMULATIVE_ORDER and industry == "general":
        here = CUMULATIVE_ORDER.index(period_type)
        for other, value in siblings.items():
            if other not in CUMULATIVE_ORDER or value is None:
                continue
            there = CUMULATIVE_ORDER.index(other)
            if (there < here and Decimal(value) > revenue) or (there > here and Decimal(value) < revenue):
                fail("cumulative", f"revenue {period_type}={revenue} vs stored {other}={value}: "
                                   "quarterly figures should grow Q1 <= 1H <= Q3 <= FY")
    return out
