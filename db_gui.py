"""
Simple AI generated DB explorer for checking RAG answers against the source data.
"""

from dotenv import load_dotenv
load_dotenv()
import os
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, inspect, text

st.set_page_config(page_title="RAG DB Explorer", page_icon="📈", layout="wide")

DEFAULT_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://username:postgres@localhost:5432/postgres")


@st.cache_resource
def get_engine(url: str):
    return create_engine(url, pool_pre_ping=True)


def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    with get_engine(st.session_state["db_url"]).connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


@st.cache_data(ttl=120, show_spinner=False)
def distinct(url: str, sql: str) -> list:
    with get_engine(url).connect() as conn:
        return [r[0] for r in conn.execute(text(sql)).fetchall() if r[0] is not None]


def in_clause(col: str, values: list, prefix: str, params: dict) -> str:
    names = []
    for i, v in enumerate(values):
        k = f"{prefix}{i}"
        params[k] = v
        names.append(f":{k}")
    return f"{col} IN ({', '.join(names)})"


with st.sidebar:
    st.header("Connection")
    st.session_state["db_url"] = st.text_input("Database URL", DEFAULT_URL, type="password")
    try:
        insp = inspect(get_engine(st.session_state["db_url"]))
        tables = sorted(insp.get_table_names())
        st.success(f"Connected · {len(tables)} tables")
    except Exception as e:  # noqa: BLE001
        st.error(f"Cannot connect: {e}")
        st.stop()
    if st.button("Clear cache / refresh"):
        st.cache_data.clear()
        st.rerun()
    with st.expander("Row counts"):
        for t in tables:
            try:
                n = query(f'SELECT COUNT(*) AS n FROM "{t}"')["n"][0]
                st.write(f"`{t}`: {n:,}")
            except Exception:  # noqa: BLE001
                pass

st.title("📈 RAG DB Explorer")
tab_stocks, tab_idx, tab_fin, tab_tables, tab_sql = st.tabs(
    ["🔎 Stocks", "📊 Indexes", "🧾 Financials", "🗂️ Tables", "⌨️ SQL"]
)

# ------------------------------------------------------------------- stocks
with tab_stocks:
    url = st.session_state["db_url"]
    c1, c2, c3 = st.columns([2, 2, 3])
    mics = c1.multiselect("Exchange (MIC)", distinct(url, "SELECT DISTINCT mic FROM stocks ORDER BY 1"))
    sectors = c2.multiselect("Sector", distinct(url, "SELECT DISTINCT sector_name FROM stocks ORDER BY 1"))
    search = c3.text_input("Search name / symbol / ISIN / description", placeholder="e.g. hydro, bank, SAVB ...")

    c4, c5, c6 = st.columns([2, 3, 3])
    idx_opts = {"(any)": None}
    for iso, sym in query("SELECT isin, symbol FROM indexes ORDER BY symbol").itertuples(index=False):
        idx_opts[sym] = iso
    idx_choice = c4.selectbox("Member of index", list(idx_opts))
    quick = c5.radio("Quick filter", ["All", "Top gainers", "Top losers", "Largest market cap", "Highest price"],
                     horizontal=True)
    price_max = float(query("SELECT COALESCE(MAX(last_price),0) AS m FROM stocks")["m"][0] or 0)
    price_rng = c6.slider("Last price range (EUR)", 0.0, max(price_max, 1.0), (0.0, max(price_max, 1.0)))

    where, params = ["last_price BETWEEN :pmin AND :pmax"], {"pmin": price_rng[0], "pmax": price_rng[1]}
    if mics:
        where.append(in_clause("mic", mics, "m", params))
    if sectors:
        where.append(in_clause("sector_name", sectors, "s", params))
    if search:
        params["q"] = f"%{search.lower()}%"
        where.append("(LOWER(name) LIKE :q OR LOWER(symbol) LIKE :q OR LOWER(isin) LIKE :q OR LOWER(description) LIKE :q)")
    if idx_choice != "(any)":
        params["idx"] = idx_opts[idx_choice]
        where.append("isin IN (SELECT stock_isin FROM index_members WHERE index_isin = :idx)")
    order = {"All": "symbol", "Top gainers": "change_prev_close_percentage DESC",
             "Top losers": "change_prev_close_percentage ASC", "Largest market cap": "(last_price*quantity) DESC",
             "Highest price": "last_price DESC"}[quick]
    df = query(
        f"""SELECT isin, mic, symbol, name, sector_name, last_price,
                   change_prev_close_percentage AS change_pct, quantity,
                   ROUND(last_price*quantity/1000000.0, 1) AS mcap_m_eur, first_trading_date
            FROM stocks WHERE {' AND '.join(where)} ORDER BY {order}""", params)

    st.caption(f"{len(df)} stocks · click a row to see details")
    ev = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      column_config={"change_pct": st.column_config.NumberColumn("change %", format="%.2f")})

    if ev.selection.rows:
        row = df.iloc[ev.selection.rows[0]]
        isin = row["isin"]
        st.divider()
        st.subheader(f"{row['name']} ({row['symbol']}) · {row['isin']}")
        info = query("SELECT description, nace, website_url, logo_url, web_id FROM stocks WHERE isin=:i", {"i": isin}).iloc[0]
        m1, m2, m3 = st.columns(3)
        m1.metric("Last price", f"{row['last_price']:.2f}", f"{row['change_pct']:.2f}%")
        m2.metric("Market cap (M EUR)", f"{row['mcap_m_eur']:,.1f}")
        m3.metric("Sector", row["sector_name"])
        st.write(info["description"])
        st.caption(f"NACE: {info['nace']} · {info['website_url']} · web_id: {info['web_id']}")

        d1, d2, d3, d4 = st.tabs(["Price history", "Financials", "Valuation", "Index memberships"])
        with d1:
            px = query("SELECT date, open_price, high_price, low_price, last_price, volume, turnover, num_trades "
                       "FROM daily_prices WHERE stock_isin=:i ORDER BY date", {"i": isin})
            if px.empty:
                st.info("No price data.")
            else:
                px["date"] = pd.to_datetime(px["date"])
                st.line_chart(px.set_index("date")["last_price"])
                st.dataframe(px.sort_values("date", ascending=False), hide_index=True, width="stretch")
        with d2:
            fin = query("""SELECT r.period_end, r.period_type, r.source_file, f.revenue, f.ebitda, f.ebit, f.net_income,
                                  f.earnings_per_share AS eps, f.total_assets, f.total_equity, f.roe, f.roa
                           FROM reports r JOIN companies c ON c.company_id=r.company_id
                           JOIN financials f ON f.report_id=r.report_id
                           WHERE c.isin=:i ORDER BY r.period_end DESC""", {"i": isin})
            st.dataframe(fin, hide_index=True, width="stretch")
        with d3:
            val = query("""SELECT r.period_end, r.period_type, v.p_e, v.p_b, v.p_s, v.ev_s, v.net_debt_ebitda,
                                  v.dividend_per_share, v.dividend_yield
                           FROM reports r JOIN companies c ON c.company_id=r.company_id
                           JOIN valuation_ratios v ON v.report_id=r.report_id
                           WHERE c.isin=:i ORDER BY r.period_end DESC""", {"i": isin})
            st.dataframe(val, hide_index=True, width="stretch")
        with d4:
            mem = query("""SELECT i.symbol AS index_symbol, i.name AS index_name, m.weight
                           FROM index_members m JOIN indexes i ON i.isin=m.index_isin WHERE m.stock_isin=:i""", {"i": isin})
            st.dataframe(mem, hide_index=True, width="stretch")

# ------------------------------------------------------------------ indexes
with tab_idx:
    idx_df = query("SELECT isin, mic, symbol, name, last_value, change_prev_close_percentage AS change_pct FROM indexes ORDER BY symbol")
    st.dataframe(idx_df, hide_index=True, width="stretch")
    if not idx_df.empty:
        sel = st.selectbox("Index", idx_df["symbol"], key="idx_sel")
        iso = idx_df.loc[idx_df["symbol"] == sel, "isin"].iloc[0]
        vals = query("SELECT date, open_value, high_value, low_value, last_value, change_prev_close_percentage AS change_pct, turnover "
                     "FROM index_values WHERE index_isin=:i ORDER BY date", {"i": iso})
        vals["date"] = pd.to_datetime(vals["date"])
        a, b = st.columns([3, 2])
        with a:
            st.subheader("Value history")
            st.line_chart(vals.set_index("date")["last_value"])
        with b:
            st.subheader("Members")
            st.dataframe(query("""SELECT s.symbol, s.name, m.weight, s.last_price, s.change_prev_close_percentage AS change_pct
                                  FROM index_members m JOIN stocks s ON s.isin=m.stock_isin
                                  WHERE m.index_isin=:i ORDER BY m.weight DESC""", {"i": iso}),
                         hide_index=True, width="stretch")
        with st.expander("Raw index values"):
            st.dataframe(vals.sort_values("date", ascending=False), hide_index=True, width="stretch")

# ---------------------------------------------------------------- financials
with tab_fin:
    url = st.session_state["db_url"]
    f1, f2, f3, f4 = st.columns(4)
    f_period = f1.multiselect("Period type", ["Q1", "Q2", "Q3", "Q4", "1H", "FY"], default=["FY"])
    f_src = f2.multiselect("Source country", distinct(url, "SELECT DISTINCT source_type FROM reports ORDER BY 1"))
    f_year = f3.multiselect("Year", distinct(url, "SELECT DISTINCT EXTRACT(YEAR FROM period_end) FROM reports ORDER BY 1")
                            if not url.startswith("sqlite") else
                            distinct(url, "SELECT DISTINCT substr(period_end,1,4) FROM reports ORDER BY 1"))
    f_q = f4.text_input("Company name / symbol", key="fin_q")
    where, params = ["1=1"], {}
    if f_period:
        where.append(in_clause("r.period_type", f_period, "p", params))
    if f_src:
        where.append(in_clause("r.source_type", f_src, "c", params))
    if f_year:
        years = [int(float(y)) for y in f_year]
        ycol = "CAST(substr(r.period_end,1,4) AS INTEGER)" if url.startswith("sqlite") else "EXTRACT(YEAR FROM r.period_end)"
        where.append(in_clause(ycol, years, "y", params))
    if f_q:
        params["q"] = f"%{f_q.lower()}%"
        where.append("(LOWER(c.name) LIKE :q OR LOWER(s.symbol) LIKE :q)")
    fdf = query(f"""SELECT s.symbol, c.name, r.period_end, r.period_type, r.currency, f.revenue, f.ebitda, f.ebit, f.net_income,
                           f.earnings_per_share AS eps, f.total_assets, f.total_equity, f.roe, f.roa,
                           f.liabilities_equity_ratio AS l_e_ratio, v.p_e, v.p_b, v.p_s, v.ev_s, v.net_debt_ebitda,
                           v.dividend_per_share AS dps, v.dividend_yield AS div_yield
                    FROM reports r
                    JOIN companies c ON c.company_id=r.company_id JOIN stocks s ON s.isin=c.isin
                    JOIN financials f ON f.report_id=r.report_id
                    LEFT JOIN valuation_ratios v ON v.report_id=r.report_id
                    WHERE {' AND '.join(where)} ORDER BY r.period_end DESC, c.name""", params)
    st.caption(f"{len(fdf)} rows")
    st.dataframe(fdf, hide_index=True, width="stretch")
    st.download_button("Download CSV", fdf.to_csv(index=False), "financials.csv", "text/csv")

# -------------------------------------------------------------- table browser
with tab_tables:
    t = st.selectbox("Table", tables, key="tbl")
    cols = [c["name"] for c in inspect(get_engine(st.session_state["db_url"])).get_columns(t)]
    g1, g2, g3, g4 = st.columns([3, 2, 2, 1])
    s_all = g1.text_input("Search across all columns", key="tbl_q")
    s_col = g2.selectbox("Sort by", ["(none)"] + cols)
    s_dir = g3.radio("Direction", ["ASC", "DESC"], horizontal=True)
    lim = g4.number_input("Limit", 10, 100000, 500, step=100)
    params = {}
    sql = f'SELECT * FROM "{t}"'
    if s_all:
        params["q"] = f"%{s_all.lower()}%"
        sql += " WHERE " + " OR ".join(f'LOWER(CAST("{c}" AS TEXT)) LIKE :q' for c in cols)
    if s_col != "(none)":
        sql += f' ORDER BY "{s_col}" {s_dir}'
    sql += f" LIMIT {int(lim)}"
    tdf = query(sql, params)
    st.caption(f"{len(tdf)} rows shown")
    st.dataframe(tdf, hide_index=True, width="stretch")
    st.download_button("Download CSV", tdf.to_csv(index=False), f"{t}.csv", "text/csv")
    with st.expander("Columns"):
        st.dataframe(pd.DataFrame(inspect(get_engine(st.session_state["db_url"])).get_columns(t))[["name", "type", "nullable"]]
                     .astype(str), hide_index=True)

# ----------------------------------------------------------------------- SQL
EXAMPLES = {
    "(pick an example)": "",
    "Top 10 stocks by market cap": "SELECT symbol, name, last_price, quantity, last_price*quantity AS mcap\nFROM stocks ORDER BY mcap DESC LIMIT 10;",
    "Highest ROE (FY 2025)": "SELECT s.symbol, c.name, f.roe\nFROM financials f JOIN reports r ON r.report_id=f.report_id\nJOIN companies c ON c.company_id=r.company_id JOIN stocks s ON s.isin=c.isin\nWHERE r.period_type='FY' AND r.period_end='2025-12-31' ORDER BY f.roe DESC LIMIT 10;",
    "Average P/E per sector": "SELECT s.sector_name, ROUND(AVG(v.p_e),2) AS avg_pe\nFROM valuation_ratios v JOIN reports r ON r.report_id=v.report_id\nJOIN companies c ON c.company_id=r.company_id JOIN stocks s ON s.isin=c.isin\nWHERE r.period_type='FY' GROUP BY s.sector_name ORDER BY avg_pe;",
    "Index weights": "SELECT i.symbol AS idx, s.symbol, m.weight FROM index_members m\nJOIN indexes i ON i.isin=m.index_isin JOIN stocks s ON s.isin=m.stock_isin\nORDER BY i.symbol, m.weight DESC;",
}
with tab_sql:
    ex = st.selectbox("Examples", list(EXAMPLES))
    sql_text = st.text_area("SQL", value=EXAMPLES[ex] or "SELECT * FROM stocks LIMIT 20;", height=180, key=f"sql_{ex}")
    allow_write = st.checkbox("Allow write statements (INSERT / UPDATE / DELETE / DDL)", value=False)
    if st.button("▶ Run", type="primary"):
        stmt = sql_text.strip().rstrip(";")
        is_read = stmt.lower().split(None, 1)[0] in ("select", "with", "explain", "show", "values") if stmt else False
        if not stmt:
            st.warning("Empty query.")
        elif not is_read and not allow_write:
            st.error("Only read queries are allowed. Tick the checkbox above to enable writes.")
        else:
            try:
                if is_read:
                    res = query(stmt)
                    st.session_state["sql_res"] = res
                    st.success(f"{len(res)} rows")
                else:
                    with get_engine(st.session_state["db_url"]).begin() as conn:
                        r = conn.execute(text(stmt))
                    st.session_state["sql_res"] = None
                    st.success(f"Done. Rows affected: {r.rowcount}")
            except Exception as e:  # noqa: BLE001
                st.session_state["sql_res"] = None
                st.error(str(e))
    res = st.session_state.get("sql_res")
    if res is not None:
        st.dataframe(res, hide_index=True, width="stretch")
        st.download_button("Download CSV", res.to_csv(index=False), "query_result.csv", "text/csv")