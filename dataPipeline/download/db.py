"""Database access for the PDF download step.

Companies are rows of `stocks` (keyed by ISIN); their website is `stocks.website_url`. Two tables are owned by this step:
  * `downloaded_files`: one row per downloaded source URL. It deduplicates
    reruns, both by URL and by SHA-256 of the content.
  * `download_errors`: one row per failure, with the stage it failed at.
Two more hold the result of the last crawl, so a rerun can skip the crawl:
  * `crawl_runs`: one row per company, describing its latest crawl.
  * `discovered_pdfs`: every PDF link that crawl found (before any filtering).
"""
import sys
import traceback as tb

from sqlalchemy import create_engine, text

from .settings import DATABASE_URL

DDL = [
    """
    CREATE TABLE IF NOT EXISTS downloaded_files (
        download_id   SERIAL PRIMARY KEY,
        stock_isin    CHAR(12) NOT NULL REFERENCES stocks(isin) ON DELETE CASCADE,
        category      TEXT        NOT NULL CHECK (category IN ('reports','announcements','other')),
        source_url    TEXT        NOT NULL,
        page_url      TEXT,
        link_text     TEXT,
        file_path     TEXT        NOT NULL,
        file_hash     CHAR(64)    NOT NULL,
        file_size     BIGINT,
        fiscal_year   INT,
        downloaded_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (stock_isin, source_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS downloaded_files_hash_idx ON downloaded_files (stock_isin, file_hash)",
    """
    CREATE TABLE IF NOT EXISTS download_errors (
        error_id    SERIAL PRIMARY KEY,
        stock_isin  CHAR(12) REFERENCES stocks(isin) ON DELETE CASCADE,
        url         TEXT,
        stage       TEXT NOT NULL,
        error       TEXT NOT NULL,
        http_status INT,
        traceback   TEXT,
        occurred_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS download_errors_company_idx ON download_errors (stock_isin, occurred_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS crawl_runs (
        stock_isin    CHAR(12) PRIMARY KEY REFERENCES stocks(isin) ON DELETE CASCADE,
        crawled_at    TIMESTAMPTZ NOT NULL,
        website_url   TEXT        NOT NULL,
        base_url      TEXT        NOT NULL,
        english       TEXT        NOT NULL,
        pages_visited INT,
        used_js       BOOLEAN,
        pdf_links     INT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discovered_pdfs (
        stock_isin     CHAR(12) NOT NULL REFERENCES stocks(isin) ON DELETE CASCADE,
        source_url     TEXT        NOT NULL,
        link_text      TEXT,
        context        TEXT,
        page_url       TEXT,
        page_title     TEXT,
        page_relevance INT,
        first_seen     TIMESTAMPTZ NOT NULL,
        last_seen      TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (stock_isin, source_url)
    )
    """,
]


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def ensure_tables(engine) -> None:
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))


def load_companies(engine, isins=None, limit=None) -> list[dict]:
    sql = "SELECT isin, name, website_url FROM stocks"
    params = {}
    if isins:
        sql += " WHERE isin = ANY(:isins)"
        params["isins"] = [i.upper() for i in isins]
    sql += " ORDER BY isin"
    if limit:
        sql += " LIMIT :limit"
        params["limit"] = limit
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def known_downloads(engine, stock_isin: str) -> tuple[dict, dict]:
    """Return ({source_url: file_path}, {file_hash: file_path}) for a company."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT source_url, file_hash, file_path FROM downloaded_files WHERE stock_isin = :c"),
            {"c": stock_isin},
        ).all()
    return {r.source_url: r.file_path for r in rows}, {r.file_hash: r.file_path for r in rows}


def load_crawl(engine, stock_isin: str, website_url: str, max_age_days: float):
    """Return the saved crawl of a company, or None if it must be crawled again.

    A saved crawl is used only if it is younger than `max_age_days`, was made for the
    same `website_url`, and found at least one PDF. Returns (meta, rows): `meta` is the
    `crawl_runs` row (plus `age_days`), `rows` are the PDF links of that crawl.
    """
    if max_age_days <= 0:
        return None
    with engine.connect() as conn:
        meta = conn.execute(
            text("SELECT *, EXTRACT(EPOCH FROM now() - crawled_at) / 86400 AS age_days "
                 "FROM crawl_runs WHERE stock_isin = :c"),
            {"c": stock_isin},
        ).first()
        if meta is None or meta.website_url != website_url or meta.age_days > max_age_days or not meta.pdf_links:
            return None
        # only links seen by that latest crawl: older ones are no longer on the site
        rows = conn.execute(
            text("SELECT source_url, link_text, context, page_url, page_title, page_relevance "
                 "FROM discovered_pdfs WHERE stock_isin = :c AND last_seen = :t ORDER BY source_url"),
            {"c": stock_isin, "t": meta.crawled_at},
        ).all()
    return dict(meta._mapping), [dict(r._mapping) for r in rows]


def save_crawl(engine, stock_isin: str, website_url: str, result) -> None:
    """Store a finished crawl (a `CrawlResult`). Links not seen this time are kept, with an older `last_seen`."""
    with engine.begin() as conn:
        now = conn.execute(text("SELECT now()")).scalar()
        conn.execute(
            text(
                """
                INSERT INTO crawl_runs
                    (stock_isin, crawled_at, website_url, base_url, english, pages_visited, used_js, pdf_links)
                VALUES (:c, :t, :w, :b, :e, :p, :j, :n)
                ON CONFLICT (stock_isin) DO UPDATE SET
                    crawled_at = EXCLUDED.crawled_at, website_url = EXCLUDED.website_url,
                    base_url = EXCLUDED.base_url, english = EXCLUDED.english,
                    pages_visited = EXCLUDED.pages_visited, used_js = EXCLUDED.used_js,
                    pdf_links = EXCLUDED.pdf_links
                """
            ),
            {"c": stock_isin, "t": now, "w": website_url, "b": result.base_url, "e": result.english,
             "p": result.pages_visited, "j": result.used_js, "n": len(result.pdfs)},
        )
        rows = [
            {"c": stock_isin, "u": pdf.url, "x": pdf.text[:500], "ctx": pdf.context, "pu": pdf.page_url,
             "pt": pdf.page_title[:500], "pr": pdf.page_relevance, "t": now}
            for pdf in result.pdfs.values()
        ]
        if rows:
            conn.execute(
                text(
                    """
                    INSERT INTO discovered_pdfs
                        (stock_isin, source_url, link_text, context, page_url, page_title,
                         page_relevance, first_seen, last_seen)
                    VALUES (:c, :u, :x, :ctx, :pu, :pt, :pr, :t, :t)
                    ON CONFLICT (stock_isin, source_url) DO UPDATE SET
                        link_text = EXCLUDED.link_text, context = EXCLUDED.context,
                        page_url = EXCLUDED.page_url, page_title = EXCLUDED.page_title,
                        page_relevance = EXCLUDED.page_relevance, last_seen = EXCLUDED.last_seen
                    """
                ),
                rows,
            )


def record_download(engine, row: dict) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO downloaded_files
                    (stock_isin, category, source_url, page_url, link_text,
                     file_path, file_hash, file_size, fiscal_year)
                VALUES (:stock_isin, :category, :source_url, :page_url, :link_text,
                        :file_path, :file_hash, :file_size, :fiscal_year)
                ON CONFLICT (stock_isin, source_url) DO UPDATE SET
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

    def __init__(self, engine, stock_isin=None):
        self.engine = engine
        self.stock_isin = stock_isin
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
                        "INSERT INTO download_errors (stock_isin, url, stage, error, http_status, traceback) "
                        "VALUES (:c, :u, :s, :e, :h, :t)"
                    ),
                    {"c": self.stock_isin, "u": url, "s": stage, "e": message[:4000],
                     "h": http_status, "t": trace},
                )
        except Exception as db_exc:  # noqa: BLE001
            print(f"    could not save error to database: {db_exc}", file=sys.stderr)
