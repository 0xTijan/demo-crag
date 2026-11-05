import sys
from pathlib import Path

# This file is named `docling.py`, so when run as a script its folder lands on
# sys.path and `import docling` would import this file instead of the library.
# Drop the script folder from sys.path before importing.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != SCRIPT_DIR]

from docling.datamodel.base_models import InputFormat  # noqa: E402
from docling.datamodel.pipeline_options import (  # noqa: E402
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: E402

ROOT = SCRIPT_DIR.parents[1]
INPUT_PDF = ROOT / "reports" / "test.pdf"
OUTPUT_MD = ROOT / "markdowns" / "test.md"


def build_converter() -> DocumentConverter:
    opts = PdfPipelineOptions()
    opts.do_ocr = True                     # handles scanned pages / image-only tables
    opts.do_table_structure = True         # run TableFormer table-structure model
    opts.table_structure_options.mode = TableFormerMode.FAST  # slower, better on complex/merged cells
    opts.table_structure_options.do_cell_matching = True          # map cells back to the PDF text layer

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def main() -> None:
    if not INPUT_PDF.exists():
        sys.exit(f"Input not found: {INPUT_PDF}")

    result = build_converter().convert(INPUT_PDF)
    doc = result.document

    markdown = doc.export_to_markdown()

    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(markdown, encoding="utf-8")

    print(f"Converted {INPUT_PDF.name} -> {OUTPUT_MD}")
    print(f"Pages: {len(doc.pages)}, tables: {len(doc.tables)}")


if __name__ == "__main__":
    main()
