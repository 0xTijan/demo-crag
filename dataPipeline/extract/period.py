"""Period, unit and currency of a report from its own text (regex, no LLM).

The LLM returns the same fields from the statement header; run.py compares the
two and marks the report needs_review when they disagree.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from .metrics import keyword_pattern, normalise

MONTHS = {
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # sl (genitive forms used in dates: "31. decembra 2024")
    "januarja": 1, "februarja": 2, "marca": 3, "aprila": 4, "maja": 5, "junija": 6, "julija": 7,
    "avgusta": 8, "septembra": 9, "oktobra": 10, "novembra": 11, "decembra": 12,
    # hr
    "sijecnja": 1, "veljace": 2, "ozujka": 3, "travnja": 4, "svibnja": 5, "lipnja": 6, "srpnja": 7,
    "kolovoza": 8, "rujna": 9, "listopada": 10, "studenog": 11, "studenoga": 11, "prosinca": 12,
    # de
    "januar": 1, "februar": 2, "marz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
}

NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d{2})(?!\d)")
ISO_DATE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_MONTH_YEAR = re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_ALT})\s+(20\d{{2}})")
MONTH_DAY_YEAR = re.compile(rf"\b({_MONTH_ALT})\s+(\d{{1,2}}),?\s+(20\d{{2}})")

PERIOD_KEYWORDS = {  # checked in this order; the first type with a hit wins
    "1H": ["polletn", "polugodisnj", "half-year", "half year", "halbjahr", "1-6", "i-vi", "6m", "h1", "1h"],
    "Q1": ["1-3", "i-iii", "3m", "q1", "prvo cetrtletje", "prvo trimesecje", "prvo tromjesecje",
           "prvi kvartal", "first quarter", "1. quartal"],
    "Q3": ["1-9", "i-ix", "9m", "q3", "devetmesecj", "devetmjesecn", "nine months", "nine-month",
           "tretje cetrtletje", "trece tromjesecje", "3. quartal"],
    "Q4": ["q4", "1-12", "i-xii", "cetrto cetrtletje", "cetvrto tromjesecje", "fourth quarter", "4. quartal"],
    "FY": ["letno porocilo", "letnem porocilu", "godisnje izvjesce", "godisnji izvjestaj",
           "godisnji financijski izvjestaj", "annual report", "jahresfinanzbericht", "geschaftsbericht",
           "jahresabschluss", "konsolidirano letno", "revidirano"],
}
_PERIOD_PATTERNS = {k: keyword_pattern(v) for k, v in PERIOD_KEYWORDS.items()}
MONTH_TO_TYPE = {3: "Q1", 6: "1H", 9: "Q3", 12: "FY"}

UNIT_PATTERNS = [  # (scale, regex on normalised text); millions and thousands before plain units
    (1_000_000, re.compile(r"v mio|v milijonih|u milijunima|in millions|in mio|mio\. ?eur|eur ?m\b|"
                           r"eur million|mio eur|mil\. ?eur|\bmeur\b|\bmio\b")),
    (1000, re.compile(r"v tisoc|v 000|v eur ?000|eur ?'?000|000 eur|\bteur\b|tsd\.? ?eur|u tisucama|"
                      r"u 000|in thousands|in eur thousand|eur thousand|thousands of eur|\(000\)|"
                      r"in tausend|v tisocih|tisuc[a-z]* (eur|kuna|kn)")),
    (1, re.compile(r"\bv eur\b|\bu eurima\b|\bin eur\b|\bu kunama\b|\bv evrih\b|\(eur\)")),
]
CURRENCIES = [("HRK", re.compile(r"\bhrk\b|\bkuna\b|\bkunama\b|\bkn\b")),
              ("EUR", re.compile(r"\beur\b|\beurima\b|€|\bevr")),
              ("USD", re.compile(r"\busd\b|\$"))]


@dataclass
class PeriodGuess:
    period_end: date | None
    period_type: str | None
    unit_scale: int | None
    currency: str | None


def _month_end(y: int, m: int, d: int) -> date | None:
    try:
        value = date(y, m, d)
    except ValueError:
        return None
    return value if d == calendar.monthrange(y, m)[1] else None


def dates_in(text: str) -> list[date]:
    """Month-end dates printed in the text (period ends are always month ends)."""
    t = normalise(text)
    found = []
    for d, m, y in NUMERIC_DATE.findall(t):
        found.append(_month_end(int(y), int(m), int(d)))
    for y, m, d in ISO_DATE.findall(t):
        found.append(_month_end(int(y), int(m), int(d)))
    for d, mon, y in DAY_MONTH_YEAR.findall(t):
        found.append(_month_end(int(y), MONTHS[mon], int(d)))
    for mon, d, y in MONTH_DAY_YEAR.findall(t):
        found.append(_month_end(int(y), MONTHS[mon], int(d)))
    return [x for x in found if x and x <= date.today()]


def period_type_in(text: str) -> str | None:
    t = normalise(text)
    for period_type, pattern in _PERIOD_PATTERNS.items():
        if pattern.search(t):
            return period_type
    return None


def unit_in(text: str) -> int | None:
    t = normalise(text)
    for scale, pattern in UNIT_PATTERNS:
        if pattern.search(t):
            return scale
    return None


def currency_in(text: str) -> str | None:
    t = normalise(text)
    for code, pattern in CURRENCIES:
        if pattern.search(t):
            return code
    return None


def detect(pages: dict[int, str], statement_pages: list[int]) -> PeriodGuess:
    """Guess the reporting period from the cover (pages 1-2) and the statement pages.

    period_end: the latest month-end date on the statement pages (the current
    column; comparatives are older), else on the cover. period_type: keywords on
    the cover, else from the month (Dec -> FY, Jun -> 1H, Mar -> Q1, Sep -> Q3).
    """
    cover = "\n".join(pages[n] for n in sorted(pages)[:2])
    stmt = "\n".join(pages[n] for n in statement_pages if n in pages)
    dates = dates_in(stmt) or dates_in(cover)
    period_end = max(dates) if dates else None

    period_type = period_type_in(cover)
    if period_end:
        by_month = MONTH_TO_TYPE.get(period_end.month)
        if period_type is None or (period_type == "Q4" and period_end.month != 12):
            period_type = by_month
        elif period_type in ("FY", "1H", "Q1", "Q3") and by_month and by_month != period_type:
            # cover keyword and date disagree ("annual report" with a June date): trust the date
            period_type = by_month
    return PeriodGuess(period_end, period_type, unit_in(stmt) or unit_in(cover), currency_in(stmt) or currency_in(cover))
