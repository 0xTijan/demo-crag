"""Derived columns for financials / valuation_ratios (dataPipeline/extract/derive.py).
Run: python tests/test_extract_derive.py   (or pytest)
"""
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.extract import derive  # noqa: E402

RAW = {
    "revenue": D(1_234_567_000), "operating_profit": D(65_678_000), "depreciation_amortisation": D(-33_087_000),
    "net_income": D(45_678_000), "eps": D("2.15"), "total_assets": D(1_000_000_000),
    "total_equity": D(450_000_000), "current_assets": D(400_000_000), "current_liabilities": D(250_000_000),
    "financial_liabilities": D(200_000_000), "financial_liabilities_current": D(60_000_000), "cash": D(50_000_000),
}


def test_financials_columns():
    fin, notes = derive.financials(RAW, shares=20_000_000)
    assert fin["ebitda"] == D("98765000.00")                     # ebit + |D&A|
    assert fin["ebit"] == D("65678000.00")
    assert fin["roe"] == D("10.1507")                            # 45.678 / 450 x 100, not annualised
    assert fin["roa"] == D("4.5678")
    assert fin["liabilities_equity_ratio"] == D("1.2222")        # (1000 - 450) / 450
    assert fin["net_working_capital"] == D("150000000.00")
    assert fin["net_liabilities"] == D("210000000.00")           # 200 + 60 - 50
    assert fin["earnings_per_share"] == D("2.1500")
    assert "total_liabilities = total_assets - total_equity" in notes


def test_reported_ebitda_and_eps_fallback():
    fin, notes = derive.financials({**RAW, "ebitda": D(99_000_000), "eps": None}, shares=20_000_000)
    assert fin["ebitda"] == D("99000000.00")
    assert fin["earnings_per_share"] == D("2.2839")
    assert "earnings_per_share = net_income / stocks.quantity" in notes


def test_valuations():
    fin, _ = derive.financials(RAW, shares=20_000_000)
    val, notes = derive.valuations(fin, price=D("43.00"), shares=20_000_000, dividend_per_share=D("1.50"))
    assert val["p_e"] == D("20.0000")
    assert val["p_b"] == D("1.9111")                             # 860m / 450m
    assert val["p_s"] == D("0.6966")
    assert val["ev_s"] == D("0.8667")                            # (860 + 210) / 1234.567
    assert val["dividend_yield"] == D("3.4884")
    assert notes == []
    empty, notes = derive.valuations(fin, price=None, shares=20_000_000, dividend_per_share=None)
    assert empty["p_e"] is None and empty["net_debt_ebitda"] == D("2.1263") and notes


def test_overflow_is_null_and_reported():
    fin, notes = derive.financials({"net_income": D(5), "total_equity": D("0.0001")}, shares=None)
    assert fin["roe"] is None
    assert any("does not fit" in n for n in notes)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
