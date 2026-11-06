-- Table: stocks
CREATE TABLE stocks (
    isin CHAR(12) PRIMARY KEY,
    mic VARCHAR(10) NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    name TEXT,
    nace TEXT,
    sector_id VARCHAR(10),
    sector_name TEXT,
    first_trading_date DATE,
    quantity BIGINT,
    description TEXT,
    logo_url TEXT,
    website_url TEXT,
    web_id TEXT,
    last_price NUMERIC(10, 2),
    change_prev_close_percentage NUMERIC(6, 2),
    CONSTRAINT unique_stock UNIQUE (mic, symbol)
);

-- Table: indexes
CREATE TABLE indexes (
    isin CHAR(12) PRIMARY KEY,
    mic VARCHAR(10) NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    name TEXT,
    last_value NUMERIC(10, 2),
    change_prev_close_percentage NUMERIC(6, 2),
    CONSTRAINT unique_index UNIQUE (mic, symbol)
);

-- Table: index_members
CREATE TABLE index_members (
    index_isin CHAR(12) NOT NULL,
    stock_isin CHAR(12) NOT NULL,
    weight NUMERIC(6, 4),  -- e.g., 12.3456%, can go up to 9999.9999 if needed
    PRIMARY KEY (index_isin, stock_isin),
    FOREIGN KEY (index_isin) REFERENCES indexes(isin) ON DELETE CASCADE,
    FOREIGN KEY (stock_isin) REFERENCES stocks(isin) ON DELETE CASCADE
);

-- Table: daily_prices
CREATE TABLE daily_prices (
    id SERIAL PRIMARY KEY,
    stock_isin CHAR(12) REFERENCES stocks(isin) ON DELETE CASCADE,
    date DATE NOT NULL,
    trading_model_id VARCHAR(10),
    open_price NUMERIC(10, 2),
    high_price NUMERIC(10, 2),
    low_price NUMERIC(10, 2),
    last_price NUMERIC(10, 2),
    vwap_price NUMERIC(15, 8),
    change_prev_close_percentage NUMERIC(6, 2),
    num_trades INTEGER,
    volume NUMERIC(20, 5),
    turnover NUMERIC(20, 2),
    price_currency CHAR(3),
    turnover_currency CHAR(3),
    CONSTRAINT unique_stock_date UNIQUE (stock_isin, date)
);

-- Table: index_values
CREATE TABLE index_values (
    id SERIAL PRIMARY KEY,
    index_isin CHAR(12) REFERENCES indexes(isin) ON DELETE CASCADE,
    date DATE NOT NULL,
    open_value NUMERIC(10, 2),
    high_value NUMERIC(10, 2),
    low_value NUMERIC(10, 2),
    last_value NUMERIC(10, 2),
    change_prev_close_percentage NUMERIC(6, 2),
    turnover NUMERIC(20, 2),
    CONSTRAINT unique_index_date UNIQUE (index_isin, date)
);

CREATE TABLE reports (
    report_id SERIAL PRIMARY KEY,
    stock_isin CHAR(12) NOT NULL REFERENCES stocks(isin) ON DELETE CASCADE,
    period_end DATE NOT NULL,
    period_type VARCHAR(10) CHECK (period_type IN ('Q1','Q2','Q3','Q4','1H','FY')),
    currency VARCHAR(10) DEFAULT 'EUR',
    source_type VARCHAR(20) CHECK (source_type IN ('Slovenia','Croatia','Austria','Other')),
    source_file TEXT
);

-- ==========================
-- 3. Core Financials
-- ==========================
CREATE TABLE financials (
    financial_id SERIAL PRIMARY KEY,
    report_id INT REFERENCES reports(report_id) ON DELETE CASCADE,
    revenue NUMERIC(20,2),
    ebitda NUMERIC(20,2),
    ebit NUMERIC(20,2),
    net_income NUMERIC(20,2),
    earnings_per_share NUMERIC(20,4),
    total_assets NUMERIC(20,2),
    total_equity NUMERIC(20,2),
    roe NUMERIC(10,4), -- in %
    roa NUMERIC(10,4), -- in %
    liabilities_equity_ratio NUMERIC(10,4),
    net_working_capital NUMERIC(20,2),
    net_liabilities NUMERIC(20,2)
);

-- ==========================
-- 4. Valuation Ratios
-- ==========================
CREATE TABLE valuation_ratios (
    valuation_id SERIAL PRIMARY KEY,
    report_id INT REFERENCES reports(report_id) ON DELETE CASCADE,
    p_e NUMERIC(10,4),
    p_b NUMERIC(10,4),
    p_s NUMERIC(10,4),
    ev_s NUMERIC(10,4),
    net_debt_ebitda NUMERIC(10,4),
    dividend_per_share NUMERIC(10,4),
    dividend_yield NUMERIC(10,4)
);
-- ==========================
-- 5. Converted report PDFs (dataPipeline/convert/docling.py)
-- ==========================
CREATE TABLE IF NOT EXISTS report_files (
    file_id           SERIAL PRIMARY KEY,
    pdf_path          TEXT        NOT NULL,          -- relative to reports/
    file_hash         CHAR(64)    NOT NULL,          -- SHA-256 of the PDF bytes
    file_size         BIGINT,
    markdown_path     TEXT,                          -- relative to repo root
    status            TEXT        NOT NULL CHECK (status IN ('converted','failed')),
    pages             INT,
    tables            INT,
    converter         TEXT        NOT NULL DEFAULT 'docling',  -- docling | pdfplumber
    converter_version TEXT,
    error             TEXT,                          -- "<stage>: <message>" when failed
    ocr_fallback_pages INT,                          -- pages re-read by the vision model
    ocr_fallback_model TEXT,                         -- e.g. qwen2.5vl:7b
    ocr_empty_events  INT,                           -- RapidOCR 'empty result' log count (hint)
    converted_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (pdf_path, file_hash, converter)          -- each pipeline tracks its own conversions
);
CREATE INDEX IF NOT EXISTS report_files_path_idx ON report_files (pdf_path, converted_at DESC);

CREATE TABLE IF NOT EXISTS report_file_errors (
    error_id    SERIAL PRIMARY KEY,
    pdf_path    TEXT NOT NULL,
    file_hash   CHAR(64),
    stage       TEXT NOT NULL,                       -- hash | convert | write | db | ruled_tables | ocr_fallback
    error       TEXT NOT NULL,
    traceback   TEXT,
    occurred_at TIMESTAMPTZ DEFAULT now(),
    converter   TEXT                                 -- pipeline that hit the error
);
