"""Step 0 of plan v1: datasheet classification (dataPipeline/index/classify.py), with a fake LLM.

No Ollama, no network: the fake answers from the text it is given.
Run: python tests/test_index_classify.py   (or pytest)
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.index import classify as cl  # noqa: E402
from dataPipeline.index import common  # noqa: E402
from dataPipeline.index.common import LLMAnswer, LLMError, first_pages, split_pages  # noqa: E402

DOCS = {
    "goodark/ss34.md": "<!-- page: 1 -->\nSS32 thru SS310 Surface Mount Schottky Barrier Rectifiers\n"
                       "<!-- page: 2 -->\nElectrical characteristics\n<!-- page: 3 -->\nPACKAGE OUTLINE SECRET",
    "goodark/2n7000.md": "<!-- page: 1 -->\n2N7000 N-Channel Enhancement Mode MOSFET",
    "novosns/nsi1050.md": "<!-- page: 1 -->\nNSI1050 isolated CAN transceiver",
    "viking/mlcc.md": "<!-- page: 1 -->\nMultilayer Ceramic Chip Capacitors, part numbering system",
    "viking/appnote.md": "<!-- page: 1 -->\nApplication note: soldering guide",
}


class FakeClassifier:
    """Answers like the model would, from keywords in the text; can be told to fail."""
    model = "fake"

    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, system, user):
        self.calls.append(user)
        if any(f in user for f in self.fail_on):
            raise LLMError("model crashed")
        text = user.lower()
        if "application note" in text:
            d = {"is_datasheet": False, "type": "soldering guide"}
        elif "schottky" in text:
            d = {"type": "Schottky Barrier Rectifiers", "family": True, "part_numbers": ["SS32", "SS310"]}
        elif "mosfet" in text:
            d = {"type": "N-channel MOSFET", "family": False, "part_numbers": ["2N7000"], "confidence": "high"}
        elif "can transceiver" in text:
            d = {"type": "isolated CAN transceiver"}
        else:
            d = {"type": "multilayer ceramic capacitor", "family": True}
        return LLMAnswer(d, 0.5, 1000, 40)


class FakeGrouper:
    """Groups by keyword; leaves out one label the first time to test the second request."""
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        labels = user.split("Labels to assign:\n", 1)[1].splitlines()
        keys = {"schottky": "schottky_rectifier", "mosfet": "mosfet_n", "can": "can_transceiver_isolated",
                "ceramic": "mlcc", "mlcc": "mlcc"}
        assign = []
        for lbl in labels:
            key = next((v for k, v in keys.items() if k in lbl.lower()), None)
            if key and not (self.calls == 1 and "CAN" in lbl):  # "forget" one label on the first call
                assign.append({"label": lbl, "type": key})
        return LLMAnswer({"assign": assign, "new_types": [{"key": v, "name": v} for v in set(keys.values())]},
                         0.1, 100, 50)


def setup_dirs():
    root = Path(tempfile.mkdtemp())
    md = root / "markdowns" / "suppliers"
    for rel, text in DOCS.items():
        (md / rel).parent.mkdir(parents=True, exist_ok=True)
        (md / rel).write_text(text, encoding="utf-8")
    common.CACHE_DIR = cl.CACHE_DIR = root / "index_cache"
    cl.TYPE_LIST = root / "index_cache" / "type_list.yaml"
    return md


def run_args(md, **kw):
    return SimpleNamespace(input=str(md), supplier=None, workers=2, limit=None, pages=2, max_chars=12000,
                           force=False, verbose=False, chunk=120, **kw)


def test_first_pages_cuts_after_two_pages():
    md = DOCS["goodark/ss34.md"]
    assert sorted(split_pages(md)) == [1, 2, 3]
    text = first_pages(md, pages=2)
    assert "Schottky" in text and "Electrical" in text and "SECRET" not in text
    assert first_pages("no markers here", pages=2) == "no markers here"
    assert first_pages("![x](data:image/png;base64,AAAA) MOSFET") == " MOSFET"


def test_classify_parses_and_cleans_the_answer():
    r = cl.classify(DOCS["goodark/ss34.md"], FakeClassifier())
    assert r["label"] == "Schottky Barrier Rectifiers" and r["family"] and r["part_numbers"] == ["SS32", "SS310"]
    assert r["confidence"] == "low" and r["is_datasheet"]  # missing confidence -> low
    assert cl.classify(DOCS["viking/appnote.md"], FakeClassifier())["is_datasheet"] is False


def test_run_writes_cache_resumes_and_survives_failures():
    md = setup_dirs()
    llm = FakeClassifier(fail_on=["NSI1050"])
    assert cl.cmd_run(run_args(md), llm) == 1                   # one failure -> exit code 1
    assert len(llm.calls) == 5
    caches = list(common.CACHE_DIR.rglob("*.json"))
    assert len(caches) == 4                                     # the failed one has no cache file
    c = common.read_cache(next(p for p in caches if "goodark" in str(p)))
    assert c["classification"]["version"] == cl.CLASSIFIER_VERSION and c["supplier"] == "goodark"

    llm2 = FakeClassifier()
    assert cl.cmd_run(run_args(md), llm2) == 0                  # rerun: only the failed one again
    assert len(llm2.calls) == 1 and "NSI1050" in llm2.calls[0]

    (md / "goodark" / "2n7000.md").write_text("<!-- page: 1 -->\n2N7000 N-Channel MOSFET rev B", encoding="utf-8")
    llm3 = FakeClassifier()
    cl.cmd_run(run_args(md), llm3)                              # changed file (new hash) -> classified again
    assert len(llm3.calls) == 1 and "rev B" in llm3.calls[0]


def test_group_and_report_build_a_type_list_and_counts():
    md = setup_dirs()
    cl.cmd_run(run_args(md), FakeClassifier())
    grouper = FakeGrouper()
    assert cl.cmd_group(run_args(md), grouper) == 0
    assert grouper.calls == 2                                   # second call for the forgotten label
    types = cl.load_type_list()
    assert "Schottky Barrier Rectifiers" in types["schottky_rectifier"]["labels"]
    assert types["can_transceiver_isolated"]["labels"] == ["isolated CAN transceiver"]
    assert cl.NOT_DATASHEET in types

    assert cl.cmd_report(run_args(md)) == 0
    counts = Counter(common.read_cache(p)["type"] for p in common.CACHE_DIR.rglob("*.json"))
    assert counts == Counter({"schottky_rectifier": 1, "mosfet_n": 1, "can_transceiver_isolated": 1, "mlcc": 1,
                              cl.NOT_DATASHEET: 1})
    assert (common.CACHE_DIR / "type_report.csv").read_text().count("\n") == 6

    # a person edits the list (merges a type); report applies it, group does not ask again
    text = cl.TYPE_LIST.read_text().replace("can_transceiver_isolated:", "interface_ic:")
    cl.TYPE_LIST.write_text(text)
    grouper2 = FakeGrouper()
    cl.cmd_group(run_args(md), grouper2)
    assert grouper2.calls == 0                                   # every label already in the list
    cl.cmd_report(run_args(md))
    assert any(common.read_cache(p)["type"] == "interface_ic" for p in common.CACHE_DIR.rglob("*.json"))


def test_normalise_label_merges_spelling_variants():
    assert cl.normalise_label(" N-Channel  MOSFETs ") == cl.normalise_label("n-channel mosfet")
    assert cl.normalise_label("Schottky diodes") == "schottky diode"
    assert cl.normalise_label("TVS") == "tvs"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
