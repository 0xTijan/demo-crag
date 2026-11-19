"""Where the converter writes each Markdown (dataPipeline/convert/common.py), without Docling or a database.

datasheets/goodark/x.pdf must become markdowns/goodark/x.md, also when only part of datasheets/ is converted.
Run: python tests/test_convert_paths.py   (or pytest)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataPipeline.convert import common  # noqa: E402
from dataPipeline.convert.common import Conversion, find_pdfs, markdown_path, process_pdf  # noqa: E402


def tree():
    root = Path(tempfile.mkdtemp())
    ds = root / "datasheets"
    for rel in ("goodark/x.pdf", "goodark/sub/y.PDF", "novosns/MT6835_Rev.1.3.pdf", "novosns/notes.txt"):
        (ds / rel).parent.mkdir(parents=True, exist_ok=True)
        (ds / rel).write_bytes(b"%PDF-1.7 " + rel.encode())
    return root, ds, root / "markdowns"


class FakeTracker:
    def __init__(self):
        self.rows = []

    def already_converted(self, rel, file_hash):
        return False

    def conversion_reason(self, rel, file_hash, force):
        return "new"

    def upsert(self, params):
        self.rows.append(params)

    def record_failure(self, params, stage, exc):
        raise AssertionError(f"{stage}: {exc}")

    def log_error(self, *a):
        pass


def fake_convert(pdf):
    return Conversion(markdown=f"converted {pdf.name}\n", pages=1, tables=0)


def test_markdown_mirrors_the_supplier_subfolder():
    _, ds, md = tree()
    rel, path = markdown_path(ds / "goodark" / "x.pdf", ds, md)
    assert rel == "goodark/x.pdf" and path == md / "goodark" / "x.md"
    assert markdown_path(ds / "goodark" / "sub" / "y.PDF", ds, md)[1] == md / "goodark" / "sub" / "y.md"
    assert markdown_path(ds / "novosns" / "MT6835_Rev.1.3.pdf", ds, md)[1].name == "MT6835_Rev.1.3.md"


def test_find_pdfs_any_case_and_filtered_to_one_supplier():
    _, ds, _ = tree()
    every = [p.relative_to(ds.resolve()).as_posix() for p in find_pdfs(ds)]
    assert every == ["goodark/sub/y.PDF", "goodark/x.pdf", "novosns/MT6835_Rev.1.3.pdf"]   # .PDF too, no .txt
    only = [p.relative_to(ds.resolve()).as_posix() for p in find_pdfs(ds, [ds / "goodark"])]
    assert only == ["goodark/sub/y.PDF", "goodark/x.pdf"]
    assert find_pdfs(ds, [ds / "goodark" / "x.pdf"]) == [(ds / "goodark" / "x.pdf").resolve()]


def test_converting_one_supplier_keeps_its_folder():
    root, ds, md = tree()
    old_root, common.ROOT = common.ROOT, root
    tracker = FakeTracker()
    try:
        for pdf in find_pdfs(ds, [ds / "goodark"]):                # e.g. docling.py datasheets/goodark
            assert process_pdf(tracker, pdf, md, fake_convert, False, ds) == "converted"
    finally:
        common.ROOT = old_root
    assert (md / "goodark" / "x.md").read_text() == "converted x.pdf\n"       # not markdowns/x.md
    assert (md / "goodark" / "sub" / "y.md").exists()
    assert not (md / "x.md").exists()
    assert {r["pdf_path"] for r in tracker.rows} == {"goodark/x.pdf", "goodark/sub/y.PDF"}
    assert {r["markdown_path"] for r in tracker.rows} == {"markdowns/goodark/x.md", "markdowns/goodark/sub/y.md"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
