"""Database access for the datasheet download step.

Only what the crawler needs to work systematically is stored:
  * `datasheet_files`: one row per unique PDF (same bytes = same row), with the file it
    was saved to and the SHA-256 of its bytes.
  * `datasheet_sources`: one row per source URL where a PDF was found, pointing to its
    `datasheet_files` row, with where it was found (page, part row, click trail), the
    part number when it can be read safely, and when the URL was first and last seen.
    Many URLs (one per orderable part number) often share one PDF.
  * `datasheet_errors`: one row per failure, with the stage it failed at.

Reruns skip known URLs, and the same bytes under another URL are stored once.
datasheets/{manufacturer}/manifest.jsonl keeps the same details as a file.
"""
import sys
import traceback as tb

from sqlalchemy import create_engine, text

from .downloader import part_number_from_url
from .settings import DATABASE_URL

DDL = [
    """
    CREATE TABLE IF NOT EXISTS datasheet_files (
        datasheet_id  SERIAL PRIMARY KEY,
        manufacturer  TEXT        NOT NULL,
        file_path     TEXT        NOT NULL,
        file_hash     CHAR(64)    NOT NULL,
        file_size     BIGINT,
        downloaded_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (manufacturer, file_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS datasheet_sources (
        source_id     SERIAL PRIMARY KEY,
        datasheet_id  INT         NOT NULL REFERENCES datasheet_files (datasheet_id) ON DELETE CASCADE,
        manufacturer  TEXT        NOT NULL,
        source_url    TEXT        NOT NULL,
        page_url      TEXT,
        part_number   TEXT,
        link_text     TEXT,
        context       TEXT,
        trail         TEXT,
        first_seen_at TIMESTAMPTZ DEFAULT now(),
        last_seen_at  TIMESTAMPTZ DEFAULT now(),
        UNIQUE (manufacturer, source_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS datasheet_sources_datasheet_idx ON datasheet_sources (datasheet_id)",
    "CREATE INDEX IF NOT EXISTS datasheet_sources_part_idx ON datasheet_sources (manufacturer, part_number)",
    """
    CREATE TABLE IF NOT EXISTS datasheet_errors (
        error_id     SERIAL PRIMARY KEY,
        manufacturer TEXT,
        url          TEXT,
        stage        TEXT NOT NULL,
        error        TEXT NOT NULL,
        http_status  INT,
        traceback    TEXT,
        occurred_at  TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS datasheet_errors_manufacturer_idx ON datasheet_errors (manufacturer, occurred_at DESC)",
]

# Older databases have one datasheet_files row per source URL. These statements move the
# URLs to datasheet_sources and keep one datasheet_files row per unique PDF (the lowest
# datasheet_id of each hash, so existing ids stay valid). Run in one transaction.
MIGRATE_ONE_ROW_PER_URL = [
    """
    CREATE TEMP TABLE keep_ids ON COMMIT DROP AS
    SELECT manufacturer, file_hash, min(datasheet_id) AS datasheet_id
    FROM datasheet_files GROUP BY manufacturer, file_hash
    """,
    """
    INSERT INTO datasheet_sources (datasheet_id, manufacturer, source_url, first_seen_at, last_seen_at)
    SELECT k.datasheet_id, f.manufacturer, f.source_url, f.downloaded_at, f.downloaded_at
    FROM datasheet_files f JOIN keep_ids k USING (manufacturer, file_hash)
    ON CONFLICT (manufacturer, source_url) DO NOTHING
    """,
    """
    UPDATE datasheet_files f SET downloaded_at = d.first_at
    FROM (SELECT manufacturer, file_hash, min(downloaded_at) AS first_at
          FROM datasheet_files GROUP BY manufacturer, file_hash) d
    WHERE f.manufacturer = d.manufacturer AND f.file_hash = d.file_hash
    """,
    """
    DELETE FROM datasheet_files f USING keep_ids k
    WHERE f.manufacturer = k.manufacturer AND f.file_hash = k.file_hash AND f.datasheet_id <> k.datasheet_id
    """,
    "ALTER TABLE datasheet_files DROP COLUMN source_url",  # also drops UNIQUE (manufacturer, source_url)
    "DROP INDEX IF EXISTS datasheet_files_hash_idx",       # replaced by the UNIQUE constraint below
    "ALTER TABLE datasheet_files ADD CONSTRAINT datasheet_files_manufacturer_file_hash_key UNIQUE (manufacturer, file_hash)",
]

SOURCE_FIELDS = ("page_url", "part_number", "link_text", "context", "trail")


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _has_column(conn, table: str, column: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM information_schema.columns "
             "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).first() is not None


def ensure_tables(engine) -> None:
    """Create the tables; migrate an older one-row-per-URL datasheet_files table first."""
    with engine.begin() as conn:
        old_layout = _has_column(conn, "datasheet_files", "source_url")
        if old_layout:
            conn.execute(text(DDL[1]))  # datasheet_sources, so the URLs can move there
            for stmt in MIGRATE_ONE_ROW_PER_URL:
                conn.execute(text(stmt))
            print("    datasheet_files migrated: URLs moved to datasheet_sources, one row per PDF", file=sys.stderr)
        for stmt in DDL:
            conn.execute(text(stmt))


def known_datasheets(engine, manufacturer: str) -> tuple[dict, dict]:
    """Return ({source_url: file_path}, {file_hash: file_path}) for a manufacturer."""
    with engine.connect() as conn:
        sources = conn.execute(
            text("SELECT s.source_url, f.file_path FROM datasheet_sources s "
                 "JOIN datasheet_files f USING (datasheet_id) WHERE s.manufacturer = :m"),
            {"m": manufacturer},
        ).all()
        files = conn.execute(
            text("SELECT file_hash, file_path FROM datasheet_files WHERE manufacturer = :m"),
            {"m": manufacturer},
        ).all()
    return {r.source_url: r.file_path for r in sources}, {r.file_hash: r.file_path for r in files}


def _source_params(row: dict) -> dict:
    params = {k: row.get(k) for k in ("manufacturer", "source_url", "file_path", "file_hash", "file_size")}
    params.update({k: row.get(k) for k in SOURCE_FIELDS})
    if not params["part_number"]:
        params["part_number"] = part_number_from_url(
            row["source_url"], row.get("page_url"), row.get("link_text"), row.get("context"))
    return params


_UPSERT_FILE = """
    INSERT INTO datasheet_files (manufacturer, file_path, file_hash, file_size {extra_cols})
    VALUES (:manufacturer, :file_path, :file_hash, :file_size {extra_vals})
    ON CONFLICT (manufacturer, file_hash) DO UPDATE SET
        file_path = EXCLUDED.file_path, file_size = EXCLUDED.file_size
    RETURNING datasheet_id
"""


def record_datasheet(engine, row: dict) -> int:
    """Save one source URL that was just found (and downloaded, or matched to a saved PDF).

    row: manufacturer, source_url, file_path, file_hash, file_size, and optionally
    page_url, part_number, link_text, context, trail. The PDF gets one datasheet_files
    row per unique hash; the URL gets (or updates) its datasheet_sources row, and is
    marked as seen now. Returns the datasheet_id.
    """
    params = _source_params(row)
    with engine.begin() as conn:
        datasheet_id = conn.execute(text(_UPSERT_FILE.format(extra_cols="", extra_vals="")), params).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO datasheet_sources
                    (datasheet_id, manufacturer, source_url, page_url, part_number, link_text, context, trail)
                VALUES (:datasheet_id, :manufacturer, :source_url, :page_url, :part_number, :link_text, :context, :trail)
                ON CONFLICT (manufacturer, source_url) DO UPDATE SET
                    datasheet_id = EXCLUDED.datasheet_id,
                    page_url     = COALESCE(EXCLUDED.page_url, datasheet_sources.page_url),
                    part_number  = COALESCE(EXCLUDED.part_number, datasheet_sources.part_number),
                    link_text    = COALESCE(EXCLUDED.link_text, datasheet_sources.link_text),
                    context      = COALESCE(EXCLUDED.context, datasheet_sources.context),
                    trail        = COALESCE(EXCLUDED.trail, datasheet_sources.trail),
                    last_seen_at = now()
                """
            ),
            {**params, "datasheet_id": datasheet_id},
        )
    return datasheet_id


def mark_seen(engine, row: dict) -> None:
    """A known URL was found again: update where it was found and its last_seen_at."""
    params = {k: row.get(k) for k in ("manufacturer", "source_url", *SOURCE_FIELDS)}
    if not params["part_number"]:
        params["part_number"] = part_number_from_url(
            row["source_url"], row.get("page_url"), row.get("link_text"), row.get("context"))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE datasheet_sources SET
                    page_url     = COALESCE(:page_url, page_url),
                    part_number  = COALESCE(:part_number, part_number),
                    link_text    = COALESCE(:link_text, link_text),
                    context      = COALESCE(:context, context),
                    trail        = COALESCE(:trail, trail),
                    last_seen_at = now()
                WHERE manufacturer = :manufacturer AND source_url = :source_url
                """
            ),
            params,
        )


def sync_manifest(engine, rows) -> int:
    """Add manifest.jsonl rows the database doesn't have yet (saved by a --no-db run, or
    details lost when an older one-row-per-URL table was migrated). Existing values and
    last_seen_at are kept; only empty fields are filled. Returns the number of rows sent.
    """
    rows = [r for r in rows if r.get("source_url") and r.get("file_hash") and r.get("file_path")]
    if not rows:
        return 0
    with engine.begin() as conn:
        for row in rows:
            params = _source_params(row)
            params["seen_at"] = row.get("downloaded_at")
            datasheet_id = conn.execute(
                text(_UPSERT_FILE.format(extra_cols=", downloaded_at",
                                         extra_vals=", COALESCE(CAST(:seen_at AS TIMESTAMPTZ), now())")),
                params,
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO datasheet_sources
                        (datasheet_id, manufacturer, source_url, page_url, part_number, link_text, context, trail,
                         first_seen_at, last_seen_at)
                    VALUES (:datasheet_id, :manufacturer, :source_url, :page_url, :part_number, :link_text, :context,
                            :trail, COALESCE(CAST(:seen_at AS TIMESTAMPTZ), now()),
                            COALESCE(CAST(:seen_at AS TIMESTAMPTZ), now()))
                    ON CONFLICT (manufacturer, source_url) DO UPDATE SET
                        page_url    = COALESCE(datasheet_sources.page_url, EXCLUDED.page_url),
                        part_number = COALESCE(datasheet_sources.part_number, EXCLUDED.part_number),
                        link_text   = COALESCE(datasheet_sources.link_text, EXCLUDED.link_text),
                        context     = COALESCE(datasheet_sources.context, EXCLUDED.context),
                        trail       = COALESCE(datasheet_sources.trail, EXCLUDED.trail)
                    """
                ),
                {**params, "datasheet_id": datasheet_id},
            )
    return len(rows)


class ErrorLog:
    """Writes failures to `datasheet_errors`. It never raises. With `engine=None` failures are only printed."""

    def __init__(self, engine, manufacturer=None):
        self.engine = engine
        self.manufacturer = manufacturer
        self.count = 0

    def __call__(self, stage: str, error, url: str | None = None, http_status: int | None = None) -> None:
        self.count += 1
        if isinstance(error, BaseException):
            message = f"{error.__class__.__name__}: {error}"
            trace = "".join(tb.format_exception(type(error), error, error.__traceback__))
        else:
            message, trace = str(error), None
        print(f"    ! [{stage}] {url or ''} {message[:200]}", file=sys.stderr)
        if self.engine is None:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO datasheet_errors (manufacturer, url, stage, error, http_status, traceback) "
                        "VALUES (:m, :u, :s, :e, :h, :t)"
                    ),
                    {"m": self.manufacturer, "u": url, "s": stage, "e": message[:4000],
                     "h": http_status, "t": trace},
                )
        except Exception as db_exc:  # noqa: BLE001
            print(f"    could not save error to database: {db_exc}", file=sys.stderr)
