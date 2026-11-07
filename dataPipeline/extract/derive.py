"""Compute every `financials` / `valuation_ratios` column from the raw line items.

Ratios and sums are never asked from the LLM. Input values are Decimals in full
currency units (already multiplied by the table unit). Every value is fitted to
its column's NUMERIC(p, s): one that does not fit is left NULL and reported.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# column -> (precision, scale) from schema.sql
FINANCIALS = {
    "revenue": (20, 2), "ebitda": (20, 2), "ebit": (20, 2), "net_income": (20, 2),
    "earnings_per_share": (20, 4), "total_assets": (20, 2), "total_equity": (20, 2),
    "roe": (10, 4), "roa": (10, 4), "liabilities_equity_ratio": (10, 4),
    "net_working_capital": (20, 2), "net_liabilities": (20, 2),
}
VALUATIONS = {
    "p_e": (10, 4), "p_b": (10, 4), "p_s": (10, 4), "ev_s": (10, 4), "net_debt_ebitda": (10, 4),
    "dividend_per_share": (10, 4), "dividend_yield": (10, 4),
}
HUNDRED = Decimal(100)


def fit(value: Decimal | None, precision: int, scale: int) -> Decimal | None:
    """Round to `scale` decimals; None if it does not fit NUMERIC(precision, scale)."""
    if value is None:
        return None
    q = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
    if abs(q) >= Decimal(10) ** (precision - scale):
        raise OverflowError
    return q


def _div(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _sum(*values: Decimal | None) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


def _fit_all(values: dict, columns: dict, notes: list[str]) -> dict:
    out = {}
    for col, (p, s) in columns.items():
        try:
            out[col] = fit(values.get(col), p, s)
        except OverflowError:
            out[col] = None
            notes.append(f"{col} = {values.get(col)} does not fit NUMERIC({p},{s}), left NULL")
    return out


def financials(v: dict[str, Decimal | None], shares: int | None) -> tuple[dict, list[str]]:
    """Raw items -> `financials` columns. Returns (columns, notes about derived values).

    ebitda: printed EBITDA, else EBIT + |D&A| (D&A is printed as a cost, either sign).
    roe / roa: net income / equity or assets x 100, for the period (not annualised).
    total_liabilities: printed, else total assets - total equity.
    net_liabilities: financial debt (non-current + current) - cash, i.e. net debt.
    earnings_per_share: printed, else net income / shares outstanding (stocks.quantity).
    """
    notes: list[str] = []
    ebitda = v.get("ebitda")
    if ebitda is None and v.get("operating_profit") is not None and v.get("depreciation_amortisation") is not None:
        ebitda = v["operating_profit"] + abs(v["depreciation_amortisation"])
        notes.append("ebitda = ebit + depreciation_amortisation")
    liabilities = v.get("total_liabilities")
    if liabilities is None and v.get("total_assets") is not None and v.get("total_equity") is not None:
        liabilities = v["total_assets"] - v["total_equity"]
        notes.append("total_liabilities = total_assets - total_equity")
    debt = _sum(v.get("financial_liabilities"), v.get("financial_liabilities_current"))
    net_liabilities = debt - v["cash"] if debt is not None and v.get("cash") is not None else None
    eps = v.get("eps")
    if eps is None and v.get("net_income") is not None and shares:
        eps = v["net_income"] / Decimal(shares)
        notes.append("earnings_per_share = net_income / stocks.quantity")
    nwc = (v["current_assets"] - v["current_liabilities"]
           if v.get("current_assets") is not None and v.get("current_liabilities") is not None else None)
    roe = _div(v.get("net_income"), v.get("total_equity"))
    roa = _div(v.get("net_income"), v.get("total_assets"))
    values = {
        "revenue": v.get("revenue"),
        "ebitda": ebitda,
        "ebit": v.get("operating_profit"),
        "net_income": v.get("net_income"),
        "earnings_per_share": eps,
        "total_assets": v.get("total_assets"),
        "total_equity": v.get("total_equity"),
        "roe": roe * HUNDRED if roe is not None else None,
        "roa": roa * HUNDRED if roa is not None else None,
        "liabilities_equity_ratio": _div(liabilities, v.get("total_equity")),
        "net_working_capital": nwc,
        "net_liabilities": net_liabilities,
    }
    return _fit_all(values, FINANCIALS, notes), notes


def valuations(fin: dict, price: Decimal | None, shares: int | None,
               dividend_per_share: Decimal | None) -> tuple[dict, list[str]]:
    """`valuation_ratios` columns from the financials, the share price at period end and the share count.

    market cap = price x shares; EV = market cap + net debt. Price-based columns are
    NULL when there is no price. p_e uses the period's EPS (not annualised).
    """
    notes: list[str] = []
    mcap = price * Decimal(shares) if price is not None and shares else None
    ev = mcap + fin["net_liabilities"] if mcap is not None and fin.get("net_liabilities") is not None else None
    dy = _div(dividend_per_share, price)
    values = {
        "p_e": _div(price, fin.get("earnings_per_share")),
        "p_b": _div(mcap, fin.get("total_equity")),
        "p_s": _div(mcap, fin.get("revenue")),
        "ev_s": _div(ev, fin.get("revenue")),
        "net_debt_ebitda": _div(fin.get("net_liabilities"), fin.get("ebitda")),
        "dividend_per_share": dividend_per_share,
        "dividend_yield": dy * HUNDRED if dy is not None else None,
    }
    if price is None:
        notes.append("no share price on or before period_end: price-based ratios left NULL")
    return _fit_all(values, VALUATIONS, notes), notes
