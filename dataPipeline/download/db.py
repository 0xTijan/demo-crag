"""Database access for the PDF download step.

Companies and their website URLs are read from `companies` joined to `stocks`
(`stocks.website_url`). Two tables are owned by this step:
  * `downloaded_files`: one row per downloaded source URL. It deduplicates
    reruns, both by URL and by SHA-256 of the content.
  * `download_errors`: one row per failure, with the stage it failed at.
"""
import sys
import traceback as tb

from sqlalchemy import create_engine, text

from .settings import DATABASE_URL

DDL = [
    """
    CREATE TABLE IF NOT EXISTS downloaded_files (
        download_id   SERIAL PRIMARY KEY,
        company_id    INT REFERENCES companies(company_id) ON DELETE CASCADE,
        category      TEXT        NOT NULL CHECK (category IN ('reports','announcements','other')),
        source_url    TEXT        NOT NULL,
        page_url      TEXT,
        link_text     TEXT,
        file_path     TEXT        NOT NULL,
        file_hash     CHAR(64)    NOT NULL,
        file_size     BIGINT,
        fiscal_year   INT,
        downloaded_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (company_id, source_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS downloaded_files_hash_idx ON downloaded_files (company_id, file_hash)",
    """
    CREATE TABLE IF NOT EXISTS download_errors (
        error_id    SERIAL PRIMARY KEY,
        company_id  INT REFERENCES companies(company_id) ON DELETE CASCADE,
        url         TEXT,
        stage       TEXT NOT NULL,
        error       TEXT NOT NULL,
        http_status INT,
        traceback   TEXT,
        occurred_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS download_errors_company_idx ON download_errors (company_id, occurred_at DESC)",
]


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))


def load_companies(engine, company_ids=None, limit=None) -> list[dict]:
    sql = """
        SELECT c.company_id, c.name, s.website_url
        FROM companies c
        LEFT JOIN stocks s ON s.isin = c.isin
    """
    params = {}
    if company_ids:
        sql += " WHERE c.company_id = ANY(:ids)"
        params["ids"] = list(company_ids)
    sql += " ORDER BY c.company_id"
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def known_downloads(engine, company_id: int) -> tuple[dict, dict]:
    """Return ({source_url: file_path}, {file_hash: file_path}) for a company."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT source_url, file_hash, file_path FROM downloaded_files WHERE company_id = :c"),
            {"c": company_id},
        ).all()
    return {r.source_url: r.file_path for r in rows}, {r.file_hash: r.file_path for r in rows}


def record_download(engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO downloaded_files
                    (company_id, category, source_url, page_url, link_text,
                     file_path, file_hash, file_size, fiscal_year)
                VALUES (:company_id, :category, :source_url, :page_url, :link_text,
                        :file_path, :file_hash, :file_size, :fiscal_year)
                ON CONFLICT (company_id, source_url) DO UPDATE SET
                    category = EXCLUDED.category, page_url = EXCLUDED.page_url,
                    link_text = EXCLUDED.link_text, file_path = EXCLUDED.file_path,
                    file_hash = EXCLUDED.file_hash, file_size = EXCLUDED.file_size,
                    fiscal_year = EXCLUDED.fiscal_year, downloaded_at = now()
                """
            ),
            row,
        )


class ErrorLog:
    """Writes failures to `download_errors`. It never raises."""

    def __init__(self, engine, company_id=None):
        self.engine = engine
        self.company_id = company_id
        self.count = 0

    def __call__(self, stage: str, error, url: str | None = None, http_status: int | None = None) -> None:
        self.count += 1
        if isinstance(error, BaseException):
            message = f"{error.__class__.__name__}: {error}"
            trace = "".join(tb.format_exception(type(error), error, error.__traceback__))
        else:
            message, trace = str(error), None
        print(f"    ! [{stage}] {url or ''} {message[:200]}", file=sys.stderr)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO download_errors (company_id, url, stage, error, http_status, traceback) "
                        "VALUES (:c, :u, :s, :e, :h, :t)"
                    ),
                    {"c": self.company_id, "u": url, "s": stage, "e": message[:4000],
                     "h": http_status, "t": trace},
                )
        except Exception as db_exc:  # noqa: BLE001
            print(f"    could not save error to database: {db_exc}", file=sys.stderr)
