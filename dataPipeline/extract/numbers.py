"""Turn the number strings printed in a report into Decimals. Deterministic, no LLM.

Handles the formats found in Slovenian, Croatian, Austrian and English reports:
    1.234.567,89   1 234 567,89   1,234,567.89   (1.234)   -1.234   −1.234   –   —   n/a
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

EMPTY = {"", "-", "–", "—", "−", "/", "n/a", "na", "n.a.", "x", "*", "0-"}
NEGATIVE_SIGNS = "-−–"
_KEEP = re.compile(r"[^0-9.,()\-−–]")


def core(raw: str) -> str:
    """Digits and separators only: what two spellings of the same number share."""
    return re.sub(r"[^0-9.,]", "", raw or "").strip(".,")


def decimal_style(samples: list[str]) -> str | None:
    """',' or '.' as the decimal separator of a table, from its unambiguous numbers.

    "1.234.567" (separator repeated) or "1.234,5" (both, the last is decimal) are
    unambiguous; "1.234" alone is not. Returns None if nothing decides it.
    """
    votes = {",": 0, ".": 0}
    for s in samples:
        c = core(s)
        if "." in c and "," in c:
            votes["," if c.rfind(",") > c.rfind(".") else "."] += 1
        elif c.count(".") > 1:
            votes[","] += 1
        elif c.count(",") > 1:
            votes["."] += 1
        else:
            m = re.fullmatch(r"\d+([.,])(\d+)", c)
            if m and len(m.group(2)) != 3:
                votes[m.group(1)] += 1
    if votes[","] == votes["."]:
        return None
    return "," if votes[","] > votes["."] else "."


def parse_number(raw: str | None, decimal: str | None = None) -> Decimal | None:
    """Parse one printed number. Returns None for empty cells and dashes.

    decimal: the table's decimal separator (from decimal_style). Needed only for
    "1.234" / "1,234" with exactly three digits after a single separator; without
    a hint those are read as thousands, the usual case in financial statements.
    Raises ValueError for text that is not a number.
    """
    if raw is None:
        return None
    s = raw.strip().replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    if s.lower() in EMPTY:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s and s[0] in NEGATIVE_SIGNS:
        negative, s = not negative, s[1:].strip()
    s = _KEEP.sub("", s.replace(" ", "").replace("'", ""))
    if not re.search(r"\d", s):
        return None
    if re.search(r"[()\-−–]", s):
        raise ValueError(f"not a number: {raw!r}")

    if "." in s and "," in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif s.count(".") > 1:
        dec = ","
    elif s.count(",") > 1:
        dec = "."
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        digits_after = len(s) - s.index(sep) - 1
        if digits_after == 3:  # ambiguous: the table's style decides, default thousands
            dec = decimal if decimal in (",", ".") else ("," if sep == "." else ".")
        else:
            dec = sep
    else:
        dec = ","
    thousands = "." if dec == "," else ","
    s = s.replace(thousands, "").replace(dec, ".")
    if s.count(".") > 1:
        raise ValueError(f"not a number: {raw!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc
    return -value if negative else value


UNIT_SCALE = {"units": 1, "thousands": 1000, "millions": 1_000_000}
