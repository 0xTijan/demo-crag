"""Spec lists per component type (dataPipeline/index/spec_lists.py), with fake LLMs.

No Ollama, no network: the fake inventory reads "name | symbol | unit" lines from the datasheet text, and the
fake merge groups items by symbol.
Run: python tests/test_index_spec_lists.py   (or pytest)
"""
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.index import classify as cl  # noqa: E402
from dataPipeline.index import common  # noqa: E402
from dataPipeline.index import spec_lists as sl  # noqa: E402
from dataPipeline.index.common import LLMAnswer, LLMError  # noqa: E402


def mosfet(name: str, specs: list[str], extra_pages: str = "") -> str:
    rows = "\n".join(f"| {s} |" for s in specs)
    return (f"<!-- page: 1 -->\n{name} N-Channel MOSFET\n<!-- page: 2 -->\n## Electrical characteristics\n"
            f"| Parameter | Symbol | Unit |\n|---|---|---|\n{rows}\n{extra_pages}")


VDS, ID, RDS, VTH, PKG = ("Drain-Source Voltage | VDS | V", "Continuous Drain Current | ID | A",
                          "Drain-Source On-Resistance | RDS(ON) | Ω", "Gate Threshold Voltage | VGS(th) | V",
                          "Package | PKG | ")
DOCS = {
    "goodark/a.md": mosfet("2N7000", [VDS, ID, RDS, VTH, PKG, "Gate charge | QG | nC"]),
    "goodark/b.md": mosfet("AM3400", [VDS, ID, RDS, PKG]),
    "novosns/c.md": mosfet("NS60N", ["Breakdown Voltage | VDS | V", ID, RDS, VTH, PKG],
                           "<!-- page: 3 -->\n## Tape and reel\n| reel | 7 inch |\n| SECRET | x |"),
    "viking/d.md": mosfet("VK60", [VDS, ID, PKG, "Avalanche energy | EAS | mJ"]),
    "viking/e.md": "<!-- page: 1 -->\nMultilayer ceramic capacitor\n| Capacitance | C | F |",
}
TYPES = {"goodark/a.md": "mosfet_n", "goodark/b.md": "mosfet_n", "novosns/c.md": "mosfet_n",
         "viking/d.md": "mosfet_n", "viking/e.md": "mlcc"}
FAMILY = {"goodark/b.md"}


class FakeInventory:
    model = "fake-inventory"

    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, system, user):
        self.calls.append(user)
        if any(f in user for f in self.fail_on):
            raise LLMError("model crashed")
        specs = []
        for line in user.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[0] not in ("Parameter", "---", "reel", "SECRET"):
                specs.append({"name": cells[0] + "1", "symbol": cells[1], "unit": cells[2], "stat": "max"})
        specs.append(specs[0] if specs else {"name": "x"})  # a duplicate, dropped by the code
        return LLMAnswer({"specs": specs}, 1.0, 2000, 300)


class FakeMerge:
    """Groups items by symbol; gives keys, rules and one invalid member id."""
    model = "fake-merge"
    RULES = {"VDS": ("vds_max", ">=", True), "ID": ("id_max", "≥", True), "RDS(ON)": ("RDS on max @ VGS 10V", "LTE", False),
             "VGS(th)": ("vgs_th", "approx", False), "PKG": ("package", "==", False), "QG": ("qg", "<=", False),
             "EAS": ("eas", "", False)}

    def __init__(self):
        self.calls = []

    def __call__(self, system, user):
        self.calls.append(user)
        groups = {}
        for line in user.splitlines():
            m = re.match(r"(D\d+\.\d+) \| [^|]* \| ([^|]*) \|", line)
            if m:
                groups.setdefault(m.group(2).strip(), []).append(m.group(1))
        specs = []
        for sym, members in groups.items():
            key, rule, crit = self.RULES[sym]
            specs.append({"key": key, "label": sym, "unit": "V", "rule": rule, "critical": crit,
                          "members": members + (["D99.1"] if sym == "VDS" else [])})
        return LLMAnswer({"specs": specs, "must_match": ["vds_max", "package", "nonexistent"],
                          "visual_check": "pinout"}, 3.0, 5000, 800)


def setup_dirs():
    root = Path(tempfile.mkdtemp())
    md = root / "markdowns" / "suppliers"
    common.CACHE_DIR = cl.CACHE_DIR = root / "index_cache"
    cl.TYPE_LIST = root / "index_cache" / "type_list.yaml"
    sl.SPEC_LIST_DIR = root / "spec_lists"
    for rel, text in DOCS.items():
        (md / rel).parent.mkdir(parents=True, exist_ok=True)
        (md / rel).write_text(text, encoding="utf-8")
    for ds in common.find_datasheets(md):  # what Step 0 (`classify report`) leaves in the cache
        common.write_cache(ds.cache_path, {"file": ds.rel, "supplier": ds.supplier, "type": TYPES[ds.rel],
                                           "classification": {"family": ds.rel in FAMILY, "confidence": "high"}})
    cl.TYPE_LIST.write_text("types:\n  mosfet_n:\n    name: N-channel MOSFET\n    datasheets: 4\n"
                            "  mlcc:\n    name: MLCC\n    datasheets: 1\n", encoding="utf-8")
    return md


def draft_args(md, types, **kw):
    base = dict(types=types, top=None, all=False, min_datasheets=1, samples=10, min_share=0.5, max_chars=24000, workers=2, input=str(md),
                supplier=None, force=False, force_inventory=False, verbose=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_spec_pages_skip_packing_pages_and_images():
    text = sl.spec_pages(DOCS["novosns/c.md"] + "\n![x](data:image/png;base64,AAAA)")
    assert "Electrical" in text and "N-Channel" in text
    assert "SECRET" not in text and "base64" not in text
    ordering = "<!-- page: 1 -->\nX\n<!-- page: 2 -->\nTape and reel\nOrdering information\n| PN | pkg |"
    assert "Ordering" in sl.spec_pages(ordering)            # packing page that also holds ordering info: kept


def test_inventory_cleans_items():
    inv = sl.inventory(DOCS["goodark/a.md"], FakeInventory())
    names = [i["name"] for i in inv["items"]]
    assert names[0] == "Drain-Source Voltage"                # footnote number removed
    assert len(names) == len(set(names)) == 6                # duplicate dropped
    assert all(i["stat"] in sl.STATS for i in inv["items"])


def test_pick_samples_spreads_over_suppliers_and_families():
    md = setup_dirs()
    cands = sl.datasheets_of_type(md, "mosfet_n")
    assert len(cands) == 4
    picked = [ds.rel for ds, _ in sl.pick_samples(cands, 3)]
    assert len({p.split("/")[0] for p in picked}) == 3       # one per supplier first
    assert picked[0].startswith("goodark/")                  # biggest supplier first
    assert picked == [ds.rel for ds, _ in sl.pick_samples(cands, 3)]  # deterministic


def test_norm_key_and_rule():
    assert sl.norm_key("R(DS)on max @ VGS=10V") == "r_ds_on_max@vgs_10v"
    assert sl.norm_key("Package") == "package"
    assert [sl.norm_rule(r) for r in ("≥", "LTE", "approx", "==", "", "bogus")] == [">=", "<=", "≈", "=", "info", "info"]


def test_draft_writes_spec_list_with_common_and_rare_specs():
    md = setup_dirs()
    inv, merge = FakeInventory(), FakeMerge()
    assert sl.cmd_draft(draft_args(md, ["mosfet_n"]), inv, merge) == 0
    assert len(inv.calls) == 4 and len(merge.calls) == 1
    assert all("SECRET" not in c for c in inv.calls)
    out = sl.load_spec_list(sl.spec_list_path("mosfet_n"))
    assert out["status"] == "draft" and out["version"] == 1 and out["type"] == "mosfet_n"
    specs = out["specs"]
    # found in >= 2 of 4 samples: common; QG and EAS (1 of 4): rare
    assert set(specs) == {"vds_max", "id_max", "rds_on_max@vgs_10v", "vgs_th", "package"}
    assert set(out["rare_specs"]) == {"qg", "eas"}
    assert specs["vds_max"]["found_in"] == "4/4" and specs["vgs_th"]["found_in"] == "2/4"
    assert list(specs)[-1] == "vgs_th"                       # ordered by how often they were found
    assert specs["id_max"]["rule"] == ">=" and specs["rds_on_max@vgs_10v"]["rule"] == "<="
    assert specs["vgs_th"]["rule"] == "≈" and out["rare_specs"]["eas"]["rule"] == "info"
    assert "Breakdown Voltage" in specs["vds_max"]["aliases"]  # synonym from another supplier
    assert out["must_match"] == ["package"] and out["visual_check"] == "pinout"  # vds_max (>=) is not must_match
    assert specs["package"]["critical"] is True and specs["package"]["rule"] == "="
    assert sl.validate(out)[0] == []
    # the inventories are cached: a second draft calls only the merge
    inv2, merge2 = FakeInventory(), FakeMerge()
    assert sl.cmd_draft(draft_args(md, ["mosfet_n"]), inv2, merge2) == 0
    assert inv2.calls == [] and len(merge2.calls) == 1


def test_existing_lists_are_not_overwritten():
    md = setup_dirs()
    sl.cmd_draft(draft_args(md, ["mosfet_n"]), FakeInventory(), FakeMerge())
    path = sl.spec_list_path("mosfet_n")
    reviewed = path.read_text().replace("status: draft", "status: reviewed")
    path.write_text(reviewed)
    sl.cmd_draft(draft_args(md, ["mosfet_n"], force=True), FakeInventory(), FakeMerge())
    assert path.read_text() == reviewed                       # reviewed: never replaced, even with --force
    assert path.with_name("mosfet_n.draft.yaml").exists()


def test_draft_survives_a_failing_sample_and_top():
    md = setup_dirs()
    assert sl.cmd_draft(draft_args(md, [], top=1), FakeInventory(fail_on=["VK60"]), FakeMerge()) == 0
    out = sl.load_spec_list(sl.spec_list_path("mosfet_n"))   # --top 1 = the type with most datasheets
    assert len(out["drafted"]["samples"]) == 3
    assert sl.cmd_draft(draft_args(md, ["nonexistent"]), FakeInventory(), FakeMerge()) == 1


def test_validate_and_check_catch_mistakes():
    md = setup_dirs()
    sl.cmd_draft(draft_args(md, ["mosfet_n"]), FakeInventory(), FakeMerge())
    good = sl.load_spec_list(sl.spec_list_path("mosfet_n"))
    assert sl.cmd_check(SimpleNamespace(types=[], verbose=True)) == 0
    bad = dict(good, version="one", must_match=["missing"],
               specs=dict(good["specs"], **{"Bad Key": {"label": "x", "rule": "≈", "stat": "most"}}))
    errors, warnings = sl.validate(bad)
    assert any("version" in e for e in errors) and any("must_match" in e for e in errors)
    assert any("Bad Key" in e and "snake_case" in e for e in errors) and any("stat" in e for e in errors)
    assert any("no unit" in w for w in warnings)
    sl.save_spec_list(bad, sl.spec_list_path("mosfet_n"))
    assert sl.cmd_check(SimpleNamespace(types=["mosfet_n"], verbose=False)) == 1
    assert sl.cmd_list(SimpleNamespace()) == 0


def test_all_drafts_every_type_and_resumes():
    md = setup_dirs()
    inv = FakeInventory()
    # mlcc has 1 datasheet: skipped by --min-datasheets 2; mosfet_n drafted
    assert sl.cmd_draft(draft_args(md, [], all=True, min_datasheets=2), inv, FakeMerge()) == 0
    assert sl.spec_list_path("mosfet_n").exists() and not sl.spec_list_path("mlcc").exists()
    # rerun (e.g. after Ctrl+C): types with a list are skipped, no LLM call, no .draft.yaml copies
    inv2, merge2 = FakeInventory(), FakeMerge()
    assert sl.cmd_draft(draft_args(md, [], all=True, min_datasheets=2), inv2, merge2) == 0
    assert inv2.calls == [] and merge2.calls == []
    assert not list(sl.SPEC_LIST_DIR.glob("*.draft.yaml"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


def test_package_is_always_in_the_spec_list():
    specs, rare = {"vrrm_max": {"rule": ">="}}, {"package": {"label": "Case", "rule": "info", "found_in": "3/10"}}
    sl.ensure_package(specs, rare)
    assert specs["package"]["label"] == "Case" and specs["package"]["rule"] == "=" and "package" not in rare
    specs, rare = {"vrrm_max": {"rule": ">="}}, {}
    sl.ensure_package(specs, rare)
    assert specs["package"]["found_in"] == "added" and specs["package"]["critical"] is True
    good = {"type": "x", "name": "x y", "version": 1, "status": "draft", "must_match": ["vrrm_max"],
            "specs": {"vrrm_max": {"label": "VRRM", "unit": "V", "rule": ">=", "critical": True}}}
    errors, warnings = sl.validate(good)
    assert not errors and any("must_match" in w for w in warnings) and any("package" in w for w in warnings)
