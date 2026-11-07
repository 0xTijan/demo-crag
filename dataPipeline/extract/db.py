"""Database access for the extraction step.

Reads:   report_files + downloaded_files + stocks (what to extract and for which company),
         daily_prices (share price at period end), reports/financials (earlier periods, for checks).
Writes:  reports, financials, valuation_ratios (schema.sql), and the two tables owned by
         this step:
  * report_extractions        one row per (markdown_path, markdown_hash, extractor_version):
                              status, scope, unit, model, every number with its page and
                              row label (items), and the failed checks.
  * report_extraction_errors  append-only failure log.
"""
from __future__ import annotations

import json
import os
import sys
import traceback as tb
from datetime import date

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://username:postgres@localhost:5432/postgres",
)

DDL = [
    """
    CREATE TABLE IF NOT EXISTS report_extractions (
        extraction_id     SERIAL PRIMARY KEY,
        markdown_path     TEXT     NOT NULL,
        markdown_hash     CHAR(64) NOT NULL,
        extractor_version TEXT     NOT NULL,
        stock_isin        CHAR(12) REFERENCES stocks(isin) ON DELETE CASCADE,
        report_id         INT REFERENCES reports(report_id) ON DELETE SET NULL,
        status            TEXT NOT NULL CHECK (status IN ('extracted','needs_review','skipped','failed')),
        scope             TEXT CHECK (scope IN ('consolidated','separate')),
        unit_scale        INT,
        model             TEXT,
        items             JSONB,
        checks            JSONB,
        error             TEXT,
        extracted_at      TIMESTAMPTZ DEFAULT now(),
        UNIQUE (markdown_path, markdown_hash, extractor_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS report_extractions_report_idx ON report_extractions (report_id)",
    """
    CREATE TABLE IF NOT EXISTS report_extraction_errors (
        error_id      SERIAL PRIMARY KEY,
        markdown_path TEXT NOT NULL,
        markdown_hash CHAR(64),
        stage         TEXT NOT NULL,
        error         TEXT NOT NULL,
        traceback     TEXT,
        occurred_at   TIMESTAMPTZ DEFAULT now()
    )
    """,
]

# Unique keys on schema.sql tables, so a rerun updates rows instead of adding them.
UNIQUE_KEYS = [
    ("reports_period_key", "reports", "stock_isin, period_end, period_type"),
    ("financials_report_key", "financials", "report_id"),
    ("valuation_ratios_report_key", "valuation_ratios", "report_id"),
]
SERIALS = [("reports", "report_id"), ("financials", "financial_id"), ("valuation_ratios", "valuation_id")]


def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def safe_url() -> str:
    """Host/database part of the URL only, so the password is never printed."""
    return DATABASE_URL.split("@")[-1]


def ensure_tables(engine) -> None:
    """Create the tracking tables and the unique keys. Exits if existing rows violate a key."""
    with engine.begin() as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
        for name, table, cols in UNIQUE_KEYS:
            if conn.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar():
                continue
            dups = conn.execute(text(
                f"SELECT {cols}, count(*) AS n FROM {table} GROUP BY {cols} HAVING count(*) > 1 LIMIT 20"
            )).all()
            if dups:
                listing = "\n".join("  " + ", ".join(str(v) for v in row) for row in dups)
                sys.exit(f"Cannot add unique key {name} on {table} ({cols}): duplicate rows exist.\n"
                         f"Remove the duplicates first:\n{listing}")
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
        # rows inserted with explicit ids (sample_data.sql) can leave a sequence behind
        for table, col in SERIALS:
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table, "c": col}).scalar()
            if seq:
                conn.execute(text(
                    f"SELECT setval('{seq}', GREATEST((SELECT COALESCE(MAX({col}), 0) FROM {table}), "
                    f"(SELECT last_value FROM {seq})), true)"
                ))


def table_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar())


INPUTS_SQL = """
    SELECT DISTINCT ON (rf.markdown_path)
           rf.markdown_path, rf.pdf_path, df.stock_isin, df.fiscal_year, df.link_text,
           s.name, s.nace, s.sector_name, s.quantity
    FROM report_files rf
    JOIN downloaded_files df ON df.file_path = rf.pdf_path
    JOIN stocks s ON s.isin = df.stock_isin
    WHERE rf.status = 'converted' AND rf.markdown_path IS NOT NULL
"""


def list_inputs(engine, isins: list[str] | None = None, markdown_paths: list[str] | None = None,
                any_category: bool = False) -> list[dict]:
    """Converted Markdown files with their company, newest conversion per file.

    Only files the download step filed as reports, unless any_category (used for
    --file: a file named on the command line is extracted whatever its category,
    since the download step's report/announcement guess can be wrong).
    """
    sql, params = INPUTS_SQL, {}
    if not any_category:
        sql += " AND df.category = 'reports'"
    if isins:
        sql += " AND df.stock_isin = ANY(:isins)"
        params["isins"] = [i.upper() for i in isins]
    if markdown_paths:
        sql += " AND rf.markdown_path = ANY(:paths)"
        params["paths"] = markdown_paths
    sql += " ORDER BY rf.markdown_path, rf.converted_at DESC, df.downloaded_at DESC"
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def company(engine, isin: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT isin AS stock_isin, name, nace, sector_name, quantity FROM stocks "
                                "WHERE isin = :i"), {"i": isin.upper()}).first()
    return dict(row._mapping) if row else None


def previous_status(engine, markdown_path: str, markdown_hash: str, version: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT status FROM report_extractions WHERE markdown_path = :p AND markdown_hash = :h "
            "AND extractor_version = :v"), {"p": markdown_path, "h": markdown_hash, "v": version}).scalar()


def price_at(engine, isin: str, period_end: date):
    """Last closing price on or up to 14 days before period_end (period ends often fall on a weekend)."""
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT last_price FROM daily_prices WHERE stock_isin = :i AND date <= :d "
            "AND date > CAST(:d AS DATE) - 14 AND last_price IS NOT NULL ORDER BY date DESC LIMIT 1"
        ), {"i": isin, "d": period_end}).scalar()


def stored_report(engine, isin: str, period_end: date, period_type: str) -> dict | None:
    """The reports + financials row already stored for this period, if any."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT r.report_id, r.source_file, f.revenue, f.ebitda, f.ebit, f.net_income, "
            "f.earnings_per_share, f.total_assets, f.total_equity FROM reports r LEFT JOIN financials f "
            "ON f.report_id = r.report_id WHERE r.stock_isin = :i AND r.period_end = :d AND r.period_type = :t"
        ), {"i": isin, "d": period_end, "t": period_type}).first()
    return dict(row._mapping) if row else None


def siblings(engine, isin: str, year: int, exclude_type: str) -> dict:
    """{period_type: revenue} of the company's other stored reports ending in `year`."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT r.period_type, f.revenue FROM reports r JOIN financials f ON f.report_id = r.report_id "
            "WHERE r.stock_isin = :i AND EXTRACT(YEAR FROM r.period_end) = :y AND r.period_type <> :t"
        ), {"i": isin, "y": year, "t": exclude_type}).all()
    return {r.period_type: r.revenue for r in rows}


REPORT_UPSERT = text("""
    INSERT INTO reports (stock_isin, period_end, period_type, currency, source_type, source_file)
    VALUES (:stock_isin, :period_end, :period_type, :currency, :source_type, :source_file)
    ON CONFLICT (stock_isin, period_end, period_type) DO UPDATE SET
        currency = EXCLUDED.currency, source_type = EXCLUDED.source_type, source_file = EXCLUDED.source_file
    RETURNING report_id
""")


def _upsert_by_report(conn, table: str, report_id: int, values: dict) -> None:
    cols = list(values)
    conn.execute(text(
        f"INSERT INTO {table} (report_id, {', '.join(cols)}) VALUES (:report_id, {', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT (report_id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}"
    ), {"report_id": report_id, **values})


EXTRACTION_UPSERT = text("""
    INSERT INTO report_extractions
        (markdown_path, markdown_hash, extractor_version, stock_isin, report_id, status, scope,
         unit_scale, model, items, checks, error, extracted_at)
    VALUES (:markdown_path, :markdown_hash, :extractor_version, :stock_isin, :report_id, :status, :scope,
            :unit_scale, :model, CAST(:items AS JSONB), CAST(:checks AS JSONB), :error, now())
    ON CONFLICT (markdown_path, markdown_hash, extractor_version) DO UPDATE SET
        stock_isin = EXCLUDED.stock_isin, report_id = EXCLUDED.report_id, status = EXCLUDED.status,
        scope = EXCLUDED.scope, unit_scale = EXCLUDED.unit_scale, model = EXCLUDED.model,
        items = EXCLUDED.items, checks = EXCLUDED.checks, error = EXCLUDED.error, extracted_at = now()
""")

EXTRACTION_FIELDS = ["markdown_path", "markdown_hash", "extractor_version", "stock_isin", "report_id",
                     "status", "scope", "unit_scale", "model", "items", "checks", "error"]


def _extraction_params(row: dict) -> dict:
    params = {k: row.get(k) for k in EXTRACTION_FIELDS}
    for k in ("items", "checks"):
        params[k] = json.dumps(params[k], default=str) if params[k] is not None else None
    return params


def load(engine, report: dict | None, fin: dict | None, val: dict | None, extraction: dict) -> int | None:
    """Write everything for one file in one transaction. Returns the report_id (None if not loaded).

    report/fin/val None = only record the extraction (skipped, failed, or a duplicate
    of a period already stored from another file).
    """
    with engine.begin() as conn:
        report_id = extraction.get("report_id")
        if report is not None:
            report_id = conn.execute(REPORT_UPSERT, report).scalar()
            _upsert_by_report(conn, "financials", report_id, fin)
            _upsert_by_report(conn, "valuation_ratios", report_id, val)
        conn.execute(EXTRACTION_UPSERT, _extraction_params({**extraction, "report_id": report_id}))
    return report_id


def record(engine, extraction: dict) -> None:
    """Record a skipped/failed file. Never raises (the DB may be the failure)."""
    try:
        load(engine, None, None, None, extraction)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not save status to database: {exc}", file=sys.stderr)


def log_error(engine, markdown_path: str, markdown_hash: str | None, stage: str, exc: BaseException) -> None:
    """Append one failure to report_extraction_errors. Never raises."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO report_extraction_errors (markdown_path, markdown_hash, stage, error, traceback) "
                "VALUES (:p, :h, :s, :e, :t)"
            ), {"p": markdown_path, "h": markdown_hash, "s": stage,
                "e": (str(exc) or exc.__class__.__name__)[:4000],
                "t": "".join(tb.format_exception(type(exc), exc, exc.__traceback__))})
    except Exception as db_exc:  # noqa: BLE001
        print(f"  could not save error to database: {db_exc}", file=sys.stderr)
