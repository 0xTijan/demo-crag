"""Step 1 of plan v1: spec extraction (dataPipeline/index/extract.py, values.py) and loading the index cache
into Postgres (load_cache.py), with a fake LLM.

No Ollama, no network. The database test runs only when TEST_DATABASE_URL points to an empty Postgres
database (e.g. postgresql+psycopg2://postgres@localhost:5432/test); it creates and drops catalog_parts.
Run: python -m pytest tests/test_index_extract.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.index import common, extract as ex, load_cache, spec_lists as sl  # noqa: E402
from dataPipeline.index.common import LLMAnswer, LLMError, write_cache  # noqa: E402
from dataPipeline.index.values import (  # noqa: E402
    check_value, convert, parse_conditions, parse_line, parse_value, raw_on_page,
)

MOSFET_MD = """<!-- page: 1 -->
# 2N7000 N-Channel Enhancement Mode MOSFET
| Parameter | Symbol | Value | Unit |
|---|---|---|---|
| Drain-Source Voltage | VDS | 60 | V |
| Drain Current | ID | 200 | mA |
| Operating Temperature | TJ | -55 to +150 | °C |
<!-- page: 2 -->
| RDS(on) | VGS=10V, ID=500mA | 5 | Ω |
<!-- page: 3 -->
Package: TO-92
<!-- page: 4 -->
Tape and reel: carrier tape dimensions | 8 mm |
"""

MLCC_MD = """<!-- page: 1 -->
# Multilayer Ceramic Chip Capacitors
Part numbering: 03 K T B 500 104
<!-- page: 2 -->
| Size | Dielectric | Voltage | Capacitance |
|---|---|---|---|
| 0603 | X7R | 50V | 150pF~470nF |
| 0603 | X7R | 100V | 225~1.6 |
"""

MOSFET_LIST = {
    "type": "mosfet_n", "name": "N-channel MOSFET", "version": 1, "status": "reviewed", "must_match": ["package"],
    "specs": {
        "vds_max": {"label": "VDS max", "unit": "V", "stat": "max", "rule": ">=", "critical": True},
        "id_max": {"label": "ID max", "unit": "A", "stat": "max", "rule": ">=", "critical": True},
        "rds_on_max@vgs_10v": {"label": "RDS(on) max @ VGS=10 V", "unit": "ohm", "stat": "max", "rule": "<=",
                               "critical": False},
        "operating_temp_range": {"label": "TJ", "unit": "degC", "stat": "range", "rule": "contains", "critical": False},
        "package": {"label": "Package", "unit": None, "stat": "value", "rule": "=", "critical": True},
    },
}
MLCC_LIST = {
    "type": "ceramic_capacitor_mlcc", "name": "multilayer ceramic capacitor", "version": 1, "status": "draft",
    "must_match": ["size"],
    "specs": {
        "size": {"label": "Size (inch)", "unit": None, "stat": "value", "rule": "=", "critical": True},
        "dielectric": {"label": "Dielectric", "unit": None, "stat": "value", "rule": "=", "critical": True},
        "rated_voltage": {"label": "Rated voltage", "unit": "V", "stat": "value", "rule": ">=", "critical": True},
        "capacitance": {"label": "Capacitance", "unit": "pF", "stat": "range", "rule": "contains", "critical": True},
        "tolerance": {"label": "Tolerance", "unit": "%", "stat": "value", "rule": "<=", "critical": False},
    },
}


class FakeExtractor:
    """Answers like the model would for the two test datasheets; can be told to fail."""
    model = "fake"

    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, system, user):
        self.calls.append(user)
        if any(f in user for f in self.fail_on):
            raise LLMError("answer is not JSON")
        if "2N7000" in user:
            d = {"common": ["operating_temp_range | -55..150 | °C | 1 | -55 to +150 |"],
                 "parts": [{"part_number": "2N7000", "family_row": "", "ordering_page": 0, "values": [
                     "vds_max | 60 | V | 1 | 60 |",
                     "id_max | 200 | mA | 1 | 200 |",                        # code converts mA -> A
                     "rds_on_max@vgs_10v | 5 | ohm | 1 | 5 | VGS=10V, ID=500mA",  # wrong page: corrected to 2
                     "package | TO-92 | | 3 | TO-92 |",
                     "qg_total | 1.2 | nC | 2 | 1.2 |",                     # not in the spec list: dropped
                     "vgs_th | 2.1 | V | 1 | 2.1 |"]}]}                       # not in the list either
        else:
            d = {"common": ["tolerance | 1; 2; 5; 10 | % | 1 | K |"],
                 "parts": [
                     {"part_number": "", "family_row": "0603 / X7R / 50 V", "ordering_page": 1, "values": [
                         "size | 0603 | | 2 | 0603 |", "dielectric | X7R | | 2 | X7R |",
                         "rated_voltage | 50 | V | 2 | 50V |", "capacitance | 150..470000 | pF | 2 | 150pF~470nF |"]},
                     {"part_number": "", "family_row": "0603 / X7R / 100 V", "ordering_page": 1, "values": [
                         "size | 0603 | | 2 | 0603 |", "rated_voltage | 100 | V | 2 | 100V |",
                         "capacitance | 225..1.6 | pF | 2 | 225~1.6 |"]},     # typo in the source: min > max
                     {"part_number": "", "family_row": "0805 / NPO / 50 V", "values": [
                         "rated_voltage | 63 | V | 2 | 63V |"]}]}                # invented: not on the page
        return LLMAnswer(d, 2.0, 5000, 300)


def setup_dirs():
    root = Path(tempfile.mkdtemp())
    md = root / "markdowns"
    for rel, text in {"goodark/2n7000.md": MOSFET_MD, "viking/mlcc.md": MLCC_MD,
                      "goodark/other.md": "<!-- page: 1 -->\nSome relay"}.items():
        (md / rel).parent.mkdir(parents=True, exist_ok=True)
        (md / rel).write_text(text, encoding="utf-8")
    common.CACHE_DIR = ex.CACHE_DIR = root / "index_cache"
    sl.SPEC_LIST_DIR = ex.SPEC_LIST_DIR = root / "spec_lists"
    sl.save_spec_list(MOSFET_LIST, sl.spec_list_path("mosfet_n"))
    sl.save_spec_list(MLCC_LIST, sl.spec_list_path("ceramic_capacitor_mlcc"))
    types = {"goodark/2n7000.md": "mosfet_n", "viking/mlcc.md": "ceramic_capacitor_mlcc", "goodark/other.md": "relay_x"}
    for ds in common.find_datasheets(md):  # what `classify report` writes
        write_cache(ds.cache_path, {"file": ds.rel, "supplier": ds.supplier, "file_hash": ds.file_hash,
                                    "type": types[ds.rel]})
    return root, md


def run_args(md, **kw):
    base = dict(input=str(md), supplier=None, types=[], drafts=False, limit=None, workers=2, max_chars=48000,
                force=False, verbose=True)
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------ values.py
def test_values_parse_convert_and_check():
    assert parse_value("-55 to +150") == {"min": -55, "max": 150}
    assert parse_value("150p..470n") == {"min": 1.5e-10, "max": 4.7e-07}
    assert parse_value("1; 2; 5") == {"set": [1, 2, 5]}
    assert parse_value("0603", numeric=False) == {"value": "0603"}
    assert convert(200, "mA", "A") == (0.2, True) and convert(1, "nF", "pF") == (1000, True)
    assert convert(5, "V", "A") == (5, False)
    assert parse_conditions("VGS=10V, ID=500mA") == {"vgs": "10V", "id": "500mA"}
    key, v = parse_line("id_max | 200 | mA | 1 | 200 | TA=25C", {"unit": "A", "stat": "max"})
    assert key == "id_max" and v["value"] == 0.2 and v["unit"] == "A" and v["conditions"] == {"ta": "25C"}
    _, v = parse_line("x | 5 | V | 1 | 5 |", {"unit": "A"})
    assert v["issues"] == ["unit 'V' does not convert to 'A'"] and v["unit"] == "V"
    assert parse_line("x | - | V | 1 | |", {"unit": "V"}) is None
    assert raw_on_page("-55 to +150", "| TJ | -55 to +150 | °C |")
    assert raw_on_page("150pF~470nF", "| 150pF ~ 470nF |")
    assert not raw_on_page("5", "TJ -55 to 150") and raw_on_page("5", "| RDS(on) | 5 | ohm |")
    assert raw_on_page("K", "03 K T B") and not raw_on_page("K", "Package outline")
    assert check_value({"min": 225, "max": 1.6, "page": 1, "raw": "225~1.6"}, {1: "225~1.6"})["issues"] == ["min > max"]


# ------------------------------------------------------------------ extract()
def test_extract_single_part_converts_checks_and_drops_unknown_keys():
    r = ex.extract(MOSFET_MD, MOSFET_LIST, FakeExtractor())
    assert len(r["parts"]) == 1
    s = r["parts"][0]["specs"]
    assert s["id_max"]["value"] == 0.2 and s["id_max"]["unit"] == "A"
    assert s["rds_on_max@vgs_10v"]["page"] == 2 and s["rds_on_max@vgs_10v"]["page_given"] == 1
    assert s["rds_on_max@vgs_10v"]["conditions"] == {"vgs": "10V", "id": "500mA"}
    assert s["operating_temp_range"] == {"min": -55, "max": 150, "unit": "degC", "stat": "range", "conditions": {},
                                         "page": 1, "raw": "-55 to +150", "verified": True}  # from "common"
    assert s["package"]["value"] == "TO-92" and "unit" not in s["package"]
    assert all(v["verified"] for v in s.values())
    assert r["stats"]["unknown_keys"] == {"qg_total": 1, "vgs_th": 1} and r["stats"]["coverage"] == 1.0


def test_extract_family_rows_flag_typos_and_invented_values():
    r = ex.extract(MLCC_MD, MLCC_LIST, FakeExtractor())
    rows = {p["family_row"]: p for p in r["parts"]}
    assert list(rows) == ["0603 / X7R / 50 V", "0603 / X7R / 100 V", "0805 / NPO / 50 V"]
    a = rows["0603 / X7R / 50 V"]
    assert a["part_number"] == "" and a["ordering_page"] == 1
    assert a["specs"]["capacitance"]["min"] == 150 and a["specs"]["capacitance"]["max"] == 470000
    assert a["specs"]["tolerance"]["set"] == [1, 2, 5, 10] and a["specs"]["size"]["value"] == "0603"
    assert rows["0603 / X7R / 100 V"]["specs"]["capacitance"]["issues"] == ["min > max"]
    assert rows["0805 / NPO / 50 V"]["specs"]["rated_voltage"]["issues"] == ["raw text not found on the page"]
    assert r["stats"]["unverified"] == 2  # min > max, invented 63 V
    assert a["specs"]["tolerance"]["verified"]  # raw "K" is a whole word on page 1 ("03 K T B")


def test_spec_pages_drop_tape_and_reel_pages():
    user = FakeExtractor()
    ex.extract(MOSFET_MD, MOSFET_LIST, user)
    assert "carrier tape" not in user.calls[0] and "vds_max | VDS max | V | max" in user.calls[0]


# ------------------------------------------------------------------ commands
def test_run_resumes_skips_drafts_and_reextracts_on_new_spec_list_version():
    root, md = setup_dirs()
    llm = FakeExtractor()
    assert ex.cmd_run(run_args(md), llm) == 0
    assert len(llm.calls) == 1                       # the MLCC list is a draft: skipped without --drafts
    c = common.read_cache(next(d.cache_path for d in common.find_datasheets(md) if "2n7000" in d.rel))
    assert c["extraction"]["type"] == "mosfet_n" and c["extraction"]["spec_list_version"] == 1
    assert c["type"] == "mosfet_n"                   # what Step 0 wrote is kept

    llm = FakeExtractor(fail_on=["Ceramic"])
    assert ex.cmd_run(run_args(md, drafts=True), llm) == 1   # the MLCC fails, the MOSFET is already done
    assert len(llm.calls) == 1
    c = common.read_cache(next(d.cache_path for d in common.find_datasheets(md) if "mlcc" in d.rel))
    assert c["extraction_error"]["error"] == "answer is not JSON" and "extraction" not in c

    llm = FakeExtractor()
    assert ex.cmd_run(run_args(md, drafts=True), llm) == 0
    assert len(llm.calls) == 1                       # only the failed one again
    c = common.read_cache(next(d.cache_path for d in common.find_datasheets(md) if "mlcc" in d.rel))
    assert "extraction_error" not in c and len(c["extraction"]["parts"]) == 3

    sl.save_spec_list(dict(MOSFET_LIST, version=2), sl.spec_list_path("mosfet_n"))
    llm = FakeExtractor()
    ex.cmd_run(run_args(md, drafts=True), llm)
    assert len(llm.calls) == 1 and "2N7000" in llm.calls[0]  # new spec-list version: that type again

    assert ex.cmd_report(run_args(md)) == 0
    assert (root / "index_cache" / "extract_report.csv").read_text().count("\n") == 3
    assert ex.cmd_show(SimpleNamespace(target="goodark/2n7000.md", input=str(md), limit=20)) == 0
    assert ex.cmd_show(SimpleNamespace(target="nothing.md", input=str(md), limit=20)) == 1
    hit = ex.find_cache("goodark/2n7000.md", md)
    for form in ("/goodark/2n7000.md", "markdowns/goodark/2n7000.md", str(md / "goodark/2n7000.md"),
                 hit.stem[:8]):
        assert ex.find_cache(form, md) == hit, form
    (md / "goodark/2n7000.md").write_text(MOSFET_MD + "changed", encoding="utf-8")  # new hash, not re-run yet
    assert ex.find_cache("goodark/2n7000.md", md) == hit                         # found by the stored file name


# ------------------------------------------------------------------ load_cache (Postgres)
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to an empty Postgres database")
def test_load_cache_syncs_catalog_parts():
    from sqlalchemy import text
    root, md = setup_dirs()
    ex.cmd_run(run_args(md, drafts=True), FakeExtractor())
    engine = load_cache.get_engine(os.environ["TEST_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS catalog_parts"))
    load_cache.ensure_tables(engine)
    cache = root / "index_cache"
    try:
        s = load_cache.sync(engine, cache, dry_run=True)
        assert s["loaded"] == 2 and s["rows_added"] == 4
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM catalog_parts")).scalar() == 0

        s = load_cache.sync(engine, cache)
        assert s == {"datasheets": 2, "stale": 0, "unchanged": 0, "loaded": 2, "rows_added": 4, "removed_datasheets": 0,
                     "rows_removed": 0}
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT type, part_number, family_row, specs, unverified FROM catalog_parts "
                                     "ORDER BY id")).all()
            assert [r.type for r in rows] == ["mosfet_n"] + ["ceramic_capacitor_mlcc"] * 3
            assert rows[0].part_number == "2N7000" and rows[0].specs["vds_max"]["value"] == 60
            assert rows[2].unverified == 1           # min > max
            # the shortlist query of plan section 6: narrow by type, then compare in Python
            hit = conn.execute(text("SELECT part_number FROM catalog_parts WHERE type = 'mosfet_n' "
                                    "AND (specs->'vds_max'->>'value')::float >= 60")).scalar()
            assert hit == "2N7000"

        assert load_cache.sync(engine, cache)["unchanged"] == 2      # nothing changed: nothing written

        sl.save_spec_list(dict(MOSFET_LIST, version=3), sl.spec_list_path("mosfet_n"))
        ex.cmd_run(run_args(md, drafts=True, types=["mosfet_n"]), FakeExtractor())  # re-extracted
        s = load_cache.sync(engine, cache)
        assert s["loaded"] == 1 and s["rows_removed"] == 1 and s["rows_added"] == 1

        (md / "goodark/2n7000.md").write_text(MOSFET_MD + "\nrev B", encoding="utf-8")  # reconverted markdown
        s = load_cache.sync(engine, cache, markdown_dir=md)
        assert s["stale"] == 1 and s["removed_datasheets"] == 1 and s["rows_removed"] == 1
        (md / "goodark/2n7000.md").write_text(MOSFET_MD, encoding="utf-8")
        load_cache.sync(engine, cache, markdown_dir=md)                 # back: loaded again

        next(p for p in cache.glob("viking/*.json")).unlink()          # datasheet gone from the cache
        s = load_cache.sync(engine, cache)
        assert s["removed_datasheets"] == 1 and s["rows_removed"] == 3
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM catalog_parts")).scalar() == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS catalog_parts"))
