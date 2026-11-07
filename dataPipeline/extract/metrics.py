"""The canonical line items this step extracts, and the keywords used to find them.

One list, imported by the locator, the prompt builder, the parser and the tests.
Keywords are matched against `normalise(text)`: lower case, diacritics removed
(č -> c, đ -> d, ß -> ss), so they are written here without diacritics. A keyword
must start at a word boundary, so "konsolidiran" does not match "nekonsolidirani"
(see keyword_pattern).
"""
from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------
# Line items. statement: income | balance | cashflow | highlights (key figures,
# dividend notes). per_share items are never multiplied by the table unit.
# description goes into the prompt; labels are typical row labels (SI/HR/DE/EN).
# --------------------------------------------------------------------------
ITEMS: dict[str, dict] = {
    "revenue": {
        "statement": "income",
        "description": "Revenue / net sales (total operating revenue from sales)",
        "labels": ["cisti prihodki od prodaje", "prihodki od prodaje", "poslovni prihodki",
                   "prihodi od prodaje", "poslovni prihodi", "umsatzerlose", "revenue", "net sales", "sales"],
    },
    "operating_profit": {
        "statement": "income",
        "description": "Operating profit (EBIT)",
        "labels": ["poslovni izid iz poslovanja", "dobicek iz poslovanja", "dobit iz poslovanja",
                   "operativna dobit", "betriebsergebnis", "ebit", "operating profit", "operating result"],
    },
    "depreciation_amortisation": {
        "statement": "income",
        "description": "Depreciation and amortisation expense (as a positive or negative number, as printed)",
        "labels": ["amortizacija", "odpisi vrednosti", "abschreibungen", "depreciation", "amortisation",
                   "amortization"],
    },
    "ebitda": {
        "statement": "income",
        "description": "EBITDA, only if the table prints it as its own row",
        "labels": ["ebitda"],
    },
    "net_income": {
        "statement": "income",
        "description": ("Net profit or loss for the period. If the table splits it, take the part "
                        "attributable to owners of the parent / controlling interests"),
        "labels": ["cisti poslovni izid", "cisti dobicek", "cista dobit", "dobit razdoblja",
                   "periodenergebnis", "jahresuberschuss", "net profit", "profit for the period", "net income"],
    },
    "eps": {
        "statement": "income",
        "per_share": True,
        "description": "Basic earnings per share",
        "labels": ["cisti dobicek na delnico", "osnovni cisti dobicek na delnico", "zarada po dionici",
                   "ergebnis je aktie", "earnings per share", "basic eps"],
    },
    "total_assets": {
        "statement": "balance",
        "description": "Total assets",
        "labels": ["sredstva skupaj", "skupaj sredstva", "ukupna imovina", "aktiva ukupno", "summe aktiva",
                   "bilanzsumme", "total assets"],
    },
    "total_equity": {
        "statement": "balance",
        "description": "Total equity (including non-controlling interests)",
        "labels": ["kapital", "skupaj kapital", "kapital i rezerve", "ukupni kapital", "eigenkapital",
                   "total equity"],
    },
    "total_liabilities": {
        "statement": "balance",
        "description": "Total liabilities (non-current + current), if printed",
        "labels": ["obveznosti skupaj", "ukupne obveze", "verbindlichkeiten", "total liabilities"],
    },
    "current_assets": {
        "statement": "balance",
        "description": "Total current (short-term) assets",
        "labels": ["kratkorocna sredstva", "kratkotrajna imovina", "umlaufvermogen", "current assets"],
    },
    "current_liabilities": {
        "statement": "balance",
        "description": "Total current (short-term) liabilities",
        "labels": ["kratkorocne obveznosti", "kratkorocne obveze", "kurzfristige verbindlichkeiten",
                   "current liabilities"],
    },
    "financial_liabilities": {
        "statement": "balance",
        "description": ("Financial debt: borrowings, loans, bonds and lease liabilities, non-current and "
                        "current. If they are on separate rows, return the non-current one here and the "
                        "current one as financial_liabilities_current"),
        "labels": ["financne obveznosti", "dolgorocne financne obveznosti", "obveze po kreditima",
                   "financijske obveze", "finanzverbindlichkeiten", "borrowings", "financial liabilities"],
    },
    "financial_liabilities_current": {
        "statement": "balance",
        "description": "Current (short-term) financial debt, only when printed on its own row",
        "labels": ["kratkorocne financne obveznosti", "kratkorocne financijske obveze",
                   "current borrowings", "short-term financial liabilities"],
    },
    "cash": {
        "statement": "balance",
        "description": "Cash and cash equivalents",
        "labels": ["denarna sredstva", "denar in denarni ustrezniki", "novac i novcani ekvivalenti",
                   "zahlungsmittel", "cash and cash equivalents"],
    },
    "dividend_per_share": {
        "statement": "highlights",
        "per_share": True,
        "description": "Dividend per share (paid or proposed for the period)",
        "labels": ["dividenda na delnico", "bruto dividenda", "dividenda po dionici", "dividende je aktie",
                   "dividend per share"],
    },
}

# Items that count as "core" for the minimum-coverage check.
CORE_ITEMS = ["revenue", "operating_profit", "net_income", "total_assets", "total_equity"]

# Which items each statement call asks for. A later call only asks for what is
# still missing, so `highlights` also fills in EBITDA / EPS when the statements lack them.
STATEMENT_ITEMS = {
    "income": [k for k, v in ITEMS.items() if v["statement"] == "income"],
    "balance": [k for k, v in ITEMS.items() if v["statement"] == "balance"],
    "cashflow": ["depreciation_amortisation"],
    "highlights": ["ebitda", "eps", "dividend_per_share", "revenue", "net_income"],
}

STATEMENT_NAMES = {
    "income": "income statement (statement of profit or loss)",
    "balance": "balance sheet (statement of financial position)",
    "cashflow": "cash flow statement",
    "highlights": "key figures / financial highlights",
}

# Statement headings.
STATEMENT_KEYWORDS: dict[str, list[str]] = {
    "income": [
        "izkaz poslovnega izida", "izkaz vseobsegajocega donosa", "izkaz celotnega vseobsegajocega donosa",
        "racun dobiti i gubitka", "racun dobiti", "izvjestaj o sveobuhvatnoj dobiti",
        "izvjestaj o dobiti", "gewinn- und verlustrechnung", "gewinn und verlustrechnung",
        "income statement", "statement of profit or loss", "profit and loss",
        "statement of comprehensive income",
    ],
    "balance": [
        "izkaz financnega polozaja", "bilanca stanja", "izvjestaj o financijskom polozaju", "bilanca",
        "bilanz", "statement of financial position", "balance sheet",
    ],
    "cashflow": [
        "izkaz denarnih tokov", "izvjestaj o novcanom toku", "izvjestaj o novcanim tokovima",
        "kapitalflussrechnung", "geldflussrechnung", "cash flow statement", "statement of cash flows",
    ],
    "highlights": [
        "kljucni podatki", "kljucni financni podatki", "pomembnejsi podatki", "kljucni kazalniki",
        "pomembnejsi kazalniki", "kljucni pokazatelji", "kljucni financijski pokazatelji",
        "financial highlights", "key figures", "key financial", "kennzahlen", "dividend",
    ],
}

SCOPE_KEYWORDS = {
    "consolidated": ["skupina", "skupine", "skupini", "konsolidiran", "grupa", "grupe", "grupi", "konzern",
                     "consolidated", "group"],
    "separate": ["druzba", "druzbe", "druzbi", "drustvo", "drustva", "nekonsolidiran", "separate",
                 "parent company", "einzelabschluss", "unconsolidated"],
}

# --------------------------------------------------------------------------
# Industry: banks and insurers have no revenue line; the prompt gets a hint
# and the revenue checks are skipped for them.
# --------------------------------------------------------------------------
INDUSTRY_HINTS = {
    "general": "",
    "bank": ("This company is a bank. For revenue use total operating income "
             "(net interest income + net fee and commission income + other operating income). "
             "It has no current/non-current split; leave current_assets and current_liabilities null."),
    "insurer": ("This company is an insurer. For revenue use net premiums earned "
                "(or insurance revenue under IFRS 17). Leave current_assets and current_liabilities null."),
}

_BANK = re.compile(r"\b(bank|banc)", re.I)
_INSURER = re.compile(r"\b(insur|zavarovaln|osiguranj|versicher|pozavarovaln|reinsur)", re.I)


def industry(nace: str | None, sector_name: str | None, name: str | None = None) -> str:
    """'bank', 'insurer' or 'general' from NACE (K64.1 / K65) and the sector or company name."""
    m = re.search(r"\d+(?:\.\d+)?", nace or "")  # "K64.19 Other monetary intermediation" -> 64.19
    code = m.group(0) if m else ""
    if code.startswith("65"):
        return "insurer"
    if code.startswith("64.1") or code.startswith("6419"):
        return "bank"
    text = normalise(" ".join(filter(None, [sector_name, name])))
    if _INSURER.search(text):
        return "insurer"
    if _BANK.search(text):
        return "bank"
    return "general"


def normalise(text: str) -> str:
    """Lower case, no diacritics, single spaces: the form all keywords are matched against."""
    text = text.replace("ß", "ss").replace("đ", "d").replace("Đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def keyword_pattern(keywords: list[str]) -> re.Pattern:
    """One regex matching any keyword at a word start.

    Keywords of 4 characters or fewer ("q1", "1-3", "ebit") must also end at a
    word boundary, so "1-3" does not match "1-30" and "ebit" not "ebitda".
    """
    parts = [re.escape(k) + (r"(?![a-z0-9])" if len(k) <= 4 else "")
             for k in sorted(keywords, key=len, reverse=True)]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")
