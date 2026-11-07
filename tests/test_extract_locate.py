"""Page splitting, statement locating and period detection (dataPipeline/extract).
Run: python tests/test_extract_locate.py   (or pytest)
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_sample  # noqa: E402
from dataPipeline.extract import period  # noqa: E402
from dataPipeline.extract.locate import locate  # noqa: E402
from dataPipeline.extract.metrics import industry, keyword_pattern, normalise  # noqa: E402
from dataPipeline.extract.pages import split_pages, table_rows  # noqa: E402


def test_split_pages_uses_markers():
    pages = split_pages(extract_sample.markdown())
    assert sorted(pages) == [1, 2, 3, 4, 5, 6]
    assert pages[4].startswith("## Konsolidirani izkaz poslovnega izida")
    assert split_pages("no markers") == {1: "no markers"}


def test_table_rows_skip_separator():
    rows = table_rows(extract_sample.BALANCE_GROUP)
    assert rows[0] == ["Postavka", "30. 9. 2025", "31. 12. 2024"]
    assert ["Sredstva skupaj", "1.000.000", "970.000"] in rows


def test_locate_prefers_group_statements_and_ignores_toc():
    found, scope, _ = locate(split_pages(extract_sample.markdown()))
    assert scope == "consolidated"
    assert found["income"].pages == [4]      # not the TOC (2), not the company statement (6)
    assert found["balance"].pages == [5]
    assert found["highlights"].pages == [3]
    assert "cashflow" not in found


def test_locate_without_group_uses_company_statement():
    pages = {1: extract_sample.COVER.replace("Skupina ", ""), 2: extract_sample.INCOME_COMPANY}
    found, scope, _ = locate(pages)
    assert scope == "separate" and found["income"].pages == [2]


def test_keywords_match_word_starts_only():
    assert not keyword_pattern(["konsolidiran"]).search(normalise("Nekonsolidirani izkaz"))
    assert keyword_pattern(["konsolidiran"]).search(normalise("Konsolidirani izkaz"))
    assert not keyword_pattern(["1-3"]).search("1-30")
    assert normalise("Izkaz finančnega položaja ĐŽ") == "izkaz financnega polozaja dz"


def test_period_from_cover_and_statement_headers():
    pages = split_pages(extract_sample.markdown())
    guess = period.detect(pages, [4, 5])
    assert guess.period_end == date(2025, 9, 30)
    assert guess.period_type == "Q3"
    assert guess.unit_scale == 1000
    assert guess.currency == "EUR"


def test_period_type_from_month_when_cover_is_silent():
    guess = period.detect({1: "# Primer", 2: "Bilanca stanja na dan 31. decembra 2024 (u tisućama kuna)"}, [2])
    assert (guess.period_end, guess.period_type, guess.unit_scale, guess.currency) == \
        (date(2024, 12, 31), "FY", 1000, "HRK")
    assert sorted(period.dates_in("as at June 30, 2024 and 31 December 2023")) == [date(2023, 12, 31), date(2024, 6, 30)]


def test_industry():
    assert industry("K64.190", None) == "bank"
    assert industry(None, "Zavarovalništvo", "Zavarovalnica Triglav d.d.") == "insurer"
    assert industry("C20.1", "Kemična industrija") == "general"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
