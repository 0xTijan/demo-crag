from dotenv import load_dotenv
load_dotenv()
import argparse
import hashlib
import os
import sys
import traceback
from importlib.metadata import version as pkg_version
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
from sqlalchemy import create_engine, text  # noqa: E402

ROOT = SCRIPT_DIR.parents[1]
INPUT_DIR = ROOT / "reports"
OUTPUT_DIR = ROOT / "markdowns"

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
         converter_version, error, converted_at)
    VALUES
        (:pdf_path, :file_hash, :file_size, :markdown_path, :status, :pages, :tables,
         :converter_version, :error, now())
    ON CONFLICT (pdf_path, file_hash) DO UPDATE SET
        file_size = EXCLUDED.file_size,
        markdown_path = EXCLUDED.markdown_path,
        status = EXCLUDED.status,
        pages = EXCLUDED.pages,
        tables = EXCLUDED.tables,
        converter_version = EXCLUDED.converter_version,
        error = EXCLUDED.error,
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

    try:
        file_hash = sha256_file(pdf)
        file_size = pdf.stat().st_size
    except Exception as exc:  # noqa: BLE001
        raise StageError("hash", exc) from exc

    try:
        if not force and already_converted(engine, rel, file_hash):
            return "skipped"
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
            converter_holder.append(build_converter())
        doc = converter_holder[0].convert(pdf).document
        markdown = doc.export_to_markdown()
        pages, tables = len(doc.pages), len(doc.tables)
    except Exception as exc:  # noqa: BLE001
        _record_failure(engine, params, "convert", exc)
        raise StageError("convert", exc) from exc

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

    print(f"  -> {md_rel} (pages: {pages}, tables: {tables})")
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
    print("connecting to db")
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            for statement in DDL:
                conn.execute(text(statement))
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Database unavailable ({DATABASE_URL.split('@')[-1]}): {exc}")

    docling_version = pkg_version("docling")
    converter_holder: list = []  # built lazily so an all-skipped run loads no models
    counts = {"converted": 0, "skipped": 0, "failed": 0}

    for pdf in sorted(INPUT_DIR.rglob("*.pdf")):
        rel = pdf.relative_to(INPUT_DIR).as_posix()
        print(f"{rel}")
        try:
            outcome = process_pdf(engine, pdf, converter_holder, args.force, docling_version)
        except StageError as err:
            counts["failed"] += 1
            print(f"  FAILED at {err.stage}: {err}", file=sys.stderr)
            file_hash = None
            try:
                file_hash = sha256_file(pdf) if err.stage != "hash" else None
            except Exception:  # noqa: BLE001
                pass
            log_error(engine, rel, file_hash, err.stage, err.original)
            continue
        counts[outcome] += 1
        if outcome == "skipped":
            print("  unchanged, skipped")

    print(
        f"Done: {counts['converted']} converted, {counts['skipped']} skipped, "
        f"{counts['failed']} failed"
    )
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
