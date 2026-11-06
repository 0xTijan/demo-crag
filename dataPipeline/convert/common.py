"""Shared plumbing for the PDF -> Markdown pipelines.

Every pipeline (currently docling.py)
does the same bookkeeping and only differs in how a PDF becomes Markdown:

1. Connect to PostgreSQL and create/upgrade the tracking tables.
2. For every reports/**/*.pdf compute its SHA-256. A PDF is identified by
   (path relative to reports/, hash, converter), so each pipeline tracks its own
   conversions and the same name with new content counts as a new version.
3. Skip it if that exact version was already converted by this pipeline and its
   Markdown still exists; otherwise call the pipeline's ``convert`` function.
4. Write the Markdown to the pipeline's output folder and record the result (or
   the failure) in the database. One bad PDF never stops the batch.

A pipeline calls ``run_pipeline(...)`` with a ``convert(pdf) -> Conversion``.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()  # read DATABASE_URL etc. from a local .env file, if present

ROOT = Path(__file__).resolve().parents[2]  # repository root
INPUT_DIR = ROOT / "reports"

# SQLAlchemy connection URL; same variable and default as db_gui.py / rag/sql.py.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)

# Idempotent schema, executed on every start so a fresh database just works.
#   report_files        one row per (pdf_path, file_hash, converter): a changed
#                       PDF gets a new row, the old one stays as history, and
#                       each pipeline has its own rows.
#   report_file_errors  append-only failure log (never updated), so retries that
#                       overwrite the report_files row cannot erase old errors.
# The ALTER/INDEX statements upgrade databases created by earlier versions
# (before error/OCR columns existed and when the key had no converter).
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
        converter         TEXT        NOT NULL DEFAULT 'docling',
        converter_version TEXT,
        error             TEXT,
        converted_at      TIMESTAMPTZ DEFAULT now()
    )
    """,
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS error TEXT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_fallback_pages INT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_fallback_model TEXT",
    "ALTER TABLE report_files ADD COLUMN IF NOT EXISTS ocr_empty_events INT",
    "UPDATE report_files SET converter = 'docling' WHERE converter IS NULL",
    # old key was (pdf_path, file_hash); now each converter has its own row
    "ALTER TABLE report_files DROP CONSTRAINT IF EXISTS report_files_pdf_path_file_hash_key",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS report_files_path_hash_converter_key
        ON report_files (pdf_path, file_hash, converter)
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
    "ALTER TABLE report_file_errors ADD COLUMN IF NOT EXISTS converter TEXT",
]

# Insert-or-update one report_files row. ON CONFLICT on the unique
# (pdf_path, file_hash, converter) means reprocessing identical content updates
# the row in place (a 'failed' row becomes 'converted', its error is cleared)
# instead of adding duplicates. Values are bound parameters, never formatted in.
UPSERT = text(
    """
    INSERT INTO report_files
        (pdf_path, file_hash, file_size, markdown_path, status, pages, tables,
         converter, converter_version, error, ocr_fallback_pages, ocr_fallback_model,
         ocr_empty_events, converted_at)
    VALUES
        (:pdf_path, :file_hash, :file_size, :markdown_path, :status, :pages, :tables,
         :converter, :converter_version, :error, :ocr_fallback_pages, :ocr_fallback_model,
         :ocr_empty_events, now())
    ON CONFLICT (pdf_path, file_hash, converter) DO UPDATE SET
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

# Append one row to report_file_errors (used by log_error()).
LOG_ERROR = text(
    """
    INSERT INTO report_file_errors (pdf_path, file_hash, stage, error, traceback, converter)
    VALUES (:pdf_path, :file_hash, :stage, :error, :traceback, :converter)
    """
)


@dataclass
class Conversion:
    """What a pipeline's convert() returns for one PDF."""

    markdown: str
    pages: int | None = None
    tables: int | None = None
    ocr_fallback_pages: int = 0
    ocr_fallback_model: str | None = None
    ocr_empty_events: int | None = None
    # Non-fatal problems (e.g. the vision OCR fallback failed but the file still
    # converted): (stage, exception) pairs, stored in report_file_errors.
    warnings: list[tuple[str, BaseException]] = field(default_factory=list)


class StageError(Exception):
    """Wraps an exception with the pipeline stage it happened in.

    stage is 'hash', 'convert', 'write' or 'db'. ``original`` keeps the real
    exception so the error log stores its true traceback.
    """

    def __init__(self, stage: str, original: BaseException):
        super().__init__(str(original))
        self.stage = stage
        self.original = original


def log(msg: str) -> None:
    """Print a progress line immediately (flushed, so it shows even when piped)."""
    print(msg, flush=True)


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's bytes (64 chars, fits CHAR(64)).

    Read in 1 MiB chunks so memory stays constant for very large reports. Based
    on content only: renaming or touching a file does not change it, editing does.
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path: Path, content: str) -> None:
    """Write UTF-8 text so readers never see a half-written file.

    Writes to <name>.tmp next to the target, then renames it over the final name
    (atomic on the same filesystem). A crash leaves the old Markdown intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class Tracker:
    """Database bookkeeping for one pipeline (one ``converter`` name)."""

    def __init__(self, engine, converter: str, version: str):
        self.engine = engine
        self.converter = converter
        self.version = version

    def log_error(self, pdf_path: str, file_hash: str | None, stage: str, exc: BaseException) -> None:
        """Append one failure to report_file_errors; never raises.

        Stores path, hash (None if the file could not be hashed), stage, message,
        the full traceback and the converter. If the database is the cause of the
        failure the secondary error is only printed, so the original is not masked.
        """
        try:
            with self.engine.begin() as conn:
                conn.execute(LOG_ERROR, {
                    "pdf_path": pdf_path,
                    "file_hash": file_hash,
                    "stage": stage,
                    "error": str(exc) or exc.__class__.__name__,
                    "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                    "converter": self.converter,
                })
        except Exception as db_exc:  # noqa: BLE001
            print(f"  could not save error to database: {db_exc}", file=sys.stderr)

    def already_converted(self, pdf_path: str, file_hash: str) -> bool:
        """True if this exact (path, hash) was converted by this pipeline.

        Requires a 'converted' row AND the Markdown file still on disk, so deleting
        an output file makes the next run regenerate it. 'failed' rows never
        count, which is why failed files are retried automatically.
        """
        with self.engine.connect() as conn:
            md_path = conn.execute(
                text("SELECT markdown_path FROM report_files "
                     "WHERE pdf_path = :p AND file_hash = :h AND converter = :c AND status = 'converted'"),
                {"p": pdf_path, "h": file_hash, "c": self.converter},
            ).scalar()
        return bool(md_path) and (ROOT / md_path).exists()

    def conversion_reason(self, rel: str, file_hash: str, force: bool) -> str:
        """Human-readable reason this PDF is being (re)converted (for the log only)."""
        if force:
            return "--force"
        with self.engine.connect() as conn:
            # Same path and content already known: it failed last time or its
            # Markdown file was deleted.
            same = conn.execute(
                text("SELECT status FROM report_files WHERE pdf_path = :p AND file_hash = :h AND converter = :c"),
                {"p": rel, "h": file_hash, "c": self.converter},
            ).first()
            if same:
                return "retrying after an earlier failure" if same[0] == "failed" else "markdown file is missing"
            # Same path but no row for this hash: replaced with new content.
            older = conn.execute(
                text("SELECT file_hash FROM report_files WHERE pdf_path = :p AND converter = :c "
                     "ORDER BY converted_at DESC LIMIT 1"),
                {"p": rel, "c": self.converter},
            ).scalar()
        return f"content changed (previous hash {older[:8]})" if older else "new file"

    def upsert(self, params: dict) -> None:
        with self.engine.begin() as conn:  # one transaction, commits on exit
            conn.execute(UPSERT, {"converter": self.converter, "converter_version": self.version, **params})

    def record_failure(self, params: dict, stage: str, exc: BaseException) -> None:
        """Upsert a 'failed' row for this (path, hash) so it is retried next run.

        Never raises: if the DB is down it only prints, to avoid hiding the real error.
        """
        try:
            self.upsert({**params, "markdown_path": None, "status": "failed", "pages": None,
                         "tables": None, "ocr_fallback_pages": None, "ocr_fallback_model": None,
                         "ocr_empty_events": None, "error": f"{stage}: {exc}"})
        except Exception as db_exc:  # noqa: BLE001
            print(f"  could not save failed status to database: {db_exc}", file=sys.stderr)


def process_pdf(tracker: Tracker, pdf: Path, output_dir: Path, convert, force: bool) -> str:
    """Process one PDF end to end. Returns 'converted' or 'skipped'.

    Stages: hash -> skip check -> convert -> write -> db. Raises
    StageError(stage, original) on failure; run_pipeline() logs it and moves on.
    The Markdown is written BEFORE the success row, so a DB failure only causes a
    reconversion next run and never a success row for a missing file.
    """
    rel = pdf.relative_to(INPUT_DIR).as_posix()          # identity (with hash + converter)
    md_path = output_dir / Path(rel).with_suffix(".md")  # mirrors reports/ layout
    md_rel = md_path.relative_to(ROOT).as_posix()

    started = time.time()
    try:  # stage "hash": no hash means no row can be keyed -> error log only
        file_hash = sha256_file(pdf)
        file_size = pdf.stat().st_size
    except Exception as exc:  # noqa: BLE001
        raise StageError("hash", exc) from exc
    log(f"  hashed: {file_hash[:8]}... ({file_size / 1_048_576:.1f} MB)")

    try:  # stage "db": skip files whose exact content was already converted
        if not force and tracker.already_converted(rel, file_hash):
            return "skipped"
        log(f"  will convert: {tracker.conversion_reason(rel, file_hash, force)}")
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc

    params = {"pdf_path": rel, "file_hash": file_hash, "file_size": file_size, "markdown_path": md_rel}

    try:  # stage "convert": the pipeline-specific part
        result: Conversion = convert(pdf)
    except Exception as exc:  # noqa: BLE001
        tracker.record_failure(params, "convert", exc)
        raise StageError("convert", exc) from exc
    for stage, exc in result.warnings:  # non-fatal problems: log, keep going
        tracker.log_error(rel, file_hash, stage, exc)

    log(f"  writing {md_rel} ...")
    try:  # stage "write"
        write_atomic(md_path, result.markdown)
    except Exception as exc:  # noqa: BLE001
        tracker.record_failure(params, "write", exc)
        raise StageError("write", exc) from exc

    try:  # stage "db": record the success
        tracker.upsert({**params, "status": "converted", "pages": result.pages, "tables": result.tables,
                        "error": None, "ocr_fallback_pages": result.ocr_fallback_pages,
                        "ocr_fallback_model": result.ocr_fallback_model,
                        "ocr_empty_events": result.ocr_empty_events})
    except Exception as exc:  # noqa: BLE001
        raise StageError("db", exc) from exc

    extra = f", vision OCR pages: {result.ocr_fallback_pages}" if result.ocr_fallback_pages else ""
    log(f"  saved to database. -> {md_rel} (pages: {result.pages}, tables: {result.tables}{extra}) "
        f"in {time.time() - started:.1f}s")
    return "converted"


def run_pipeline(converter: str, version: str, output_dir: Path,
                 convert: Callable[[Path], Conversion], description: str) -> None:
    """CLI entry point shared by all pipelines.

    Exits with status 1 if any PDF failed, so cron/CI can detect a partial run.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--force", action="store_true", help="reconvert every PDF, ignoring the hash table")
    args = parser.parse_args()

    if not INPUT_DIR.exists():
        sys.exit(f"Input folder not found: {INPUT_DIR}")
    # Connect and create/upgrade the tables in one transaction. On failure print
    # only the host part of the URL (after "@") so the password is never shown.
    log(f"[{converter}] connecting to db")
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            for statement in DDL:
                conn.execute(text(statement))
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Database unavailable ({DATABASE_URL.split('@')[-1]}): {exc}")
    log("database ready (tables checked)")

    tracker = Tracker(engine, converter, version)
    counts = {"converted": 0, "skipped": 0, "failed": 0}
    pdfs = sorted(INPUT_DIR.rglob("*.pdf"))  # recursive and sorted: deterministic order
    log(f"Found {len(pdfs)} PDF(s) in {INPUT_DIR}{' (--force)' if args.force else ''}")
    for idx, pdf in enumerate(pdfs, 1):
        rel = pdf.relative_to(INPUT_DIR).as_posix()
        log(f"[{idx}/{len(pdfs)}] {rel}")
        try:
            outcome = process_pdf(tracker, pdf, output_dir, convert, args.force)
        except StageError as err:
            # One bad PDF must not stop the batch: count it, store the error, go on.
            counts["failed"] += 1
            log(f"  FAILED at stage '{err.stage}': {err}")
            file_hash = None  # re-hash for the error row (impossible if hashing failed)
            try:
                file_hash = sha256_file(pdf) if err.stage != "hash" else None
            except Exception:  # noqa: BLE001
                pass
            tracker.log_error(rel, file_hash, err.stage, err.original)
            continue
        counts[outcome] += 1
        if outcome == "skipped":
            log("  unchanged (same path + hash, markdown present), skipped")

    log(f"Done: {counts['converted']} converted, {counts['skipped']} skipped, {counts['failed']} failed")
    if counts["failed"]:
        sys.exit(1)
