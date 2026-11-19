"""Database access for the datasheet download step.

Only what the crawler needs to work systematically is stored:
  * `datasheet_files`: one row per distinct PDF (manufacturer + SHA-256 of its bytes)
    and the file it was saved to.
  * `datasheet_urls`: one row per source URL, pointing to its PDF in datasheet_files
    (many parts of a series share one datasheet). Reruns skip known URLs.
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
        downloaded_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (manufacturer, file_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS datasheet_urls (
        url_id        SERIAL PRIMARY KEY,
        datasheet_id  INT         NOT NULL REFERENCES datasheet_files(datasheet_id) ON DELETE CASCADE,
        manufacturer  TEXT        NOT NULL,
        source_url    TEXT        NOT NULL,
        first_seen    TIMESTAMPTZ DEFAULT now(),
        last_seen     TIMESTAMPTZ DEFAULT now(),
        UNIQUE (manufacturer, source_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS datasheet_urls_datasheet_idx ON datasheet_urls (datasheet_id)",
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


# Earlier layouts of datasheet_files: one row per URL (column source_url), or one row per
# PDF with a URL list (column source_urls). Both are moved to datasheet_files + datasheet_urls.
def _migrate(conn, old_column: str) -> None:
    urls = "source_url" if old_column == "source_url" else "unnest(source_urls)"
    for stmt in (
        "ALTER TABLE datasheet_files RENAME TO datasheet_files_old",
        "ALTER INDEX IF EXISTS datasheet_files_pkey RENAME TO datasheet_files_old_pkey",
        "ALTER SEQUENCE IF EXISTS datasheet_files_datasheet_id_seq RENAME TO datasheet_files_old_id_seq",
        "DROP INDEX IF EXISTS datasheet_files_hash_idx",
        "DROP INDEX IF EXISTS datasheet_files_urls_idx",
        "ALTER TABLE datasheet_files_old DROP CONSTRAINT IF EXISTS datasheet_files_manufacturer_file_hash_key",
        DDL[0], DDL[1], DDL[2],
        """
        INSERT INTO datasheet_files (manufacturer, file_hash, file_path, file_size, downloaded_at)
        SELECT manufacturer, file_hash, min(file_path), max(file_size), min(downloaded_at)
        FROM datasheet_files_old GROUP BY manufacturer, file_hash
        """,
        f"""
        INSERT INTO datasheet_urls (datasheet_id, manufacturer, source_url, first_seen, last_seen)
        SELECT DISTINCT ON (o.manufacturer, o.url) f.datasheet_id, o.manufacturer, o.url, o.at, o.at
        FROM (SELECT manufacturer, file_hash, {urls} AS url, downloaded_at AS at FROM datasheet_files_old) o
        JOIN datasheet_files f USING (manufacturer, file_hash)
        ORDER BY o.manufacturer, o.url, o.at DESC
        """,
        "DROP TABLE datasheet_files_old",
    ):
        conn.execute(text(stmt))


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        old = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'datasheet_files' AND column_name IN ('source_url', 'source_urls')")).first()
        if old:
            _migrate(conn, old[0])
            print("    datasheet_files migrated: one row per PDF, URLs in datasheet_urls")
        for stmt in DDL:
            conn.execute(text(stmt))


def known_datasheets(engine, manufacturer: str) -> tuple[dict, dict]:
    """Return ({source_url: file_path}, {file_hash: file_path}) for a manufacturer."""
    with engine.connect() as conn:
        files = conn.execute(
            text("SELECT file_hash, file_path FROM datasheet_files WHERE manufacturer = :m"), {"m": manufacturer}).all()
        urls = conn.execute(
            text("SELECT u.source_url, f.file_path FROM datasheet_urls u JOIN datasheet_files f USING (datasheet_id) "
                 "WHERE u.manufacturer = :m"), {"m": manufacturer}).all()
    return {r.source_url: r.file_path for r in urls}, {r.file_hash: r.file_path for r in files}


def record_datasheet(engine, row: dict) -> None:
    """Record that `source_url` delivers the PDF `file_hash` (row: manufacturer, source_url,
    file_path, file_hash, file_size): one datasheet_files row per PDF, one datasheet_urls row
    per URL. A URL that used to deliver another PDF (a new revision) now points to this one;
    a PDF left without any URL loses its row (its file stays on disk)."""
    params = {k: row[k] for k in ("manufacturer", "source_url", "file_path", "file_hash", "file_size")}
    with engine.begin() as conn:
        datasheet_id = conn.execute(text(
            """
            INSERT INTO datasheet_files (manufacturer, file_hash, file_path, file_size)
            VALUES (:manufacturer, :file_hash, :file_path, :file_size)
            ON CONFLICT (manufacturer, file_hash) DO UPDATE SET file_path = datasheet_files.file_path
            RETURNING datasheet_id
            """), params).scalar_one()
        conn.execute(text(
            """
            INSERT INTO datasheet_urls (datasheet_id, manufacturer, source_url)
            VALUES (:id, :manufacturer, :source_url)
            ON CONFLICT (manufacturer, source_url) DO UPDATE SET
                datasheet_id = EXCLUDED.datasheet_id, last_seen = now()
            """), {**params, "id": datasheet_id})
        conn.execute(text(
            """
            DELETE FROM datasheet_files f WHERE f.manufacturer = :manufacturer
              AND NOT EXISTS (SELECT 1 FROM datasheet_urls u WHERE u.datasheet_id = f.datasheet_id)
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
