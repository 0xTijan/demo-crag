"""Number parsing for dataPipeline/extract (numbers.py). Synthetic inputs only.
Run: python tests/test_extract_numbers.py   (or pytest)
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.extract.numbers import decimal_style, parse_number  # noqa: E402


def test_slovenian_and_english_formats():
    assert parse_number("1.234.567,89") == Decimal("1234567.89")
    assert parse_number("1 234 567,89") == Decimal("1234567.89")
    assert parse_number("1,234,567.89") == Decimal("1234567.89")
    assert parse_number("1 234") == Decimal("1234")
    assert parse_number("2,15") == Decimal("2.15")
    assert parse_number("0.5") == Decimal("0.5")


def test_negatives():
    assert parse_number("(1.234)") == Decimal("-1234")
    assert parse_number("-1.234") == Decimal("-1234")
    assert parse_number("−1.234,5") == Decimal("-1234.5")
    assert parse_number("( 12.345 )") == Decimal("-12345")


def test_empty_cells():
    for raw in ["", "-", "–", "—", "n/a", None, "/"]:
        assert parse_number(raw) is None


def test_three_digits_after_one_separator_uses_table_style():
    assert parse_number("1.234") == Decimal("1234")              # default: thousands
    assert parse_number("1,234") == Decimal("1234")
    assert parse_number("1.524", decimal=".") == Decimal("1.524")
    assert parse_number("1,524", decimal=",") == Decimal("1.524")
    assert parse_number("1.524", decimal=",") == Decimal("1524")


def test_decimal_style_from_unambiguous_cells():
    assert decimal_style(["1.234.567", "12", "(980.100)"]) == ","
    assert decimal_style(["1,234,567", "3.5"]) == "."
    assert decimal_style(["1.234", "5"]) is None


def test_text_is_rejected():
    try:
        parse_number("12-34")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
