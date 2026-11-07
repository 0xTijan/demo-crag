"""Consistency checks (dataPipeline/extract/validate.py).
Run: python tests/test_extract_validate.py   (or pytest)
"""
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.extract.validate import validate  # noqa: E402

CURRENT = {"revenue": D(1000), "operating_profit": D(100), "net_income": D(60),
           "total_assets": D(2000), "total_equity": D(800), "total_liabilities": D(1200)}
PERIOD = {"text": (date(2025, 9, 30), "Q3"), "model": (date(2025, 9, 30), "Q3")}


def run(current=CURRENT, prior=None, **kw):
    args = dict(industry="general", unit_scale=1, period_end=date(2025, 9, 30), period_type="Q3",
                period_votes=PERIOD, fiscal_year_hint=2025, stored_prior=None, stored_prior_balance=None, siblings={}, dropped=[])
    args.update(kw)
    return [c["check"] for c in validate(current, prior or {}, **args)]


def test_clean_report_passes():
    assert run() == []


def test_balance_and_plausibility():
    assert "balance" in run({**CURRENT, "total_liabilities": D(1000)})
    assert "plausibility" in run({**CURRENT, "operating_profit": D(5000)})
    assert "plausibility" not in run({**CURRENT, "operating_profit": D(5000)}, industry="bank")


def test_period_disagreement_and_coverage():
    assert "period" in run(period_votes={"text": (date(2025, 9, 30), "Q3"), "model": (date(2025, 6, 30), "1H")})
    assert "period" in run(fiscal_year_hint=2021)
    assert "coverage" in run({"revenue": D(1), "total_assets": D(1)})
    assert "not_on_page" in run(dropped=["net_income.current: '1' is not printed"])


def test_comparative_yoy_and_cumulative():
    stored = {"revenue": D(900), "net_income": D(50), "total_assets": D(1900), "total_equity": D(750), "ebit": D(90)}
    year_end = {"total_assets": D(1950), "total_equity": D(780)}
    assert run(prior={"revenue": D(900), "total_assets": D(1950)}, stored_prior=stored,
               stored_prior_balance=year_end) == []
    assert "comparative" in run(prior={"revenue": D(950)}, stored_prior=stored)
    # balance comparatives are checked against the year end, not the same quarter last year
    assert "comparative" in run(prior={"total_assets": D(1900)}, stored_prior=stored, stored_prior_balance=year_end)
    assert "comparative" not in run(prior={"total_assets": D(1900)}, stored_prior=stored)
    assert "yoy" in run(stored_prior={**stored, "revenue": D(50)})
    assert "yoy" in run(stored_prior_balance={**year_end, "total_assets": D(100)})
    assert "cumulative" in run(siblings={"1H": D(1500)})
    assert run(siblings={"1H": D(600), "FY": D(1400)}) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
