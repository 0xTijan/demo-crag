"""Database access for the datasheet download step.

Only what the crawler needs to work systematically is stored:
  * `datasheet_files`: one row per distinct PDF (manufacturer + SHA-256 of its bytes),
    with the file it was saved to and `source_urls`, every URL that delivers it (many
    parts of a series share one datasheet). Reruns skip known URLs.
  * `datasheet_errors`: one row per failure, with the stage it failed at. For a
    datasheet that could not be obtained (404, not a PDF, "not publicly available")
    `context` holds everything known about the part: page, headings, section, the
    table headers, its row as {header: value}, and the whole table.

Details of where a PDF was found (page, table row, click trail) are not stored here;
they are in datasheets/{manufacturer}/manifest.jsonl.
"""
import json
import sys
import traceback as tb

from sqlalchemy import create_engine, text

from .settings import DATABASE_URL

DDL = [
    """
    CREATE TABLE IF NOT EXISTS datasheet_files (
        datasheet_id  SERIAL PRIMARY KEY,
        manufacturer  TEXT        NOT NULL,
        file_hash     CHAR(64)    NOT NULL,
        file_path     TEXT        NOT NULL,
        file_size     BIGINT,
        source_urls   TEXT[]      NOT NULL DEFAULT '{}',
        downloaded_at TIMESTAMPTZ DEFAULT now(),
        updated_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE (manufacturer, file_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS datasheet_files_urls_idx ON datasheet_files USING GIN (source_urls)",
    """
    CREATE TABLE IF NOT EXISTS datasheet_errors (
        error_id     SERIAL PRIMARY KEY,
        manufacturer TEXT,
        url          TEXT,
        stage        TEXT NOT NULL,
        error        TEXT NOT NULL,
        http_status  INT,
        traceback    TEXT,
        context      JSONB,
        occurred_at  TIMESTAMPTZ DEFAULT now()
    )
    """,
    # databases created before the context column existed
    "ALTER TABLE datasheet_errors ADD COLUMN IF NOT EXISTS context JSONB",
    "CREATE INDEX IF NOT EXISTS datasheet_errors_manufacturer_idx ON datasheet_errors (manufacturer, occurred_at DESC)",
]


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


# Databases from before the one-row-per-PDF layout had one row per URL (column source_url).
MIGRATE_PER_URL = [
    "ALTER TABLE datasheet_files RENAME TO datasheet_files_per_url",
    "ALTER INDEX IF EXISTS datasheet_files_pkey RENAME TO datasheet_files_per_url_pkey",
    "ALTER SEQUENCE IF EXISTS datasheet_files_datasheet_id_seq RENAME TO datasheet_files_per_url_id_seq",
    "DROP INDEX IF EXISTS datasheet_files_hash_idx",
    DDL[0],
    DDL[1],
    """
    INSERT INTO datasheet_files (manufacturer, file_hash, file_path, file_size, source_urls, downloaded_at, updated_at)
    SELECT manufacturer, file_hash, min(file_path), max(file_size), array_agg(DISTINCT source_url),
           min(downloaded_at), max(downloaded_at)
    FROM datasheet_files_per_url GROUP BY manufacturer, file_hash
    """,
    "DROP TABLE datasheet_files_per_url",
]


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        old_layout = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'datasheet_files' AND column_name = 'source_url'")).first()
        if old_layout:
            for stmt in MIGRATE_PER_URL:
                conn.execute(text(stmt))
            print("    datasheet_files migrated to one row per PDF (source_urls)")
        for stmt in DDL:
            conn.execute(text(stmt))


def known_datasheets(engine, manufacturer: str) -> tuple[dict, dict]:
    """Return ({source_url: file_path}, {file_hash: file_path}) for a manufacturer."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT file_hash, file_path, source_urls FROM datasheet_files WHERE manufacturer = :m"),
            {"m": manufacturer},
        ).all()
    urls = {u: r.file_path for r in rows for u in r.source_urls}
    return urls, {r.file_hash: r.file_path for r in rows}


def record_datasheet(engine, row: dict) -> None:
    """Add `source_url` to the row of its PDF (one row per manufacturer + file_hash).

    row: manufacturer, source_url, file_path, file_hash, file_size. A URL that used to
    deliver another PDF (e.g. a new datasheet revision) is moved to this one; a PDF left
    without any URL loses its row (its file stays on disk)."""
    params = {k: row[k] for k in ("manufacturer", "source_url", "file_path", "file_hash", "file_size")}
    with engine.begin() as conn:
        conn.execute(text(
            """
            UPDATE datasheet_files SET source_urls = array_remove(source_urls, :source_url), updated_at = now()
            WHERE manufacturer = :manufacturer AND file_hash <> :file_hash AND :source_url = ANY(source_urls)
            """), params)
        conn.execute(text(
            "DELETE FROM datasheet_files WHERE manufacturer = :manufacturer AND cardinality(source_urls) = 0"),
            params)
        conn.execute(text(
            """
            INSERT INTO datasheet_files (manufacturer, file_hash, file_path, file_size, source_urls)
            VALUES (:manufacturer, :file_hash, :file_path, :file_size, ARRAY[CAST(:source_url AS TEXT)])
            ON CONFLICT (manufacturer, file_hash) DO UPDATE SET
                source_urls = CASE WHEN :source_url = ANY(datasheet_files.source_urls)
                                   THEN datasheet_files.source_urls
                                   ELSE array_append(datasheet_files.source_urls, CAST(:source_url AS TEXT)) END,
                updated_at = now()
            """), params)


# Failures that are about one datasheet (and can be retried with run.py --retry-failed)
DATASHEET_STAGES = ("download", "verify", "write", "db", "datasheet_unavailable")


def failed_datasheets(engine, manufacturers=None) -> list[dict]:
    """datasheet_errors rows about a datasheet that was not obtained, oldest first."""
    sql = ("SELECT error_id, manufacturer, url, stage, error, http_status, context FROM datasheet_errors "
           "WHERE stage = ANY(:stages)")
    params: dict = {"stages": list(DATASHEET_STAGES)}
    if manufacturers:
        sql += " AND manufacturer = ANY(:m)"
        params["m"] = list(manufacturers)
    sql += " ORDER BY error_id"
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def delete_errors(engine, error_ids) -> None:
    if error_ids:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM datasheet_errors WHERE error_id = ANY(:ids)"), {"ids": list(error_ids)})


class ErrorLog:
    """Writes failures to `datasheet_errors`. It never raises. With `engine=None` failures are only printed."""

    def __init__(self, engine, manufacturer=None):
        self.engine = engine
        self.manufacturer = manufacturer
        self.count = 0

    def __call__(self, stage: str, error, url: str | None = None, http_status: int | None = None,
                 context: dict | None = None) -> None:
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
                        "INSERT INTO datasheet_errors (manufacturer, url, stage, error, http_status, traceback, context) "
                        "VALUES (:m, :u, :s, :e, :h, :t, CAST(:c AS JSONB))"
                    ),
                    {"m": self.manufacturer, "u": url, "s": stage, "e": message[:4000],
                     "h": http_status, "t": trace,
                     "c": json.dumps(context, ensure_ascii=False) if context else None},
                )
        except Exception as db_exc:  # noqa: BLE001
            print(f"    could not save error to database: {db_exc}", file=sys.stderr)
