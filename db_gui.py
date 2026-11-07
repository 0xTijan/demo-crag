"""
Simple AI generated DB explorer for checking RAG answers against the source data.
"""

from dotenv import load_dotenv
load_dotenv()
import os
import re
import html
from pathlib import Path
import json
import hashlib
import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

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


# UI state lives in the URL so a page refresh (or a shared link) restores it. Widgets use Streamlit's
# bind="query-params" (key = query parameter name, default values are left out of the URL); tabs go
# through url_tabs() so the URL holds a short slug instead of the emoji label.
def url_tabs(param: str, tabs: dict[str, str]):
    """st.tabs ({slug: label}) whose active tab is kept in the URL as ?<param>=<slug>."""
    slugs = {label: slug for slug, label in tabs.items()}
    first = next(iter(tabs))

    def sync():
        slug = slugs[st.session_state[param]]
        if slug == first:
            st.query_params.pop(param, None)
        else:
            st.query_params[param] = slug

    if param in st.query_params and st.query_params[param] not in tabs:
        st.query_params.pop(param)  # unknown slug: fall back to the first tab
    default = tabs[st.query_params.get(param, first)]
    return st.tabs(list(tabs.values()), key=param, default=default, on_change=sync)


# ------------------------------------------------------------------ look & feel
# Chart colours: validated categorical slots 1-2 (blue, orange), stepped for each surface; "up"/"down" are the
# status colours used for gains / losses (always together with a +/- sign, never colour alone).
_COLORS = {
    "light": {"s1": "#2a78d6", "s2": "#eb6834", "rule": "#8d8c87", "surface": "#fcfcfb", "up": "#006300", "down": "#c62f2f"},
    "dark": {"s1": "#3987e5", "s2": "#d95926", "rule": "#8d8c87", "surface": "#1a1a19", "up": "#3fbf3f", "down": "#ee7b7b"},
}
RANGES = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "All": None}


def colors() -> dict:
    return _COLORS["dark" if st.context.theme.type == "dark" else "light"]


def compact(v, prefix: str = "") -> str:
    """1234567 -> 1.2M (for headline numbers; tables use the column formats)."""
    if v is None or pd.isna(v):
        return "–"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{prefix}{v / div:,.1f}{suffix}"
    return f"{prefix}{v:,.0f}"


def signed_color(v) -> str:
    """Styler cell style: gains / losses in the status colours (the +/- sign carries the meaning too)."""
    if v is None or pd.isna(v) or v == 0:
        return ""
    return f"color: {colors()['up' if v > 0 else 'down']}"


def range_picker(key: str):
    return st.segmented_control("Range", list(RANGES), default="1Y", required=True, key=key, bind="query-params",
                                label_visibility="collapsed")


def in_range(df: pd.DataFrame, col: str, choice: str) -> pd.DataFrame:
    months = RANGES.get(choice)
    if not months or df.empty:
        return df
    return df[df[col] >= df[col].max() - pd.DateOffset(months=months)]


def time_chart(df: pd.DataFrame, x: str, y: str, label: str, fmt: str = ",.2f", height: int = 300) -> None:
    """Single-series line + 10% area wash, y-axis fitted to the data, crosshair + tooltip on hover."""
    c = colors()
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=None, axis=alt.Axis(grid=False, labelOverlap=True)))
    lo, hi = float(df[y].min()), float(df[y].max())
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1.0
    # explicit domain: the area layer would otherwise pull 0 into the scale and flatten the line
    yenc = alt.Y(f"{y}:Q", title=None, scale=alt.Scale(domain=[lo - pad, hi + pad], nice=False),
                 axis=alt.Axis(tickCount=5))
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", clear="pointerout", empty=False)
    shown = alt.condition(hover, alt.value(1), alt.value(0))
    chart = alt.layer(
        base.mark_area(color=c["s1"], opacity=0.1, clip=True).encode(y=yenc),
        base.mark_line(color=c["s1"], strokeWidth=2, strokeJoin="round", strokeCap="round").encode(y=yenc),
        base.mark_rule(color=c["rule"], strokeWidth=1).encode(opacity=shown),
        base.mark_point(filled=True, size=80, color=c["s1"], stroke=c["surface"], strokeWidth=2)
            .encode(y=yenc, opacity=shown),
        base.mark_point(opacity=0, size=400).encode(  # invisible hit layer: nearest point drives hover + tooltip
            y=yenc, tooltip=[alt.Tooltip(f"{x}:T", title="Date", format="%d %b %Y"),
                             alt.Tooltip(f"{y}:Q", title=label, format=fmt)]).add_params(hover),
    ).properties(height=height)
    st.altair_chart(chart, width="stretch")


def bar_chart(df: pd.DataFrame, x: str, y: str, label: str, height: int = 110) -> None:
    """Thin single-series columns under a time chart (e.g. volume), with a tooltip per bar."""
    chart = alt.Chart(df).mark_bar(color=colors()["s1"], opacity=0.55, cornerRadiusTopLeft=1, cornerRadiusTopRight=1).encode(
        x=alt.X(f"{x}:T", title=None, axis=None),
        y=alt.Y(f"{y}:Q", title=None, axis=alt.Axis(format="~s", tickCount=2)),
        tooltip=[alt.Tooltip(f"{x}:T", title="Date", format="%d %b %Y"), alt.Tooltip(f"{y}:Q", title=label, format=",.0f")],
    ).properties(height=height)
    st.altair_chart(chart, width="stretch")


# number formats shared by every financials / valuation table
FIN_COLS = {
    "period_end": st.column_config.DateColumn("Period end"),
    "period_type": st.column_config.TextColumn("Period"),
    "source_file": st.column_config.TextColumn("Source file"),
    "currency": st.column_config.TextColumn("Ccy"),
    "revenue": st.column_config.NumberColumn("Revenue", format="compact"),
    "ebitda": st.column_config.NumberColumn("EBITDA", format="compact"),
    "ebit": st.column_config.NumberColumn("EBIT", format="compact"),
    "net_income": st.column_config.NumberColumn("Net income", format="compact"),
    "total_assets": st.column_config.NumberColumn("Total assets", format="compact"),
    "total_equity": st.column_config.NumberColumn("Total equity", format="compact"),
    "eps": st.column_config.NumberColumn("EPS", format="%.2f"),
    "roe": st.column_config.NumberColumn("ROE", format="%.1f%%"),
    "roa": st.column_config.NumberColumn("ROA", format="%.1f%%"),
    "l_e_ratio": st.column_config.NumberColumn("Liab./equity", format="%.1f"),
    "p_e": st.column_config.NumberColumn("P/E", format="%.1f"),
    "p_b": st.column_config.NumberColumn("P/B", format="%.2f"),
    "p_s": st.column_config.NumberColumn("P/S", format="%.2f"),
    "ev_s": st.column_config.NumberColumn("EV/S", format="%.2f"),
    "net_debt_ebitda": st.column_config.NumberColumn("Net debt/EBITDA", format="%.2f"),
    "dividend_per_share": st.column_config.NumberColumn("DPS", format="%.2f"),
    "dps": st.column_config.NumberColumn("DPS", format="%.2f"),
    "dividend_yield": st.column_config.NumberColumn("Div. yield", format="%.2f%%"),
    "div_yield": st.column_config.NumberColumn("Div. yield", format="%.2f%%"),
    "symbol": st.column_config.TextColumn("Symbol"),
    "name": st.column_config.TextColumn("Company"),
}


@st.cache_data(ttl=120, show_spinner=False)
def series_trends(url: str, table: str, key_col: str, value_col: str, days: int = 90) -> dict:
    """{key: [values of the last `days` days, oldest first]} for sparklines (identifiers are fixed, not user input)."""
    with get_engine(url).connect() as conn:
        last = conn.execute(text(f"SELECT MAX(date) FROM {table}")).scalar()
        if last is None:
            return {}
        cut = (pd.Timestamp(last) - pd.Timedelta(days=days)).date().isoformat()
        data = pd.read_sql(text(f"SELECT {key_col} AS k, {value_col} AS v FROM {table} WHERE date >= :cut ORDER BY date"),
                           conn, params={"cut": cut})
    return {k: [float(v) for v in g["v"].dropna()] for k, g in data.groupby("k")}


def price_trends(url: str) -> dict:
    return series_trends(url, "daily_prices", "stock_isin", "last_price")


MAX_CONTENT_WIDTH = 1600  # px; on wider screens the content stays centred and only the side margins grow
st.html(f"""<style>
  [data-testid="stMainBlockContainer"] {{ max-width: calc({MAX_CONTENT_WIDTH}px + 10rem); margin-inline: auto; }}
  .st-key-sql textarea {{ font-family: 'Source Code Pro', ui-monospace, monospace; font-size: .9rem; }}
</style>""")
st.title(":material/query_stats: RAG DB Explorer", anchor=False)
status = st.empty()  # subtitle + connection status, filled in once the Database tab has connected
tab_stocks, tab_idx, tab_fin, tab_tables, tab_schema, tab_sql, tab_db = url_tabs("tab", {
    "stocks": ":material/search: Stocks", "indexes": ":material/monitoring: Indexes",
    "financials": ":material/receipt_long: Financials", "tables": ":material/table_view: Tables",
    "schema": ":material/schema: Schema", "sql": ":material/terminal: SQL", "database": ":material/database: Database",
})
SUBTITLE = "Browse the source data behind RAG answers: stocks, indexes, financial reports and the raw tables."

# ------------------------------------------------------------------- database
# Shown as the last tab but filled first: every other tab needs the connection.
with tab_db:
    with st.container(border=True):
        st.subheader("Connection", anchor=False)
        # deliberately not URL-bound: it contains the password
        st.session_state["db_url"] = st.text_input("Database URL", DEFAULT_URL, type="password", key="db_url_input",
                                                   help="SQLAlchemy URL; defaults to `DATABASE_URL` from the environment / .env")
        try:
            insp = inspect(get_engine(st.session_state["db_url"]))
            tables = sorted(insp.get_table_names())
        except Exception as e:  # noqa: BLE001
            st.error(f"Cannot connect: {e}", icon=":material/error:")
            status.caption(f"{SUBTITLE}  \n:red-badge[:material/error: Not connected] · fix the URL in the **Database** tab")
            st.stop()
        try:
            u = make_url(st.session_state["db_url"])
            where_db = f"{u.database or ''}{' @ ' + u.host if u.host else ''}"
        except Exception:  # noqa: BLE001
            where_db = ""
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.markdown(f":green-badge[:material/check_circle: Connected · {len(tables)} tables]"
                    + (f" `{where_db}`" if where_db else ""))
        if c2.button("Clear cache & refresh", icon=":material/refresh:", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    st.subheader("Row counts", anchor=False)
    counts = []
    for t in tables:
        try:
            counts.append({"table": t, "rows": int(query(f'SELECT COUNT(*) AS n FROM "{t}"')["n"][0])})
        except Exception:  # noqa: BLE001
            pass
    if counts:
        cdf = pd.DataFrame(counts).sort_values("rows", ascending=False)
        st.dataframe(cdf, hide_index=True, width="stretch", column_config={
            "table": st.column_config.TextColumn("Table"),
            "rows": st.column_config.ProgressColumn("Rows", format="localized", min_value=0,
                                                    max_value=int(max(cdf["rows"].max(), 1)))})

status.caption(f"{SUBTITLE}  \n:green-badge[:material/check_circle: Connected · {len(tables)} tables]"
               + (f" `{where_db}`" if where_db else ""))

# ------------------------------------------------------------------- stocks
STOCK_COLS = {
    "isin": st.column_config.TextColumn("ISIN"),
    "mic": st.column_config.TextColumn("Exchange"),
    "symbol": st.column_config.TextColumn("Symbol"),
    "name": st.column_config.TextColumn("Company", width="medium"),
    "sector_name": st.column_config.TextColumn("Sector"),
    "last_price": st.column_config.NumberColumn("Price (EUR)", format="%.2f"),
    "change_pct": st.column_config.NumberColumn("Change", format="%+.2f%%"),
    "trend": st.column_config.LineChartColumn("90-day trend", width="small"),
    "quantity": st.column_config.NumberColumn("Shares", format="compact"),
    "first_trading_date": st.column_config.DateColumn("Listed since", format="MMM YYYY"),
}
PRICE_COLS = {
    "date": st.column_config.DateColumn("Date"),
    **{c: st.column_config.NumberColumn(c.replace("_price", "").capitalize(), format="%.2f")
       for c in ("open_price", "high_price", "low_price", "last_price")},
    "volume": st.column_config.NumberColumn("Volume", format="localized"),
    "turnover": st.column_config.NumberColumn("Turnover", format="compact"),
    "num_trades": st.column_config.NumberColumn("Trades", format="localized"),
}


def grouped_bars(df: pd.DataFrame, x: str, series: dict[str, str], height: int = 260) -> None:
    """Two-series column chart (categorical slots 1-2) with a legend on top and a tooltip per bar."""
    c = colors()
    labels = list(series.values())
    long = df.melt(id_vars=[x], value_vars=list(series), var_name="metric", value_name="value")
    long["metric"] = long["metric"].map(series)
    # bars capped at 24px; the chart is sized from the number of periods so groups don't spread out
    chart = alt.Chart(long).mark_bar(size=24, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X(f"{x}:N", title=None, sort=None, axis=alt.Axis(labelAngle=0, domain=False, ticks=False),
                scale=alt.Scale(paddingInner=0.5, paddingOuter=0.25)),
        xOffset=alt.XOffset("metric:N", sort=labels),
        y=alt.Y("value:Q", title=None, axis=alt.Axis(tickCount=4, labelExpr="replace(format(datum.value, '~s'), 'G', 'B')")),
        color=alt.Color("metric:N", sort=labels, scale=alt.Scale(domain=labels, range=[c["s1"], c["s2"]]),
                        legend=alt.Legend(orient="top", title=None, symbolType="square")),
        tooltip=[alt.Tooltip(f"{x}:N", title="Period"), alt.Tooltip("metric:N", title="Metric"),
                 alt.Tooltip("value:Q", title="Value", format=",.0f")],
    ).properties(height=height, width=max(360, 120 * df[x].nunique()))
    st.altair_chart(chart, width="content")


with tab_stocks:
    url = st.session_state["db_url"]
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 2, 3])
        mics = c1.multiselect("Exchange (MIC)", distinct(url, "SELECT DISTINCT mic FROM stocks ORDER BY 1"),
                              key="s_mic", bind="query-params", placeholder="All exchanges")
        sectors = c2.multiselect("Sector", distinct(url, "SELECT DISTINCT sector_name FROM stocks ORDER BY 1"),
                                 key="s_sector", bind="query-params", placeholder="All sectors")
        search = c3.text_input("Search name / symbol / ISIN / description", placeholder="e.g. hydro, bank, SAVB ...",
                               key="s_q", bind="query-params", icon=":material/search:")

        c4, c5, c6 = st.columns([2, 5, 3])
        idx_opts = {"(any)": None}
        for iso, sym in query("SELECT isin, symbol FROM indexes ORDER BY symbol").itertuples(index=False):
            idx_opts[sym] = iso
        idx_choice = c4.selectbox("Member of index", list(idx_opts), key="s_index", bind="query-params")
        # no format_func on bound segmented controls: the URL stores the displayed label
        quick = c5.segmented_control("Order by", ["Symbol", "Top gainers", "Top losers", "Market cap", "Price"],
                                     default="Symbol", required=True, key="s_sort", bind="query-params")
        price_max = float(query("SELECT COALESCE(MAX(last_price),0) AS m FROM stocks")["m"][0] or 0)
        price_rng = c6.slider("Last price range (EUR)", 0.0, max(price_max, 1.0), (0.0, max(price_max, 1.0)),
                              key="s_price", bind="query-params")

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
    order = {"Symbol": "symbol", "Top gainers": "change_prev_close_percentage DESC",
             "Top losers": "change_prev_close_percentage ASC", "Market cap": "(last_price*quantity) DESC",
             "Price": "last_price DESC"}[quick]
    df = query(
        f"""SELECT isin, mic, symbol, name, sector_name, last_price,
                   change_prev_close_percentage AS change_pct, quantity,
                   ROUND(last_price*quantity/1000000.0, 1) AS mcap_m_eur, first_trading_date
            FROM stocks WHERE {' AND '.join(where)} ORDER BY {order}""", params)
    trends = price_trends(url)
    df["trend"] = [trends.get(i, []) for i in df["isin"]]
    df["mcap"] = df["mcap_m_eur"] * 1e6

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Stocks shown", f"{len(df):,}", border=True)
    k2.metric("Advancing · declining", f"{int((df['change_pct'] > 0).sum())} · {int((df['change_pct'] < 0).sum())}",
              border=True)
    k3.metric("Median change", f"{df['change_pct'].median():+.2f}%" if len(df) else "–", border=True)
    k4.metric("Total market cap", compact(df["mcap"].sum(), "€") if len(df) else "–", border=True)

    st.caption("Click a row to see the company details.")
    # the selected stock is kept in the URL by ISIN (row positions shift when filters change); the table key
    # follows the rows shown, so a new result set starts from the URL selection instead of a stale row position
    url_isin = st.query_params.get("s_isin")
    url_rows = [i for i, v in enumerate(df["isin"]) if v == url_isin][:1]
    ev = st.dataframe(
        df.style.map(signed_color, subset=["change_pct"]), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row",
        column_order=["symbol", "name", "sector_name", "mic", "last_price", "change_pct", "trend", "mcap",
                      "first_trading_date", "isin"],
        column_config={**STOCK_COLS, "mcap": st.column_config.ProgressColumn(
            "Market cap (EUR)", format="compact", min_value=0, max_value=float(max(df["mcap"].max(), 1) if len(df) else 1))},
        key="s_table_" + hashlib.sha1("|".join(df["isin"]).encode()).hexdigest()[:12],
        selection_default={"selection": {"rows": url_rows}} if url_rows else None)

    if not ev.selection.rows:
        st.query_params.pop("s_isin", None)
        st.query_params.pop("s_view", None)
        st.session_state.pop("s_view", None)  # next selected stock opens on the first detail tab
    else:
        row = df.iloc[ev.selection.rows[0]]
        isin = row["isin"]
        st.query_params["s_isin"] = isin
        info = query("SELECT description, nace, website_url, logo_url, web_id FROM stocks WHERE isin=:i", {"i": isin}).iloc[0]
        px = query("SELECT date, open_price, high_price, low_price, last_price, volume, turnover, num_trades "
                   "FROM daily_prices WHERE stock_isin=:i ORDER BY date", {"i": isin})
        px["date"] = pd.to_datetime(px["date"])
        year = in_range(px, "date", "1Y")

        with st.container(border=True):
            st.subheader(row["name"], anchor=False)
            st.markdown(f":blue-badge[{row['symbol']}] :gray-badge[:material/account_balance: {row['mic']}] "
                        f":gray-badge[:material/category: {row['sector_name']}] :gray-badge[{isin}]")
            st.write(info["description"])
            links = [f"NACE {info['nace']}"] if info["nace"] else []
            if info["website_url"]:
                links.append(f"[:material/open_in_new: {info['website_url'].split('//')[-1]}]({info['website_url']})")
            if info["web_id"]:
                links.append(f"web_id `{info['web_id']}`")
            st.caption(" · ".join(links))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Last price", f"€{row['last_price']:,.2f}", f"{row['change_pct']:+.2f}%",
                      chart_data=px["last_price"].tail(60).tolist() or None, chart_type="area")
            m2.metric("Market cap", compact(row["mcap"], "€"))
            m3.metric("52-week range", f"{year['low_price'].min():,.2f} – {year['high_price'].max():,.2f}"
                      if not year.empty else "–")
            m4.metric("Avg daily turnover (30d)", compact(px["turnover"].tail(30).mean(), "€") if not px.empty else "–")

        d1, d2, d3, d4 = url_tabs("s_view", {"prices": "Price history", "financials": "Financials",
                                             "valuation": "Valuation", "indexes": "Index memberships"})
        with d1:
            if px.empty:
                st.info("No price data.", icon=":material/info:")
            else:
                shown = in_range(px, "date", range_picker("s_range"))
                time_chart(shown, "date", "last_price", "Close (EUR)")
                st.caption("Daily volume")
                bar_chart(shown, "date", "volume", "Volume")
                with st.expander("Daily prices", icon=":material/table:"):
                    st.dataframe(px.sort_values("date", ascending=False), hide_index=True, width="stretch",
                                 column_config=PRICE_COLS, placeholder="–")
        with d2:
            fin = query("""SELECT r.period_end, r.period_type, r.currency, f.revenue, f.ebitda, f.ebit, f.net_income,
                                  f.earnings_per_share AS eps, f.total_assets, f.total_equity, f.roe, f.roa, r.source_file
                           FROM reports r JOIN financials f ON f.report_id=r.report_id
                           WHERE r.stock_isin=:i ORDER BY r.period_end DESC""", {"i": isin})
            if fin.empty:
                st.info("No financial reports.", icon=":material/info:")
            else:
                yearly = fin[fin["period_type"] == "FY"]
                chart_df = (yearly if not yearly.empty else fin).iloc[::-1].copy()
                chart_df["period"] = chart_df["period_type"] + " " + pd.to_datetime(chart_df["period_end"]).dt.strftime(
                    "%Y" if not yearly.empty else "%b %Y")
                st.caption(f"Revenue and net income per {'fiscal year' if not yearly.empty else 'period'} "
                           f"({chart_df['currency'].iloc[-1]})")
                grouped_bars(chart_df, "period", {"revenue": "Revenue", "net_income": "Net income"})
                st.dataframe(fin, hide_index=True, width="stretch", column_config=FIN_COLS, placeholder="–")
        with d3:
            val = query("""SELECT r.period_end, r.period_type, v.p_e, v.p_b, v.p_s, v.ev_s, v.net_debt_ebitda,
                                  v.dividend_per_share, v.dividend_yield
                           FROM reports r JOIN valuation_ratios v ON v.report_id=r.report_id
                           WHERE r.stock_isin=:i ORDER BY r.period_end DESC""", {"i": isin})
            if val.empty:
                st.info("No valuation data.", icon=":material/info:")
            else:
                st.caption("Most recent value of each ratio (hover ⓘ for the report it comes from)")
                v1, v2, v3, v4 = st.columns(4)
                for col, (label, field, fmt) in zip((v1, v2, v3, v4), (
                        ("P/E", "p_e", "{:.1f}"), ("P/B", "p_b", "{:.2f}"),
                        ("Net debt / EBITDA", "net_debt_ebitda", "{:.2f}"), ("Dividend yield", "dividend_yield", "{:.2f}%"))):
                    known = val[val[field].notna()]
                    if known.empty:
                        col.metric(label, "–", border=True)
                    else:
                        r0 = known.iloc[0]
                        col.metric(label, fmt.format(r0[field]), border=True,
                                   help=f"{r0['period_type']} report, period ending {r0['period_end']}")
                st.dataframe(val, hide_index=True, width="stretch", column_config=FIN_COLS, placeholder="–")
        with d4:
            mem = query("""SELECT i.symbol AS index_symbol, i.name AS index_name, m.weight
                           FROM index_members m JOIN indexes i ON i.isin=m.index_isin WHERE m.stock_isin=:i""", {"i": isin})
            if mem.empty:
                st.info("Not a member of any index.", icon=":material/info:")
            else:
                st.dataframe(mem, hide_index=True, width="stretch", column_config={
                    "index_symbol": st.column_config.TextColumn("Index"),
                    "index_name": st.column_config.TextColumn("Name"),
                    "weight": st.column_config.ProgressColumn("Weight in index", format="%.2f%%", min_value=0, max_value=100)})

# ------------------------------------------------------------------ indexes
with tab_idx:
    idx_df = query("SELECT isin, mic, symbol, name, last_value, change_prev_close_percentage AS change_pct FROM indexes ORDER BY symbol")
    if idx_df.empty:
        st.info("No indexes in the database.", icon=":material/info:")
    else:
        idx_trends = series_trends(st.session_state["db_url"], "index_values", "index_isin", "last_value")
        for start in range(0, len(idx_df), 4):
            for col, r in zip(st.columns(4), idx_df.iloc[start:start + 4].itertuples(index=False)):
                col.metric(r.symbol, f"{r.last_value:,.2f}", f"{r.change_pct:+.2f}%", help=r.name, border=True,
                           chart_data=idx_trends.get(r.isin) or None, chart_type="area")
        st.caption("Last value and change vs previous close · sparkline: last 90 days")

        sel = st.segmented_control("Index", idx_df["symbol"].tolist(), default=idx_df["symbol"].iloc[0], required=True,
                                   key="i_index", bind="query-params")
        r = idx_df[idx_df["symbol"] == sel].iloc[0]
        iso = r["isin"]
        vals = query("SELECT date, open_value, high_value, low_value, last_value, change_prev_close_percentage AS change_pct, turnover "
                     "FROM index_values WHERE index_isin=:i ORDER BY date", {"i": iso})
        vals["date"] = pd.to_datetime(vals["date"])
        a, b = st.columns(2, gap="medium")
        with a:
            with st.container(border=True):
                st.subheader(f"{r['name']}", anchor=False)
                st.markdown(f":blue-badge[{r['symbol']}] :gray-badge[:material/account_balance: {r['mic']}] :gray-badge[{iso}]")
                if vals.empty:
                    st.info("No index values.", icon=":material/info:")
                else:
                    time_chart(in_range(vals, "date", range_picker("i_range")), "date", "last_value", r["symbol"], height=340)
        with b:
            with st.container(border=True):
                st.subheader("Members", anchor=False)
                st.dataframe(query("""SELECT s.symbol, s.name, m.weight, s.last_price, s.change_prev_close_percentage AS change_pct
                                      FROM index_members m JOIN stocks s ON s.isin=m.stock_isin
                                      WHERE m.index_isin=:i ORDER BY m.weight DESC""", {"i": iso})
                             .style.map(signed_color, subset=["change_pct"]),
                             hide_index=True, width="stretch", height=430,
                             column_order=["symbol", "name", "weight"], column_config={
                                 **STOCK_COLS, "name": st.column_config.TextColumn("Company", width="medium"),
                                 "weight": st.column_config.ProgressColumn("Weight", format="%.1f%%", min_value=0, max_value=100)})
        with st.expander("Raw index values", icon=":material/table:"):
            st.dataframe(vals.sort_values("date", ascending=False), hide_index=True, width="stretch", column_config={
                "date": st.column_config.DateColumn("Date"),
                **{c: st.column_config.NumberColumn(c.replace("_value", "").capitalize(), format="%.2f")
                   for c in ("open_value", "high_value", "low_value", "last_value")},
                "change_pct": st.column_config.NumberColumn("Change", format="%+.2f%%"),
                "turnover": st.column_config.NumberColumn("Turnover", format="compact")})

# ---------------------------------------------------------------- financials
with tab_fin:
    url = st.session_state["db_url"]
    with st.container(border=True):
        f1, f2, f3, f4 = st.columns(4)
        f_period = f1.multiselect("Period type", ["Q1", "Q2", "Q3", "Q4", "1H", "FY"], default=["FY"],
                                  key="f_period", bind="query-params", placeholder="All periods")
        f_src = f2.multiselect("Source country", distinct(url, "SELECT DISTINCT source_type FROM reports ORDER BY 1"),
                               key="f_country", bind="query-params", placeholder="All countries")
        year_sql = ("SELECT DISTINCT substr(period_end,1,4) FROM reports ORDER BY 1" if url.startswith("sqlite") else
                    "SELECT DISTINCT EXTRACT(YEAR FROM period_end) FROM reports ORDER BY 1")
        # plain ints (EXTRACT returns Decimal('2025')) so the URL reads ?f_year=2025
        f_year = f3.multiselect("Year", [int(float(y)) for y in distinct(url, year_sql)], key="f_year",
                                bind="query-params", placeholder="All years")
        f_q = f4.text_input("Company name / symbol", key="f_q", bind="query-params", icon=":material/search:")
    where, params = ["1=1"], {}
    if f_period:
        where.append(in_clause("r.period_type", f_period, "p", params))
    if f_src:
        where.append(in_clause("r.source_type", f_src, "c", params))
    if f_year:
        ycol = "CAST(substr(r.period_end,1,4) AS INTEGER)" if url.startswith("sqlite") else "EXTRACT(YEAR FROM r.period_end)"
        where.append(in_clause(ycol, f_year, "y", params))
    if f_q:
        params["q"] = f"%{f_q.lower()}%"
        where.append("(LOWER(s.name) LIKE :q OR LOWER(s.symbol) LIKE :q)")
    fdf = query(f"""SELECT s.symbol, s.name, r.period_end, r.period_type, r.currency, f.revenue, f.ebitda, f.ebit, f.net_income,
                           f.earnings_per_share AS eps, f.total_assets, f.total_equity, f.roe, f.roa,
                           f.liabilities_equity_ratio AS l_e_ratio, v.p_e, v.p_b, v.p_s, v.ev_s, v.net_debt_ebitda,
                           v.dividend_per_share AS dps, v.dividend_yield AS div_yield
                    FROM reports r
                    JOIN stocks s ON s.isin=r.stock_isin
                    JOIN financials f ON f.report_id=r.report_id
                    LEFT JOIN valuation_ratios v ON v.report_id=r.report_id
                    WHERE {' AND '.join(where)} ORDER BY r.period_end DESC, s.name""", params)
    h1, h2 = st.columns([4, 1], vertical_alignment="bottom")
    h1.caption(f"{len(fdf):,} reports · {fdf['symbol'].nunique() if len(fdf) else 0} companies")
    h2.download_button("Download CSV", fdf.to_csv(index=False), "financials.csv", "text/csv",
                       icon=":material/download:", width="stretch")
    st.dataframe(fdf, hide_index=True, width="stretch", height=560, column_config=FIN_COLS, placeholder="–")

# -------------------------------------------------------------- table browser
with tab_tables:
    with st.container(border=True):
        g0, g1, g2, g3, g4 = st.columns([2, 3, 2, 2, 1])
        t = g0.selectbox("Table", tables, key="t_table", bind="query-params",  # sort column belongs to the old table
                         on_change=lambda: st.session_state.update(t_sort="(none)"))
        cols = [c["name"] for c in inspect(get_engine(st.session_state["db_url"])).get_columns(t)]
        s_all = g1.text_input("Search across all columns", key="t_q", bind="query-params", icon=":material/search:")
        s_col = g2.selectbox("Sort by", ["(none)"] + cols, key="t_sort", bind="query-params")
        s_dir = g3.segmented_control("Direction", ["Asc", "Desc"], default="Asc", required=True, key="t_dir",
                                     bind="query-params")
        lim = g4.number_input("Limit", 10, 100000, 500, step=100, key="t_limit", bind="query-params")
    params = {}
    sql = f'SELECT * FROM "{t}"'
    if s_all:
        params["q"] = f"%{s_all.lower()}%"
        sql += " WHERE " + " OR ".join(f'LOWER(CAST("{c}" AS TEXT)) LIKE :q' for c in cols)
    if s_col != "(none)":
        sql += f' ORDER BY "{s_col}" {s_dir.upper()}'
    sql += f" LIMIT {int(lim)}"
    tdf = query(sql, params)
    h1, h2 = st.columns([4, 1], vertical_alignment="bottom")
    h1.caption(f"`{t}` · {len(tdf):,} rows shown")
    h2.download_button("Download CSV", tdf.to_csv(index=False), f"{t}.csv", "text/csv",
                       icon=":material/download:", width="stretch")
    st.dataframe(tdf, hide_index=True, width="stretch", height=560, placeholder="NULL")
    with st.expander("Columns", icon=":material/view_column:"):
        st.dataframe(pd.DataFrame(inspect(get_engine(st.session_state["db_url"])).get_columns(t))[["name", "type", "nullable"]]
                     .astype(str), hide_index=True, width="stretch")

# -------------------------------------------------------------------- schema
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_COL_STOP = r"\b(NOT\s+NULL|NULL|PRIMARY\s+KEY|REFERENCES|DEFAULT|CHECK|UNIQUE|CONSTRAINT|GENERATED|COLLATE)\b"


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _ident(s: str) -> str:
    return s.strip().strip('"').split(".")[-1].lower()


def _idents(s: str) -> list[str]:
    return [_ident(x) for x in s.split(",") if x.strip()]


def parse_schema_sql(sql: str) -> dict:
    """Small DDL parser: CREATE TABLE (+ ALTER TABLE ... ADD FOREIGN KEY) -> schema dict."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    schema: dict = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)\s*\(", sql, flags=re.I):
        name = _ident(m.group(1))
        i, depth = m.end(), 1
        while i < len(sql) and depth:
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        body = sql[m.end(): i - 1]
        cols, pk, fks, uniques = [], [], [], []
        for item in _split_top_level(body):
            cm = re.match(r"CONSTRAINT\s+\S+\s+(.*)", item, flags=re.I | re.S)
            if cm:
                item = cm.group(1)
            up = item.upper()
            if up.startswith("PRIMARY KEY"):
                pk += _idents(re.search(r"\((.*?)\)", item, flags=re.S).group(1))
            elif up.startswith("FOREIGN KEY"):
                fm = re.search(r"FOREIGN\s+KEY\s*\((.*?)\)\s*REFERENCES\s+([\w.\"]+)\s*(?:\((.*?)\))?", item, flags=re.I | re.S)
                if fm:
                    fks.append({"columns": _idents(fm.group(1)), "ref_table": _ident(fm.group(2)),
                                "ref_columns": _idents(fm.group(3) or "")})
            elif up.startswith("UNIQUE"):
                um = re.search(r"\((.*?)\)", item, flags=re.S)
                if um:
                    uniques.append(_idents(um.group(1)))
            elif up.startswith(("CHECK", "EXCLUDE")):
                continue
            else:
                cm2 = re.match(r'("?\w+"?)\s+(.*)', item, flags=re.S)
                if not cm2:
                    continue
                col, rest = _ident(cm2.group(1)), " ".join(cm2.group(2).split())
                stop = re.search(_COL_STOP, rest, flags=re.I)
                ctype = (rest[: stop.start()] if stop else rest).strip()
                is_pk = bool(re.search(r"PRIMARY\s+KEY", rest, flags=re.I))
                cols.append({"name": col, "type": ctype, "nullable": not (is_pk or re.search(r"NOT\s+NULL", rest, flags=re.I))})
                if is_pk:
                    pk.append(col)
                if re.search(r"\bUNIQUE\b", rest, flags=re.I):
                    uniques.append([col])
                rm = re.search(r"REFERENCES\s+([\w.\"]+)\s*(?:\((.*?)\))?", rest, flags=re.I)
                if rm:
                    fks.append({"columns": [col], "ref_table": _ident(rm.group(1)), "ref_columns": _idents(rm.group(2) or "")})
        schema[name] = {"columns": cols, "pk": pk, "fks": fks, "uniques": uniques}
    for m in re.finditer(r"ALTER\s+TABLE\s+(?:ONLY\s+)?([\w.\"]+)\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\((.*?)\)"
                         r"\s*REFERENCES\s+([\w.\"]+)\s*(?:\((.*?)\))?", sql, flags=re.I | re.S):
        t = _ident(m.group(1))
        if t in schema:
            schema[t]["fks"].append({"columns": _idents(m.group(2)), "ref_table": _ident(m.group(3)),
                                     "ref_columns": _idents(m.group(4) or "")})
    # "REFERENCES t" without a column list means t's primary key
    for info in schema.values():
        for fk in info["fks"]:
            if not fk["ref_columns"] and fk["ref_table"] in schema:
                fk["ref_columns"] = list(schema[fk["ref_table"]]["pk"])
    return schema


def read_schema_db(url: str) -> dict:
    insp = inspect(get_engine(url))  # fresh inspector -> no stale reflection cache
    schema = {}
    for t in sorted(insp.get_table_names()):
        schema[t] = {
            "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)}
                        for c in insp.get_columns(t)],
            "pk": insp.get_pk_constraint(t).get("constrained_columns") or [],
            "fks": [{"columns": fk["constrained_columns"], "ref_table": fk["referred_table"],
                     "ref_columns": fk["referred_columns"]} for fk in insp.get_foreign_keys(t)],
            "uniques": [u["column_names"] for u in insp.get_unique_constraints(t)],
        }
    return schema


def _font(text: str, color: str, size: int | None = None) -> str:
    """Graphviz rejects an empty <font></font> (the whole node label is dropped), so only wrap real text."""
    if not text:
        return ""
    size_attr = f' point-size="{size}"' if size else ""
    return f'<font color="{color}"{size_attr}>{text}</font>'


def schema_to_dot(schema: dict, tables: list[str], keys_only: bool, rankdir: str) -> tuple[str, list[dict]]:
    """Build the Graphviz DOT plus edge metadata (node / cell ids) the browser viewer uses to redraw links."""
    e = html.escape
    lines = [f'digraph schema {{ rankdir={rankdir}; bgcolor="transparent"; nodesep=0.5; ranksep=0.9;',
             '  node [shape=plain fontname="Helvetica" fontsize=11];',
             '  edge [color="#7a8599" arrowhead=crow arrowtail=tee dir=both];']
    referenced = {(fk["ref_table"], c) for t in tables for fk in schema[t]["fks"] for c in fk["ref_columns"]}
    missing = sorted({fk["ref_table"] for t in tables for fk in schema[t]["fks"] if fk["ref_table"] not in schema})
    node_id = {t: f"n{i}" for i, t in enumerate(list(tables) + missing)}
    cell_id: dict[tuple[str, str], str] = {}
    for t in tables:
        info = schema[t]
        fk_cols = {c for fk in info["fks"] for c in fk["columns"]}
        uq_cols = {c for u in info["uniques"] for c in u}
        rows = [f'<tr><td colspan="3" bgcolor="#2e5aac"><font color="white"><b>{e(t)}</b></font></td></tr>']
        for j, c in enumerate(info["columns"]):
            n = c["name"]
            if keys_only and not (n in info["pk"] or n in fk_cols or (t, n) in referenced):
                continue
            badge = " ".join(b for b, on in (("PK", n in info["pk"]), ("FK", n in fk_cols), ("UQ", n in uq_cols)) if on)
            label = f"<b>{e(n)}</b>" if n in info["pk"] else e(n)
            if not c["nullable"] and n not in info["pk"]:
                label += " *"
            bg = ' bgcolor="#fff4d6"' if n in info["pk"] else (' bgcolor="#e8f0ff"' if n in fk_cols else "")
            # graphviz only emits a cell id into the SVG when the cell also has an href (unwrapped again in JS)
            cid = cell_id[(t, n)] = f"c{node_id[t][1:]}_{j}"
            rows.append(f'<tr><td align="left" port="{e(n)}" id="{cid}" href="#"{bg}>{label}</td>'
                        f'<td align="left"{bg}>{_font(e(c["type"].lower()), "#666666")}</td>'
                        f'<td{bg}>{_font(badge, "#b34700", 9)}</td></tr>')
        if keys_only and len(rows) == 1:
            rows.append('<tr><td colspan="3"><font color="#999999">(no keys)</font></td></tr>')
        lines.append(f'  "{t}" [id="{node_id[t]}" label=<<table border="0" cellborder="1" cellspacing="0" '
                     f'cellpadding="4" bgcolor="white" color="#9aa4b5">{"".join(rows)}</table>>];')
    for rt in missing:
        lines.append(f'  "{rt}" [id="{node_id[rt]}" label=<<table border="0" cellborder="1" cellspacing="0" '
                     f'cellpadding="4" bgcolor="white" color="#cc0000"><tr><td bgcolor="#ffdddd"><b>{e(rt)}</b></td></tr>'
                     f'<tr><td><font color="#cc0000">not defined</font></td></tr></table>>];')
    edges = []
    for t in tables:
        for fk in schema[t]["fks"]:
            rt = fk["ref_table"]
            if rt not in node_id:  # defined but filtered out of the view
                continue
            src_cell = cell_id.get((t, fk["columns"][0])) if len(fk["columns"]) == 1 else None
            dst_cell = cell_id.get((rt, fk["ref_columns"][0])) if len(fk["ref_columns"]) == 1 else None
            src = f'"{t}":"{e(fk["columns"][0])}"' if src_cell else f'"{t}"'
            dst = f'"{rt}":"{e(fk["ref_columns"][0])}"' if dst_cell else f'"{rt}"'
            eid = f"e{len(edges)}"
            lines.append(f'  {dst} -> {src} [id="{eid}"];')
            # tail = referenced side ("one"), head = referencing side ("many")
            edges.append({"id": eid, "tail": node_id[rt], "tailCell": dst_cell, "head": node_id[t], "headCell": src_cell})
    lines.append("}")
    return "\n".join(lines), edges


# @hpcc-js/wasm-graphviz (Apache-2.0), vendored so the viewer works fully offline; the WASM binary is embedded.
GRAPHVIZ_JS = Path(__file__).with_name("vendor") / "hpcc-js-wasm-graphviz" / "graphviz.js"

_ZOOM_VIEWER = """
<div id="wrap" class="__THEME__">
  <div id="toolbar">
    <button id="zin" title="Zoom in">+</button>
    <button id="zout" title="Zoom out">&minus;</button>
    <button id="fit" title="Fit to view">Fit</button>
    <button id="one" title="Actual size">1:1</button>
    <button id="reset" title="Undo table moves and restore the automatic layout">Reset layout</button>
    <span id="pct"></span>
  </div>
  <div id="stage"><div id="msg">Rendering…</div></div>
</div>
<style>
  html, body { margin: 0; height: 100%; font-family: Helvetica, Arial, sans-serif; }
  #wrap { position: relative; height: __H__px; border: 1px solid rgba(255,255,255,.12); border-radius: 8px;
          overflow: hidden; box-sizing: border-box; background: #0f1115; }
  #stage { width: 100%; height: 100%; cursor: grab; touch-action: none; user-select: none; }
  #stage.panning { cursor: grabbing; }
  #stage svg { width: 100%; height: 100%; display: block; }
  #stage g.node { cursor: move; }
  #stage g.node.moving { filter: drop-shadow(0 6px 10px rgba(0,0,0,.7)); }
  #stage g.node:hover > polygon:first-of-type { stroke: #6ea8ff; }
  #links path { fill: none; stroke: #8b97ab; stroke-width: 1.2; }
  #links path.hot { stroke: #6ea8ff; stroke-width: 2; }
  #toolbar { position: absolute; top: 8px; right: 8px; z-index: 2; display: flex; gap: 4px; align-items: center; }
  #toolbar button { min-width: 32px; height: 28px; border: 1px solid #3a4150; border-radius: 6px; background: #1e222a;
                    color: #e6e6e6; cursor: pointer; font-size: 14px; padding: 0 8px; }
  #toolbar button:hover { background: #2a3140; }
  #pct { font-size: 12px; color: #fff; background: rgba(46,90,172,.9); padding: 4px 6px; border-radius: 6px; }
  #msg { padding: 16px; color: #aaa; }
  /* light app theme: light canvas + toolbar (the tables themselves are white in both) */
  #wrap.light { background: #f2f1ee; border-color: #e3e2de; }
  #wrap.light #toolbar button { background: #fcfcfb; color: #1b1b19; border-color: #d6d5d0; }
  #wrap.light #toolbar button:hover { background: #e9e8e4; }
  #wrap.light #msg { color: #52514e; }
</style>
<script type="module">
const DOT = __DOT__;
const EDGES = __EDGES__;
const STORE_KEY = "schema-layout:__KEY__";
const SVGNS = "http://www.w3.org/2000/svg";
const stage = document.getElementById("stage");
let Graphviz;
try {
  // the vendored module is inlined as text and imported from a blob: URL -> no network access needed
  const src = new Blob([__GVSRC__], { type: "text/javascript" });
  ({ Graphviz } = await import(URL.createObjectURL(src)));
} catch (err) {
  stage.innerHTML = '<div id="msg">Could not load the bundled Graphviz renderer: '
                    + err + '</div>';
  throw err;
}
const gv = await Graphviz.load();
stage.innerHTML = gv.dot(DOT);
const svg = stage.querySelector("svg");
svg.removeAttribute("width");
svg.removeAttribute("height");
svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
const graph = svg.querySelector("g.graph");
graph.querySelector(":scope > polygon")?.remove();            // graph background
svg.querySelectorAll("title").forEach((t) => t.remove());      // no hover tooltips
svg.querySelectorAll("a").forEach((a) => a.replaceWith(...a.childNodes));  // drop the dummy cell links

// ---- markers: crow's foot ("many") and bar ("one"), sized in diagram units
const defs = document.createElementNS(SVGNS, "defs");
defs.innerHTML = `
  <marker id="many" viewBox="0 -7 12 14" refX="12" refY="0" markerWidth="12" markerHeight="14"
          markerUnits="userSpaceOnUse" orient="auto">
    <path d="M0,0 L12,-6 M0,0 L12,0 M0,0 L12,6" fill="none" stroke="#8b97ab" stroke-width="1.2"/></marker>
  <marker id="one" viewBox="0 -7 12 14" refX="12" refY="0" markerWidth="12" markerHeight="14"
          markerUnits="userSpaceOnUse" orient="auto-start-reverse">
    <path d="M6,-6 L6,6" fill="none" stroke="#8b97ab" stroke-width="1.5"/></marker>`;
svg.prepend(defs);

// ---- nodes (tables) and their offsets from the automatic layout
const nodes = {};
graph.querySelectorAll(":scope > g.node").forEach((g) => {
  nodes[g.id] = { g, box: g.getBBox(), dx: 0, dy: 0 };
});
const layer = document.createElementNS(SVGNS, "g");
layer.id = "links";
graph.insertBefore(layer, graph.querySelector(":scope > g.node"));  // links render under the tables
graph.querySelectorAll(":scope > g.edge").forEach((g) => g.remove());

const links = EDGES.filter((ed) => nodes[ed.tail] && nodes[ed.head]).map((ed) => {
  const path = document.createElementNS(SVGNS, "path");
  path.setAttribute("marker-start", "url(#one)");
  path.setAttribute("marker-end", "url(#many)");
  layer.appendChild(path);
  const rowY = (nid, cid) => {
    const cell = cid && document.getElementById("a_" + cid);
    const b = cell ? cell.getBBox() : nodes[nid].box;
    return b.y + b.height / 2;
  };
  return { ...ed, path, tailY: rowY(ed.tail, ed.tailCell), headY: rowY(ed.head, ed.headCell) };
});

function drawLink(l) {
  const T = nodes[l.tail], H = nodes[l.head];
  const tL = T.box.x + T.dx, tR = tL + T.box.width, hL = H.box.x + H.dx, hR = hL + H.box.width;
  const y0 = l.tailY + T.dy, y3 = l.headY + H.dy;
  const GAP = 24;
  let x0, x3, c1, c2;
  if (tR + GAP <= hL) {            // tail left of head
    x0 = tR; x3 = hL;
    const k = Math.max(30, (x3 - x0) / 2); c1 = x0 + k; c2 = x3 - k;
  } else if (hR + GAP <= tL) {     // tail right of head
    x0 = tL; x3 = hR;
    const k = Math.max(30, (x0 - x3) / 2); c1 = x0 - k; c2 = x3 + k;
  } else {                         // overlapping horizontally: loop around the right-hand side
    x0 = tR; x3 = hR;
    const xm = Math.max(tR, hR) + 40; c1 = xm; c2 = xm;
  }
  l.path.setAttribute("d", `M${x0},${y0} C${c1},${y0} ${c2},${y3} ${x3},${y3}`);
}
function drawAll() { links.forEach(drawLink); }
function place(n) { n.g.setAttribute("transform", `translate(${n.dx} ${n.dy})`); }

function loadLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    for (const [id, [dx, dy]] of Object.entries(saved)) if (nodes[id]) Object.assign(nodes[id], { dx, dy });
  } catch (_) { /* storage unavailable: start from the automatic layout */ }
  Object.values(nodes).forEach(place);
}
function saveLayout() {
  const out = {};
  for (const [id, n] of Object.entries(nodes)) if (n.dx || n.dy) out[id] = [n.dx, n.dy];
  try { localStorage.setItem(STORE_KEY, JSON.stringify(out)); } catch (_) { /* ignore */ }
}

// ---- zoom / pan via the viewBox
const pct = document.getElementById("pct");
const base = svg.viewBox.baseVal;
const home = { x: base.x, y: base.y, w: base.width, h: base.height };
let vb = { ...home };
const MIN = 0.1, MAX = 20;   // zoom limits relative to the initial fit

function pxPerUnit() { return Math.min(stage.clientWidth / vb.w, stage.clientHeight / vb.h); }
function apply() {
  svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  pct.textContent = Math.round(pxPerUnit() * 100 * 0.75) + "%";  // graphviz units are pt (1pt = 1.333px)
}
function toSvg(clientX, clientY, el = svg) {
  const p = svg.createSVGPoint();
  p.x = clientX; p.y = clientY;
  return p.matrixTransform(el.getScreenCTM().inverse());
}
function zoomAt(factor, cx, cy) {
  const scale = home.w / (vb.w / factor);
  if (scale < MIN || scale > MAX) return;
  vb.x = cx - (cx - vb.x) / factor;
  vb.y = cy - (cy - vb.y) / factor;
  vb.w /= factor;
  vb.h /= factor;
  apply();
}
function zoomCenter(factor) { zoomAt(factor, vb.x + vb.w / 2, vb.y + vb.h / 2); }
function fit() {   // fit to the current content (tables may have been moved outside the original area)
  const b = graph.getBBox(), m = graph.transform.baseVal.consolidate()?.matrix ?? svg.createSVGMatrix();
  const pad = 12;
  vb = { x: b.x * m.a + m.e - pad, y: b.y * m.d + m.f - pad, w: b.width * m.a + 2 * pad, h: b.height * m.d + 2 * pad };
  apply();
}
function actualSize() {
  const cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2;
  vb.w = stage.clientWidth * 0.75; vb.h = stage.clientHeight * 0.75;
  vb.x = cx - vb.w / 2; vb.y = cy - vb.h / 2;
  apply();
}

stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  const p = toSvg(e.clientX, e.clientY);
  zoomAt(Math.exp(-e.deltaY * 0.0015), p.x, p.y);
}, { passive: false });

// ---- dragging: on a table moves the table, elsewhere pans the view
let drag = null;
stage.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const g = e.target.closest("g.node");
  stage.setPointerCapture(e.pointerId);
  if (g && nodes[g.id]) {
    const n = nodes[g.id];
    graph.appendChild(g);                        // bring to front
    g.classList.add("moving");
    links.forEach((l) => l.path.classList.toggle("hot", l.tail === g.id || l.head === g.id));
    drag = { node: n, last: toSvg(e.clientX, e.clientY, graph) };
  } else {
    drag = { x: e.clientX, y: e.clientY };
    stage.classList.add("panning");
  }
});
stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (drag.node) {
    const p = toSvg(e.clientX, e.clientY, graph);
    drag.node.dx += p.x - drag.last.x;
    drag.node.dy += p.y - drag.last.y;
    drag.last = p;
    place(drag.node);
    links.forEach((l) => { if (l.tail === drag.node.g.id || l.head === drag.node.g.id) drawLink(l); });
  } else {
    const k = pxPerUnit();
    vb.x -= (e.clientX - drag.x) / k;
    vb.y -= (e.clientY - drag.y) / k;
    drag = { x: e.clientX, y: e.clientY };
    apply();
  }
});
const endDrag = () => {
  if (drag?.node) {
    drag.node.g.classList.remove("moving");
    links.forEach((l) => l.path.classList.remove("hot"));
    saveLayout();
  }
  drag = null;
  stage.classList.remove("panning");
};
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("dblclick", (e) => { if (!e.target.closest("g.node")) fit(); });

document.getElementById("zin").onclick = () => zoomCenter(1.25);
document.getElementById("zout").onclick = () => zoomCenter(1 / 1.25);
document.getElementById("fit").onclick = fit;
document.getElementById("one").onclick = actualSize;
document.getElementById("reset").onclick = () => {
  Object.values(nodes).forEach((n) => { n.dx = 0; n.dy = 0; place(n); });
  try { localStorage.removeItem(STORE_KEY); } catch (_) { /* ignore */ }
  drawAll();
  vb = { ...home };
  apply();
};
window.addEventListener("resize", apply);

loadLayout();
drawAll();
apply();
</script>
"""


def _js_literal(obj) -> str:
    """JSON -> JS literal that is safe to embed inside a <script> element."""
    return json.dumps(obj).replace("</", "<\\/")


@st.cache_resource
def _graphviz_js_literal() -> str:
    """The vendored Graphviz module as a JS string literal (read once per server process)."""
    return _js_literal(GRAPHVIZ_JS.read_text(encoding="utf-8"))


def render_zoomable_dot(dot: str, edges: list[dict], height: int) -> None:
    """Render DOT in the browser with the bundled Graphviz WASM build: zoom, pan and draggable tables."""
    js = _js_literal

    values = {
        "H": str(height),
        "THEME": "dark" if st.context.theme.type == "dark" else "light",
        "KEY": hashlib.sha1(dot.encode("utf-8")).hexdigest()[:16],
        "EDGES": js(edges),
        "DOT": js(dot),
        "GVSRC": _graphviz_js_literal(),
    }
    # single pass, so placeholder-like text inside the DOT or the bundle is never substituted
    page = re.sub(r"__(H|THEME|KEY|EDGES|DOT|GVSRC)__", lambda m: values[m.group(1)], _ZOOM_VIEWER)
    st.iframe(page, height=height + 4)


with tab_schema:
    s1, s2, s3 = st.columns([2, 3, 1])
    src = s1.radio("Source", ["schema.sql file", "Live database"], horizontal=True, key="sch_src", bind="query-params")
    schema_path = s2.text_input("Schema file", str(SCHEMA_FILE), key="schema_path", disabled=src == "Live database")
    s3.write("")
    if s3.button("Refresh schema", icon=":material/refresh:", width="stretch"):
        st.session_state.pop("schema_data", None)

    cache_key = (src, schema_path if src != "Live database" else st.session_state["db_url"])
    if st.session_state.get("schema_data", (None,))[0] != cache_key:
        try:
            if src == "Live database":
                data, origin = read_schema_db(st.session_state["db_url"]), "live database"
            else:
                p = Path(schema_path)
                data = parse_schema_sql(p.read_text(encoding="utf-8"))
                origin = f"{p.name} (file modified {pd.Timestamp.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M:%S})"
            st.session_state["schema_data"] = (cache_key, data, origin, pd.Timestamp.now())
        except Exception as ex:  # noqa: BLE001
            st.session_state.pop("schema_data", None)
            st.error(f"Could not load schema: {ex}")

    if "schema_data" in st.session_state:
        _, schema, origin, loaded_at = st.session_state["schema_data"]
        n_fks = sum(len(v["fks"]) for v in schema.values())
        n_cols = sum(len(v["columns"]) for v in schema.values())
        st.caption(f"Loaded from **{origin}** at {loaded_at:%H:%M:%S} · {len(schema)} tables · "
                   f"{n_cols} columns · {n_fks} foreign keys")

        o1, o2, o3, o4 = st.columns([4, 1, 1, 2])
        all_tables = sorted(schema)
        shown = o1.multiselect("Tables", all_tables, default=all_tables, key="sch_tables", bind="query-params")
        keys_only = o2.toggle("Keys only", key="sch_keys", bind="query-params")
        rankdir = "LR" if o3.toggle("Horizontal", value=True, key="sch_lr", bind="query-params") else "TB"
        view_h = o4.slider("Viewer height (px)", 300, 1600, 700, step=50, key="sch_h", bind="query-params")

        if not shown:
            st.info("Select at least one table.")
        else:
            dot, edges = schema_to_dot(schema, shown, keys_only, rankdir)
            render_zoomable_dot(dot, edges, view_h)
            st.caption("Scroll to zoom · drag the background to pan · drag a table to move it "
                       "(positions are remembered in this browser) · double-click or **Fit** to fit · "
                       "**PK** primary key · **FK** foreign key · **UQ** unique · `*` NOT NULL · "
                       "edges go from the referenced column (one) to the referencing column (many) · "
                       "red tables are referenced but not defined")

            rels = [{"from_table": t, "from_columns": ", ".join(fk["columns"]), "to_table": fk["ref_table"],
                     "to_columns": ", ".join(fk["ref_columns"]), "target_defined": fk["ref_table"] in schema}
                    for t in shown for fk in schema[t]["fks"]]
            with st.expander(f"Relationships ({len(rels)})"):
                st.dataframe(pd.DataFrame(rels), hide_index=True, width="stretch")
            with st.expander("Table details"):
                for t in shown:
                    info = schema[t]
                    fk_map = {c: f"{fk['ref_table']}({', '.join(fk['ref_columns'])})"
                              for fk in info["fks"] for c in fk["columns"]}
                    st.markdown(f"**{t}**")
                    st.dataframe(pd.DataFrame([{"column": c["name"], "type": c["type"], "nullable": c["nullable"],
                                                "pk": c["name"] in info["pk"], "references": fk_map.get(c["name"], "")}
                                               for c in info["columns"]]), hide_index=True, width="stretch")
            with st.expander("Graphviz DOT source"):
                st.code(dot, language="dot")

# ----------------------------------------------------------------------- SQL
EXAMPLES = {
    "(pick an example)": "",
    "Top 10 stocks by market cap": "SELECT symbol, name, last_price, quantity, last_price*quantity AS mcap\nFROM stocks ORDER BY mcap DESC LIMIT 10;",
    "Highest ROE (FY 2025)": "SELECT s.symbol, s.name, f.roe\nFROM financials f JOIN reports r ON r.report_id=f.report_id\nJOIN stocks s ON s.isin=r.stock_isin\nWHERE r.period_type='FY' AND r.period_end='2025-12-31' ORDER BY f.roe DESC LIMIT 10;",
    "Average P/E per sector": "SELECT s.sector_name, ROUND(AVG(v.p_e),2) AS avg_pe\nFROM valuation_ratios v JOIN reports r ON r.report_id=v.report_id\nJOIN stocks s ON s.isin=r.stock_isin\nWHERE r.period_type='FY' GROUP BY s.sector_name ORDER BY avg_pe;",
    "Index weights": "SELECT i.symbol AS idx, s.symbol, m.weight FROM index_members m\nJOIN indexes i ON i.isin=m.index_isin JOIN stocks s ON s.isin=m.stock_isin\nORDER BY i.symbol, m.weight DESC;",
}
DEFAULT_SQL = "SELECT * FROM stocks LIMIT 20;"


def _load_example():
    # a bound widget can only be changed through session state (Streamlit logs a one-off harmless warning)
    st.session_state["sql"] = EXAMPLES[st.session_state["sql_ex"]] or DEFAULT_SQL


with tab_sql:
    with st.container(border=True):
        st.selectbox("Examples", list(EXAMPLES), key="sql_ex", on_change=_load_example, bind="query-params")
        sql_text = st.text_area("SQL", value=DEFAULT_SQL, height=180, key="sql", bind="query-params")
        b1, b2 = st.columns([1, 4], vertical_alignment="center")
        run = b1.button("Run", type="primary", icon=":material/play_arrow:", width="stretch")
        # deliberately not URL-bound: a link must never switch writes on
        allow_write = b2.toggle("Allow write statements (INSERT / UPDATE / DELETE / DDL)", value=False)
    if run:
        stmt = sql_text.strip().rstrip(";")
        is_read = stmt.lower().split(None, 1)[0] in ("select", "with", "explain", "show", "values") if stmt else False
        if not stmt:
            st.warning("Empty query.", icon=":material/warning:")
        elif not is_read and not allow_write:
            st.error("Only read queries are allowed. Switch on writes above to run this statement.", icon=":material/block:")
        else:
            try:
                if is_read:
                    st.session_state["sql_res"] = query(stmt)
                else:
                    with get_engine(st.session_state["db_url"]).begin() as conn:
                        r = conn.execute(text(stmt))
                    st.session_state["sql_res"] = None
                    st.success(f"Done. Rows affected: {r.rowcount}", icon=":material/check_circle:")
            except Exception as e:  # noqa: BLE001
                st.session_state["sql_res"] = None
                st.error(str(e), icon=":material/error:")
    res = st.session_state.get("sql_res")
    if res is not None:
        h1, h2 = st.columns([4, 1], vertical_alignment="bottom")
        h1.caption(f"{len(res):,} rows")
        h2.download_button("Download CSV", res.to_csv(index=False), "query_result.csv", "text/csv",
                           icon=":material/download:", width="stretch")
        st.dataframe(res, hide_index=True, width="stretch")
