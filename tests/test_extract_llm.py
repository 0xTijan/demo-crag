"""Model mapping + verification (dataPipeline/extract/llm.py, run.py), with a fake model.
No Ollama, no database. Run: python tests/test_extract_llm.py   (or pytest)
"""
import copy
import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_sample  # noqa: E402
from dataPipeline.extract import llm, run  # noqa: E402
from dataPipeline.extract.locate import locate  # noqa: E402
from dataPipeline.extract.metrics import STATEMENT_NAMES  # noqa: E402
from dataPipeline.extract.pages import split_pages  # noqa: E402

PAGES = split_pages(extract_sample.markdown())


def fake_chat(edit=None, calls=None):
    """A model that answers extract_sample.model_reply; `edit(statement, reply)` can corrupt it."""
    def chat(messages, schema):
        user = messages[1]["content"]
        statement = next(s for s, name in STATEMENT_NAMES.items() if f"Statement: {name}" in user)
        if calls is not None:
            calls.append((statement, sorted(schema["properties"]["items"]["properties"])))
        reply = copy.deepcopy(extract_sample.model_reply(statement))
        if edit:
            edit(statement, reply)
        return "```json\n" + json.dumps(reply) + "\n```"
    return chat


def extract(chat):
    found, scope, _ = locate(PAGES)
    results = run.run_statements(chat, found, PAGES, {"name": "Primer d.d."}, scope, "general", "Q3")
    return results, run.parse_items(results, PAGES, 1000)


def test_schema_requires_every_item():
    schema = llm.build_schema(["revenue", "eps"])
    assert schema["properties"]["items"]["required"] == ["revenue", "eps"]


def test_full_report_with_a_correct_model():
    calls = []
    results, (current, prior, items_json, dropped, scales) = extract(fake_chat(calls=calls))
    # income + balance, then key figures only for what is still missing (EBITDA); no dividend for Q3
    assert [c[0] for c in calls] == ["income", "balance", "highlights"]
    assert calls[2][1] == ["ebitda"]
    assert dropped == [] and scales == {1000}
    assert current["revenue"] == D(1_234_567_000)
    assert current["depreciation_amortisation"] == D(-33_087_000)  # sign taken from the cell "(33.087)"
    assert current["eps"] == D("2.15")                              # per share: not x1000
    assert current["ebitda"] == D(98_765_000)
    assert prior["total_assets"] == D(970_000_000)
    assert items_json["net_income"]["page"] == 4
    assert items_json["depreciation_amortisation"]["current_raw"] == "(33.087)"


def test_numbers_not_on_the_page_are_dropped():
    def hallucinate(statement, reply):
        if statement == "income":
            reply["items"]["revenue"]["current"] = "1.234.568"
            reply["items"]["net_income"]["page"] = 99          # wrong page: still found on the real one
    _, (current, _, _, dropped, _) = extract(fake_chat(hallucinate))
    assert "revenue" not in current
    assert current["net_income"] == D(45_678_000)
    assert len(dropped) == 1 and dropped[0].startswith("revenue.current")


def test_same_number_in_two_rows_uses_the_label():
    pages = {1: "| Postavka | 2025 |\n|---|---|\n| Kapital | 500 |\n| Rezerve | (500) |\n"}
    hit = llm.find_number("500", "Rezerve", pages, [1], 1)
    assert hit.raw == "(500)" and hit.label == "Rezerve"
    assert llm.find_number("500", "Kapital", pages, [1], 1).raw == "500"
    assert llm.find_number("1,50", None, {1: "Bruto dividenda znaša 1,50 EUR na delnico."}, [1], 1).raw == "1,50"


def test_same_values_tolerance():
    assert run.same_values({"revenue": D(1000)}, {"revenue": D(1004)})
    assert not run.same_values({"revenue": D(1000)}, {"revenue": D(1010)})


def test_prompt_contains_only_located_pages():
    found, scope, _ = locate(PAGES)
    messages = llm.build_messages("income", ["revenue"], llm.join_pages(PAGES, found["income"].pages),
                                  "Primer", scope, "bank")
    assert "<!-- page: 4 -->" in messages[1]["content"] and "<!-- page: 6 -->" not in messages[1]["content"]
    assert "group (consolidated)" in messages[0]["content"]
    assert "This company is a bank" in messages[1]["content"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
