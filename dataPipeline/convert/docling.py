from dotenv import load_dotenv
load_dotenv()
import argparse
import hashlib
import io
import logging
import os
import re
import sys
import time
import traceback
from importlib.metadata import version as pkg_version
from pathlib import Path

# This file is named `docling.py`, so when run as a script its folder lands on
# sys.path and `import docling` would import this file instead of the library.
# Drop the script folder from sys.path before importing.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != SCRIPT_DIR]

# Make the repo root importable so helpers.config (model settings) can be reused.
sys.path.insert(0, str(SCRIPT_DIR.parents[1]))

from docling.datamodel.base_models import InputFormat  # noqa: E402
from docling.datamodel.pipeline_options import (  # noqa: E402
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from helpers.config import model_settings  # noqa: E402

ROOT = SCRIPT_DIR.parents[1]
INPUT_DIR = ROOT / "reports"
OUTPUT_DIR = ROOT / "markdowns"

# --- Vision-LLM OCR fallback -------------------------------------------------
# After Docling finishes, pages whose extracted text is shorter than this many
# characters (markdown/whitespace symbols ignored) are treated as "OCR came back
# empty" and are re-read by a local vision model through Ollama.
MIN_PAGE_CHARS = int(os.getenv("OCR_FALLBACK_MIN_PAGE_CHARS", "100"))
RENDER_SCALE = 2.0  # page render scale for the vision model (~144 dpi)
OCR_EMPTY_MARKER = "returned empty result"  # RapidOCR log text (hint only)
FALLBACK_PROMPT = (
    "Transcribe this page of a financial report into Markdown. Keep headings, "
    "paragraphs and lists. Render every table as a Markdown table, preserving all "
    "rows, columns and numbers exactly as printed. When a cell spans several rows or "
    "columns, repeat that cell's text in every row and column it covers (Markdown "
    "has no merged cells). Do not summarise, translate or "
    "invent anything. Write [unreadable] where text cannot be read. "
    "Output only the Markdown."
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)

DDL = [
    """
    CREATE TABLE IF NOT EXISTS report_files (
        file_id           SERIAL PRIMARY KEY,
        pdf_path          TEXT        NOT NULL,
        file_hash         CHAR(64)    NOT NULL,
        file_size         BIGINT,
        markdown_path     TEXT,
        status            TEXT        NOT NULL CHECK (status IN ('converted','failed')),
        pages             INT,
        tables            INT,
        converter         TEXT        DEFAULT 'docling',
        converter_version TEXT,
        error             TEXT,
        converted_at      TIMESTAMPTZ DEFAULT now(),
        UNIQUE (pdf_path, file_hash)
    )
    """,
    # Columns added after the first release; ADD COLUMN IF NOT EXISTS upgrades
    # existing databases because CREATE TABLE IF NOT EXISTS leaves them untouched.
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_fallback_pages INT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_fallback_model TEXT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_empty_events INT",
    """
    CREATE INDEX IF NOT EXISTS report_files_path_idx
        ON report_files (pdf_path, converted_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS report_file_errors (
        error_id    SERIAL PRIMARY KEY,
        pdf_path    TEXT NOT NULL,
        file_hash   CHAR(64),
        stage       TEXT NOT NULL,
        error       TEXT NOT NULL,
        traceback   TEXT,
        occurred_at TIMESTAMPTZ DEFAULT now()
    )
    """,
]

UPSERT = text(
    """
    INSERT INTO report_files
        (pdf_path, file_hash, file_size, markdown_path, status, pages, tables,
         converter_version, error, ocr_fallback_pages, ocr_fallback_model,
         ocr_empty_events, converted_at)
    VALUES
        (:pdf_path, :file_hash, :file_size, :markdown_path, :status, :pages, :tables,
         :converter_version, :error, :ocr_fallback_pages, :ocr_fallback_model,
         :ocr_empty_events, now())
    ON CONFLICT (pdf_path, file_hash) DO UPDATE SET
        file_size = EXCLUDED.file_size,
        markdown_path = EXCLUDED.markdown_path,
        status = EXCLUDED.status,
        pages = EXCLUDED.pages,
        tables = EXCLUDED.tables,
        converter_version = EXCLUDED.converter_version,
        error = EXCLUDED.error,
        ocr_fallback_pages = EXCLUDED.ocr_fallback_pages,
        ocr_fallback_model = EXCLUDED.ocr_fallback_model,
        ocr_empty_events = EXCLUDED.ocr_empty_events,
        converted_at = now()
    """
)

LOG_ERROR = text(
    """
    INSERT INTO report_file_errors (pdf_path, file_hash, stage, error, traceback)
    VALUES (:pdf_path, :file_hash, :stage, :error, :traceback)
    """
)


def build_converter() -> DocumentConverter:
    opts = PdfPipelineOptions()
    opts.do_ocr = True                     # handles scanned pages / image-only tables
    opts.do_table_structure = True         # run TableFormer table-structure model
    opts.table_structure_options.mode = TableFormerMode.ACCURATE  # slower, better on complex/merged cells
    opts.table_structure_options.do_cell_matching = True          # map cells back to the PDF text layer

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def log(msg: str) -> None:
    """Print a progress line immediately (flushed, so it shows even when piped)."""
    print(msg, flush=True)


def conversion_reason(engine, rel: str, file_hash: str, force: bool) -> str:
    """Human-readable reason this PDF is being (re)converted, for the progress log."""
    if force:
        return "--force"
    with engine.connect() as conn:
        same = conn.execute(
            text("SELECT status, markdown_path FROM report_files WHERE pdf_path = :p AND file_hash = :h"),
            {"p": rel, "h": file_hash},
        ).first()
        if same:
            if same[0] == "failed":
                return "retrying after an earlier failure"
            return "markdown file is missing"
        older = conn.execute(
            text("SELECT file_hash FROM report_files WHERE pdf_path = :p ORDER BY converted_at DESC LIMIT 1"),
            {"p": rel},
        ).scalar()
    if older:
        return f"content changed (previous hash {older[:8]})"
    return "new file"


class OcrEmptyCounter(logging.Handler):
    """Counts log records saying RapidOCR returned an empty result.

    Attached to the root logger while a PDF is converted. This is only a hint
    (it also fires on harmless blank regions); the page text-length check below
    decides whether the vision model actually runs.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if OCR_EMPTY_MARKER in record.getMessage():
            self.count += 1


def page_text_length(doc, page_no: int) -> int:
    """Characters of real text Docling extracted for one page (markup ignored)."""
    md = doc.export_to_markdown(page_no=page_no)
    return len(re.sub(r"[\s|#*_>\-]", "", md))


def find_sparse_pages(doc) -> list[int]:
    """Page numbers whose extracted text is below MIN_PAGE_CHARS."""
    return [n for n in sorted(doc.pages) if page_text_length(doc, n) < MIN_PAGE_CHARS]


def vision_ocr_pages(pdf: Path, page_numbers: list[int]) -> dict[int, str]:
    """Transcribe the given 1-based pages with the Ollama vision model.

    Renders each page with pypdfium2 (already a Docling dependency) and sends the
    PNG to the model configured as "ocrFallbackModel" in config.json. Returns
    {page_no: markdown}. A page the model fails on is left out, so the caller
    keeps Docling's text for it. Imports are lazy: a run that never needs the
    fallback does not need Ollama.
    """
    import pypdfium2 as pdfium
    from ollama import Client

    model, options = model_settings("ocrFallbackModel", default_model="qwen2.5vl:7b")
    client = Client()
    results: dict[int, str] = {}
    pdf_doc = pdfium.PdfDocument(str(pdf))
    try:
        for i, page_no in enumerate(page_numbers, 1):
            started = time.time()
            log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: sending to {model} ...")
            image = pdf_doc[page_no - 1].render(scale=RENDER_SCALE).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            reply = client.chat(
                model=model,
                messages=[{"role": "user", "content": FALLBACK_PROMPT, "images": [buf.getvalue()]}],
                options=options,
            )
            text_out = (reply["message"]["content"] or "").strip()
            if text_out:
                results[page_no] = text_out
                log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: got {len(text_out)} chars in {time.time() - started:.1f}s")
            else:
                log(f"    [vision {i}/{len(page_numbers)}] page {page_no}: empty answer, keeping Docling text")
    finally:
        pdf_doc.close()
    return results


def merge_pages(doc, replacements: dict[int, str]) -> str:
    """Markdown for the whole document, page by page, with vision text swapped in.

    Each page is emitted as `<!-- page: N -->` followed by either the vision
    transcription (for replaced pages) or Docling's own text for that page.
    """
    parts = []
    for n in sorted(doc.pages):
        body = replacements.get(n) or doc.export_to_markdown(page_no=n)
        parts.append(f"<!-- page: {n} -->\n\n{body.strip()}")
    return "\n\n".join(parts) + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class StageError(Exception):
    """Wraps an exception with the pipeline stage it happened in."""

    def __init__(self, stage: str, original: BaseException):
        super().__init__(str(original))
        self.stage = stage
        self.original = original


def log_error(engine, pdf_path: str, file_hash: str | None, stage: str, exc: BaseException) -> None:
    """Append an error to report_file_errors; never raise (DB may be the problem)."""
    try:
        with engine.begin() as conn:
            conn.execute(
                LOG_ERROR,
                {
                    "pdf_path": pdf_path,
                    "file_hash": file_hash,
                    "stage": stage,
                    "error": str(exc) or exc.__class__.__name__,
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            )
    except Exception as db_exc:  # noqa: BLE001
        print(f"  could not save error to database: {db_exc}", file=sys.stderr)


def already_converted(engine, pdf_path: str, file_hash: str) -> bool:
    """True if this exact content was converted and its Markdown still exists."""
    with engine.connect() as conn:
        md_path = conn.execute(
            text(
                "SELECT markdown_path FROM report_files "
                "WHERE pdf_path = :p AND file_hash = :h AND status = 'converted'"
            ),
            {"p": pdf_path, "h": file_hash},
        ).scalar()
    return bool(md_path) and (ROOT / md_path).exists()


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def process_pdf(engine, pdf: Path, converter_holder: list, force: bool, docling_version: str) -> str:
    """Process one PDF. Returns 'converted' or 'skipped'; raises StageError on failure."""
    rel = pdf.relative_to(INPUT_DIR).as_posix()
    md_path = OUTPUT_DIR / Path(rel).with_suffix(".md")
    md_rel = md_path.relative_to(ROOT).as_posix()

    started = time.time()
    try:
        file_hash = sha256_file(pdf)
        file_size = pdf.stat().st_size
    except Exception as exc:  # noqa: BLE001
        raise StageError("hash", exc) from exc
    log(f"  hashed: {file_hash[:8]}... ({file_size / 1_048_576:.1f} MB)")

    try:
        if not force and already_converted(engine, rel, file_hash):
            return "skipped"
        log(f"  will convert: {conversion_reason(engine, rel, file_hash, force)}")
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc

    params = {
        "pdf_path": rel,
        "file_hash": file_hash,
        "file_size": file_size,
        "markdown_path": md_rel,
        "converter_version": docling_version,
    }

    try:
        if not converter_holder:
            log("  loading Docling models (first conversion this run) ...")
            t0 = time.time()
            converter_holder.append(build_converter())
            log(f"  models ready in {time.time() - t0:.1f}s")
        log("  converting with Docling (OCR + table structure) ...")
        t0 = time.time()
        counter = OcrEmptyCounter()
        logging.getLogger().addHandler(counter)
        try:
            doc = converter_holder[0].convert(pdf).document
        finally:
            logging.getLogger().removeHandler(counter)
        markdown = doc.export_to_markdown()
        pages, tables = len(doc.pages), len(doc.tables)
        log(f"  Docling done in {time.time() - t0:.1f}s: {pages} pages, {tables} tables")
        sparse = find_sparse_pages(doc)
        log(f"  OCR check: {len(sparse)} sparse page(s) under {MIN_PAGE_CHARS} chars"
            f"{' ' + str(sparse[:10]) if sparse else ''}; RapidOCR empty-result messages: {counter.count}")
    except Exception as exc:  # noqa: BLE001
        _record_failure(engine, params, "convert", exc)
        raise StageError("convert", exc) from exc

    fallback_pages, fallback_model = 0, None
    if sparse:
        log(f"  running vision OCR fallback on {len(sparse)} page(s) ...")
        try:
            replacements = vision_ocr_pages(pdf, sparse)
            if replacements:
                markdown = merge_pages(doc, replacements)
                fallback_pages = len(replacements)
                fallback_model = model_settings("ocrFallbackModel", "qwen2.5vl:7b")[0]
                log(f"  vision OCR replaced {fallback_pages}/{len(sparse)} page(s)")
            else:
                log("  vision OCR returned nothing; keeping Docling text")
        except Exception as exc:  # noqa: BLE001
            # The file still converts with Docling's own text; the failed
            # fallback is only logged so it can be retried or reviewed.
            print(f"  vision OCR failed, keeping Docling text: {exc}", file=sys.stderr)
            log_error(engine, rel, file_hash, "ocr_fallback", exc)
    params["ocr_fallback_pages"] = fallback_pages
    params["ocr_fallback_model"] = fallback_model
    params["ocr_empty_events"] = counter.count

    log(f"  writing {md_rel} ...")
    try:
        write_atomic(md_path, markdown)
    except Exception as exc:  # noqa: BLE001
        _record_failure(engine, params, "write", exc)
        raise StageError("write", exc) from exc

    try:
        with engine.begin() as conn:
            conn.execute(
                UPSERT,
                {**params, "status": "converted", "pages": pages, "tables": tables, "error": None},
            )
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc

    extra = f", vision OCR pages: {fallback_pages}" if fallback_pages else ""
    log(f"  saved to database. -> {md_rel} (pages: {pages}, tables: {tables}{extra}) in {time.time() - started:.1f}s")
    return "converted"


def _record_failure(engine, params: dict, stage: str, exc: BaseException) -> None:
    """Mark this (path, hash) as failed so it is retried next run."""
    try:
        with engine.begin() as conn:
            conn.execute(
                UPSERT,
                {
                    **params,
                    "markdown_path": None,
                    "status": "failed",
                    "pages": None,
                    "tables": None,
                    "ocr_fallback_pages": None,
                    "ocr_fallback_model": None,
                    "ocr_empty_events": None,
                    "error": f"{stage}: {exc}",
                },
            )
    except Exception as db_exc:  # noqa: BLE001
        print(f"  could not save failed status to database: {db_exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert new/changed PDFs in reports/ to Markdown.")
    parser.add_argument("--force", action="store_true", help="reconvert every PDF, ignoring the hash table")
    args = parser.parse_args()

    if not INPUT_DIR.exists():
        sys.exit(f"Input folder not found: {INPUT_DIR}")
    log("connecting to db")
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            for statement in DDL:
                conn.execute(text(statement))
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Database unavailable ({DATABASE_URL.split('@')[-1]}): {exc}")

    log("database ready (tables checked)")
    docling_version = pkg_version("docling")
    converter_holder: list = []  # built lazily so an all-skipped run loads no models
    counts = {"converted": 0, "skipped": 0, "failed": 0}

    pdfs = sorted(INPUT_DIR.rglob("*.pdf"))
    log(f"Found {len(pdfs)} PDF(s) in {INPUT_DIR}{' (--force)' if args.force else ''}")
    for idx, pdf in enumerate(pdfs, 1):
        rel = pdf.relative_to(INPUT_DIR).as_posix()
        log(f"[{idx}/{len(pdfs)}] {rel}")
        try:
            outcome = process_pdf(engine, pdf, converter_holder, args.force, docling_version)
        except StageError as err:
            counts["failed"] += 1
            log(f"  FAILED at stage '{err.stage}': {err}")
            file_hash = None
            try:
                file_hash = sha256_file(pdf) if err.stage != "hash" else None
            except Exception:  # noqa: BLE001
                pass
            log_error(engine, rel, file_hash, err.stage, err.original)
            continue
        counts[outcome] += 1
        if outcome == "skipped":
            log("  unchanged (same path + hash, markdown present), skipped")

    print(
        f"Done: {counts['converted']} converted, {counts['skipped']} skipped, "
        f"{counts['failed']} failed"
    )
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
