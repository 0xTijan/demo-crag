"""A small synthetic report in the Markdown format dataPipeline/convert writes.

Shared by the test_extract_*.py files. Slovenian layout: a cover, a table of
contents (mentions every statement but has no numbers), key figures, then the
group (skupina) and the company (družba) statements. Amounts are in thousand EUR.
"""

COVER = """# Skupina Primer d.d.

## Nerevidirano poročilo o poslovanju za obdobje januar–september 2025

Ljubljana, november 2025
"""

TOC = """## Kazalo

- Ključni podatki ........ 3
- Izkaz poslovnega izida Skupine ........ 4
- Izkaz finančnega položaja Skupine ........ 5
- Izkaz poslovnega izida družbe ........ 6
"""

HIGHLIGHTS = """## Ključni podatki Skupine

| v tisoč EUR | 1–9 2025 | 1–9 2024 |
|---|---|---|
| Čisti prihodki od prodaje | 1.234.567 | 1.101.220 |
| EBITDA | 98.765 | 90.001 |
| Čisti poslovni izid | 45.678 | 40.100 |
| Število zaposlenih | 1.234 | 1.200 |
| Čisti dobiček na delnico (v EUR) | 2,15 | 1,89 |
"""

INCOME_GROUP = """## Konsolidirani izkaz poslovnega izida Skupine Primer

(v tisoč EUR)

| Postavka | Pojasnilo | 1. 1.–30. 9. 2025 | 1. 1.–30. 9. 2024 |
|---|---|---|---|
| Čisti prihodki od prodaje | 1 | 1.234.567 | 1.101.220 |
| Stroški blaga, materiala in storitev | 2 | (987.654) | (880.100) |
| Stroški dela | 3 | (120.000) | (110.000) |
| Amortizacija | 4 | (33.087) | (31.000) |
| Poslovni izid iz poslovanja | | 65.678 | 59.001 |
| Finančni izid | 5 | (8.000) | (7.500) |
| Davek iz dobička | | (12.000) | (11.401) |
| Čisti poslovni izid obračunskega obdobja | | 45.678 | 40.100 |
| Čisti dobiček na delnico (v EUR) | | 2,15 | 1,89 |
"""

BALANCE_GROUP = """## Konsolidirani izkaz finančnega položaja Skupine Primer

v tisoč EUR

| Postavka | 30. 9. 2025 | 31. 12. 2024 |
|---|---|---|
| Nekratkoročna sredstva | 600.000 | 590.000 |
| Kratkoročna sredstva | 400.000 | 380.000 |
| Denar in denarni ustrezniki | 50.000 | 45.000 |
| Sredstva skupaj | 1.000.000 | 970.000 |
| Kapital | 450.000 | 420.000 |
| Dolgoročne finančne obveznosti | 200.000 | 210.000 |
| Kratkoročne finančne obveznosti | 60.000 | 55.000 |
| Kratkoročne obveznosti | 250.000 | 240.000 |
| Obveznosti skupaj | 550.000 | 550.000 |
"""

INCOME_COMPANY = """## Izkaz poslovnega izida družbe Primer d.d.

(v tisoč EUR)

| Postavka | 1–9 2025 | 1–9 2024 |
|---|---|---|
| Čisti prihodki od prodaje | 800.000 | 760.000 |
| Amortizacija | (20.000) | (19.000) |
| Poslovni izid iz poslovanja | 40.000 | 38.000 |
| Davek iz dobička | (6.000) | (5.500) |
| Čisti poslovni izid obračunskega obdobja | 30.000 | 28.000 |
"""

PAGES = {1: COVER, 2: TOC, 3: HIGHLIGHTS, 4: INCOME_GROUP, 5: BALANCE_GROUP, 6: INCOME_COMPANY}


def markdown() -> str:
    """The whole document, joined like dataPipeline/convert/vision_ocr.join_pages."""
    return "\n\n".join(f"<!-- page: {n} -->\n\n{t.strip()}" for n, t in sorted(PAGES.items())) + "\n"


def model_reply(statement: str) -> dict:
    """What a well-behaved model answers for each statement of this report."""
    header = {"period_end": "2025-09-30", "period_type": "Q3", "unit": "thousands", "currency": "EUR"}
    if statement == "income":
        items = {
            "revenue": {"row_label": "Čisti prihodki od prodaje", "current": "1.234.567", "prior": "1.101.220", "page": 4},
            "operating_profit": {"row_label": "Poslovni izid iz poslovanja", "current": "65.678", "prior": "59.001", "page": 4},
            "depreciation_amortisation": {"row_label": "Amortizacija", "current": "33.087", "prior": "31.000", "page": 4},
            "ebitda": None,
            "net_income": {"row_label": "Čisti poslovni izid obračunskega obdobja", "current": "45.678", "prior": "40.100", "page": 4},
            "eps": {"row_label": "Čisti dobiček na delnico (v EUR)", "current": "2,15", "prior": "1,89", "page": 4},
        }
    elif statement == "balance":
        items = {
            "total_assets": {"row_label": "Sredstva skupaj", "current": "1.000.000", "prior": "970.000", "page": 5},
            "total_equity": {"row_label": "Kapital", "current": "450.000", "prior": "420.000", "page": 5},
            "total_liabilities": {"row_label": "Obveznosti skupaj", "current": "550.000", "prior": "550.000", "page": 5},
            "current_assets": {"row_label": "Kratkoročna sredstva", "current": "400.000", "prior": "380.000", "page": 5},
            "current_liabilities": {"row_label": "Kratkoročne obveznosti", "current": "250.000", "prior": "240.000", "page": 5},
            "financial_liabilities": {"row_label": "Dolgoročne finančne obveznosti", "current": "200.000", "prior": "210.000", "page": 5},
            "financial_liabilities_current": {"row_label": "Kratkoročne finančne obveznosti", "current": "60.000", "prior": "55.000", "page": 5},
            "cash": {"row_label": "Denar in denarni ustrezniki", "current": "50.000", "prior": "45.000", "page": 5},
        }
    else:
        items = {"ebitda": {"row_label": "EBITDA", "current": "98.765", "prior": "90.001", "page": 3}}
    return {**header, "items": items}
