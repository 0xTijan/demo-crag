"""PDF -> Markdown with Docling, with exact ruled tables from the PDF itself.

Usage:  python dataPipeline/convert/docling.py [--force]

Per PDF (bookkeeping, hashing and DB tracking are in common.py):
1. Docling converts the PDF (layout model, OCR, TableFormer table structure).
2. Every table drawn with lines is rebuilt from the PDF's vector lines and text
   layer (ruled_tables.py) and replaces Docling's version: Docling sometimes
   crops tables (lost columns) and misreads merged cells. Merged cells are
   repeated in every row/column they cover; nested tables are folded into their
   cell. Tables without lines and scanned pages keep Docling's result.
3. Pages where Docling found (almost) no text are re-read by a local vision
   model (vision_ocr.py).
4. The Markdown is written to markdowns/ with a `<!-- page: N -->` marker per page.
"""
import logging
import re
import sys
import time
from importlib.metadata import version as pkg_version
from pathlib import Path

# This file is named `docling.py`, so when run as a script its folder lands on
# sys.path and `import docling` would import this file instead of the library.
# Drop the script folder from sys.path, and make the repo root importable.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != SCRIPT_DIR]
sys.path.insert(0, str(SCRIPT_DIR.parents[1]))

from docling.datamodel.base_models import InputFormat  # noqa: E402
from docling.datamodel.pipeline_options import (  # noqa: E402
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: E402

from dataPipeline.convert import ruled_tables, vision_ocr  # noqa: E402
from dataPipeline.convert.common import ROOT, Conversion, log, run_pipeline  # noqa: E402

CONVERTER = "docling"
OUTPUT_DIR = ROOT / "markdowns"
OCR_EMPTY_MARKER = "returned empty result"  # RapidOCR log text (hint only)
# list markers Docling leaves at the start of table cells ("－DRGR664")
BULLET_PREFIX = re.compile(r"^[－•·▪●◦]\s*")


def build_converter() -> DocumentConverter:
    """Build the Docling converter used for every PDF.

    Constructing it loads the ML models, which is slow, so it is created once per
    run and only when a file really needs converting (see convert()).
    """
    opts = PdfPipelineOptions()
    opts.do_ocr = True                     # handles scanned pages / image-only tables
    opts.do_table_structure = True         # run TableFormer table-structure model
    opts.table_structure_options.mode = TableFormerMode.ACCURATE  # slower, better on complex/merged cells
    opts.table_structure_options.do_cell_matching = True          # map cells back to the PDF text layer

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


class OcrEmptyCounter(logging.Handler):
    """Counts log records saying RapidOCR returned an empty result.

    Attached to the root logger while a PDF is converted. Only a hint (it also
    fires on harmless blank regions); the page text-length check decides whether
    the vision model runs.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)  # DEBUG = see every record
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if OCR_EMPTY_MARKER in record.getMessage():
            self.count += 1


def clean_docling_tables(doc) -> None:
    """Strip list markers Docling keeps at the start of table cells."""
    for table in doc.tables:
        for cell in table.data.table_cells:
            cell.text = BULLET_PREFIX.sub("", cell.text)


_converter = None  # lazy shared Docling converter; an all-skipped run loads no models


def convert(pdf: Path) -> Conversion:
    """Convert one PDF with Docling + ruled-table override + vision fallback."""
    global _converter
    warnings = []
    if _converter is None:
        log("  loading Docling models (first conversion this run) ...")
        t0 = time.time()
        _converter = build_converter()
        log(f"  models ready in {time.time() - t0:.1f}s")

    log("  converting with Docling (OCR + table structure) ...")
    t0 = time.time()
    counter = OcrEmptyCounter()  # count RapidOCR "empty result" messages for this PDF only
    logging.getLogger().addHandler(counter)
    try:
        doc = _converter.convert(pdf).document
    finally:
        logging.getLogger().removeHandler(counter)
    log(f"  Docling done in {time.time() - t0:.1f}s: {len(doc.pages)} pages, {len(doc.tables)} tables")

    # Replace Docling's tables with the exact ruled tables from the PDF. Never
    # fatal: on any error the file keeps Docling's own tables.
    try:
        t0 = time.time()
        found = ruled_tables.extract_tables(pdf)
        applied = ruled_tables.apply_to_docling_document(doc, found)
        log(f"  ruled tables: {applied} rebuilt from PDF lines in {time.time() - t0:.1f}s "
            f"({len(doc.tables) - applied} left from Docling)")
    except Exception as exc:  # noqa: BLE001
        log(f"  ruled-table extraction failed, keeping Docling tables: {exc}")
        warnings.append(("ruled_tables", exc))
    clean_docling_tables(doc)

    # Per-page Markdown (escape_html=False keeps "&" and "<" as plain text).
    pages = {n: doc.export_to_markdown(page_no=n, escape_html=False) for n in sorted(doc.pages)}
    log(f"  RapidOCR empty-result messages: {counter.count}")
    pages, fallback_pages, fallback_model, fb_warnings = vision_ocr.apply_fallback(pdf, pages)

    return Conversion(
        markdown=vision_ocr.join_pages(pages),
        pages=len(doc.pages),
        tables=len(doc.tables),
        ocr_fallback_pages=fallback_pages,
        ocr_fallback_model=fallback_model,
        ocr_empty_events=counter.count,
        warnings=warnings + fb_warnings,
    )


if __name__ == "__main__":
    run_pipeline(CONVERTER, pkg_version("docling"), OUTPUT_DIR, convert,
                 "Convert new/changed PDFs in reports/ to Markdown with Docling.")
