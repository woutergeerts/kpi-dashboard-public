import io
import os
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
from queries_public import (
    load_kpis, load_ytd_growth,
    load_trends,
    load_lead_time, load_length_of_stay, load_group_size,
    load_channel,
    load_cancellation_stats,
    load_behaviour_annual,
    load_checkin_dow, load_checkout_dow,
    load_regional_annual, load_regional_monthly,
    count_properties, MIN_PROPERTIES,
    REGION_ORDER, COUNTRY_TO_REGION,
    COUNTRY_CURRENCY, load_fx_rate,
)

st.set_page_config(page_title="Mews Hotel Performance Benchmark", page_icon="🏨", layout="wide")

PINK       = "#E87DC2"
ORANGE     = "#FF6B00"
YELLOW     = "#D4E833"
WARM_GREY  = "#E8E6DF"
BLUE       = "#C8E5EE"
MAUVE      = "#F0E0EE"
RICH_BLACK = "#262626"
PALETTE    = [PINK, ORANGE, YELLOW, BLUE, MAUVE, WARM_GREY]
GLOBAL_COLOR = RICH_BLACK

TOO_FEW_MSG = f"⚠️ Fewer than {MIN_PROPERTIES} properties match these filters. Data suppressed to protect confidentiality."

# ── Hardcoded date range with 30-day lag ──────────────────────────────────────
# On the 1st of each month, pub_end advances automatically.
# Cache keys include pub_end so cached results refresh monthly.
_today    = date.today()
_lag_date = _today - timedelta(days=30)
PUB_END   = _lag_date.replace(day=1) - timedelta(days=1)  # last day of month ≥30 days ago
PUB_START = date(2025, 1, 1)

# ── Analyst commentary (edit analyst_insight.md to update) ───────────────────
_insight_path = os.path.join(os.path.dirname(__file__), "analyst_insight.md")
try:
    with open(_insight_path, "r") as _f:
        ANALYST_TEXT = _f.read()
except FileNotFoundError:
    ANALYST_TEXT = "*Analyst commentary not found. Add content to `analyst_insight.md`.*"

ANALYST_NAME  = "Wouter Geerts"
ANALYST_TITLE = "Senior Director, Research & Insights, Mews"
ANALYST_PHOTO = os.path.join(os.path.dirname(__file__), "analyst_photo.jpg")

# ── Session state defaults ────────────────────────────────────────────────────
if "pub_region" not in st.session_state:
    st.session_state.pub_region = None   # None = Global
if "pub_country" not in st.session_state:
    st.session_state.pub_country = None  # None = all countries in region

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://www.mews.com/hubfs/Mews-logo-2021-dark.svg", width=100) if False else None
    st.markdown("## Mews Benchmark")
    st.caption(
        f"**Data:** {PUB_START.strftime('%b %d, %Y')} → {PUB_END.strftime('%b %d, %Y')}"
    )
    st.caption("Updated monthly.")
    st.divider()

    # ── Region buttons ────────────────────────────────────────────────────────
    st.markdown("**Region**")
    region_btns = ["🌍 Global"] + REGION_ORDER
    cols_r = st.columns(2)
    for i, label in enumerate(region_btns):
        region_val = None if label == "🌍 Global" else label
        is_active = st.session_state.pub_region == region_val
        btn_type = "primary" if is_active else "secondary"
        if cols_r[i % 2].button(label, key=f"reg_{i}", type=btn_type, use_container_width=True):
            if is_active:
                st.session_state.pub_region = None
            else:
                st.session_state.pub_region = region_val
            st.session_state.pub_country = None
            st.rerun()

    # ── Country buttons (only when a non-Global region is selected) ───────────
    selected_region = st.session_state.pub_region
    selected_country = st.session_state.pub_country

    if selected_region is not None:
        st.divider()
        st.markdown(f"**Country** — {selected_region}")
        region_countries = sorted(
            c for c, r in COUNTRY_TO_REGION.items() if r == selected_region
        )
        cols_c = st.columns(2)
        for i, cname in enumerate(region_countries):
            is_active = selected_country == cname
            btn_type = "primary" if is_active else "secondary"
            if cols_c[i % 2].button(cname, key=f"ctry_{i}", type=btn_type, use_container_width=True):
                if is_active:
                    st.session_state.pub_country = None
                else:
                    st.session_state.pub_country = cname
                selected_country = st.session_state.pub_country
                st.rerun()

    st.divider()
    st.caption("Data is suppressed where fewer than 5 properties contribute to a metric.")

# ── Currency logic ────────────────────────────────────────────────────────────
curr_sym = "€"
fx_rate  = 1.0
local_db = False

if selected_country is not None:
    _cinfo = COUNTRY_CURRENCY.get(selected_country)
    if _cinfo and _cinfo[0] != "EUR":
        curr_sym = _cinfo[1]
        fx_rate  = 1.0
        local_db = True
    # EUR countries stay at defaults
elif selected_region == "North America":
    curr_sym = "$"
    fx_rate  = load_fx_rate("USD")
    local_db = False

def mc(v: float) -> str:
    return f"{curr_sym}{v:,.2f}"

# ── Filters dict ──────────────────────────────────────────────────────────────
filters = {
    "region":  [selected_region]  if selected_region  else [],
    "country": [selected_country] if selected_country else [],
    "segment": [],
    "start":   str(PUB_START),
    "end":     str(PUB_END),
    "local":   local_db,
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def _too_few(n: int) -> bool:
    return n < MIN_PROPERTIES

def excel_download_btn(df: pd.DataFrame, filename: str,
                       label: str = "⬇️ Download data (.xlsx)"):
    df = df.copy()
    for col in df.select_dtypes(include=["datetimetz"]).columns:
        df[col] = df[col].dt.tz_localize(None)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    st.download_button(label=label, data=buf.getvalue(), file_name=filename,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=False)

def render_annual_tiles(df_ann: pd.DataFrame, dim_col: str, selected_items: list,
                        years: list = [2024, 2025, 2026],
                        global_label: str = "🌍 **Global**", item_icon: str = "📍"):
    if df_ann.empty:
        st.info("No annual data available.")
        return
    show_items = ["Global"] + selected_items
    n_years = len(years)
    for metric, label, fmt in [
        ("occupancy", "Occupancy (%)",           lambda v: f"{v:.1f}%"),
        ("adr",       f"Avg ADR ({curr_sym})",    mc),
        ("revpar",    f"Avg RevPAR ({curr_sym})", mc),
    ]:
        st.markdown(f"**{label}**")
        hcols = st.columns([2] + [1] * n_years)
        hcols[0].markdown("**Dimension**")
        for ci, yr in enumerate(years, 1):
            yr_label = f"**{yr}**" if yr < date.today().year else f"**{yr} (YTD)**"
            hcols[ci].markdown(yr_label)
        for item in show_items:
            row_df = df_ann[df_ann[dim_col] == item]
            cols = st.columns([2] + [1] * n_years)
            cols[0].markdown(global_label if item == "Global" else f"{item_icon} {item}")
            for ci, yr in enumerate(years, 1):
                yr_row = row_df[row_df["year"] == yr]
                if yr_row.empty or pd.isna(yr_row[metric].iloc[0]):
                    cols[ci].metric(str(yr), "—")
                else:
                    val = float(yr_row[metric].iloc[0])
                    prev = row_df[row_df["year"] == yr - 1]
                    if not prev.empty and not pd.isna(prev[metric].iloc[0]):
                        pv = float(prev[metric].iloc[0])
                        delta = (f"{val-pv:+.1f}pp vs {yr-1}"
                                 if metric == "occupancy"
                                 else f"{(val-pv)/pv*100:+.1f}% vs {yr-1}")
                    else:
                        delta = None
                    cols[ci].metric(str(yr), fmt(val), delta)
        st.divider()

def render_monthly_lines(df_mon: pd.DataFrame, dim_col: str, selected_items: list,
                         color_map: dict):
    if df_mon.empty:
        st.info("No monthly trend data for selected filters.")
        return
    show_items = ["Global"] + selected_items
    df_plot = df_mon[df_mon[dim_col].isin(show_items)].copy()
    df_plot["month_label"] = df_plot["month"].dt.strftime("%b %Y")
    tick_months = (df_plot[df_plot[dim_col] == "Global"]
                   .sort_values("month")[["month", "month_label"]].drop_duplicates())
    col_r1, col_r2, col_r3 = st.columns(3)
    for col, metric, label in [
        (col_r1, "occupancy", "Occupancy (%)"),
        (col_r2, "adr",       f"ADR ({curr_sym})"),
        (col_r3, "revpar",    f"RevPAR ({curr_sym})"),
    ]:
        with col:
            fig = px.line(df_plot, x="month", y=metric, color=dim_col,
                          title=label,
                          labels={"month": "", metric: label,
                                  dim_col: dim_col.replace("_", " ").title()},
                          color_discrete_map=color_map)
            fig.update_xaxes(tickvals=tick_months["month"].tolist(),
                             ticktext=tick_months["month_label"].tolist(),
                             tickangle=-45)
            for trace in fig.data:
                if trace.name == "Global":
                    trace.line.width = 3
                    trace.line.dash  = "dot"
            fig.update_layout(legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)
    excel_download_btn(df_plot.drop(columns="month_label", errors="ignore"),
                       f"monthly_{dim_col}.xlsx")

st.title("🏨 Mews Hotel Performance Benchmark")
st.caption(
    f"Global hotel market data from Mews PMS. "
    f"Data period: {PUB_START.strftime('%B %d, %Y')} to {PUB_END.strftime('%B %d, %Y')}."
)

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "💡 Analyst Insights",
    "📊 Market KPIs",
    "🗺️ Regional Overview",
    "🔍 Booking Behaviour",
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Analyst Insights
# ═══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.subheader("Analyst Insight")
    with st.container(border=True):
        col_text, col_photo = st.columns([3, 1])
        with col_text:
            st.markdown(ANALYST_TEXT)
            st.markdown(f"**{ANALYST_NAME}**  \n*{ANALYST_TITLE}*")
        with col_photo:
            if ANALYST_PHOTO and os.path.exists(ANALYST_PHOTO):
                st.image(ANALYST_PHOTO, width=120)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Market KPIs
# ═══════════════════════════════════════════════════════════════════════════════
with tab2:
    with st.spinner("Checking filters…"):
        n_props = count_properties(filters)

    if _too_few(n_props):
        st.warning(TOO_FEW_MSG)
    else:
        st.subheader("Key Metrics")
        st.caption(f"Period: {PUB_START.strftime('%b %d, %Y')} – {PUB_END.strftime('%b %d, %Y')}")
        with st.spinner("Loading KPIs…"):
            kpis = load_kpis(filters)
        c1, c2, c3 = st.columns(3)
        c1.metric(f"ADR ({curr_sym})",     mc(kpis.get('adr', 0)))
        c2.metric("Occupancy",             f"{kpis.get('occupancy', 0):.1f}%")
        c3.metric(f"RevPAR ({curr_sym})",  mc(kpis.get('revpar', 0)))

        st.markdown(f"**YTD Growth — 2026 vs 2025 (Jan 1 – {PUB_END.strftime('%b %d')})**")
        with st.spinner("Loading YTD growth…"):
            ytd = load_ytd_growth(
                tuple(filters["region"]), tuple(filters["country"]), tuple(filters["segment"]),
                local=local_db)
        if ytd.get("too_few"):
            st.info(TOO_FEW_MSG)
        elif ytd:
            g1, g2, g3 = st.columns(3)
            g1.metric("Occupancy 2026 YTD", f"{ytd['occ_2026']:.1f}%",
                      f"{ytd['occ_chg']:+.1f}% vs 2025 ({ytd['occ_2025']:.1f}%)")
            g2.metric(f"ADR 2026 YTD", mc(ytd['adr_2026']),
                      f"{ytd['adr_chg']:+.1f}% vs 2025 ({mc(ytd['adr_2025'])})")
            g3.metric(f"RevPAR 2026 YTD", mc(ytd['revpar_2026']),
                      f"{ytd['revpar_chg']:+.1f}% vs 2025 ({mc(ytd['revpar_2025'])})")
        else:
            st.info("YTD growth data not available.")

        st.divider()
        st.subheader("Historical Performance (7-day rolling average)")
        st.caption(f"Full date range: {PUB_START.strftime('%b %Y')} – {PUB_END.strftime('%b %Y')}")
        with st.spinner("Loading trends…"):
            df_trends = load_trends(filters)
        if not df_trends.empty:
            tick_df = df_trends.drop_duplicates("month_label").sort_values("day_of_year")
            years = sorted(df_trends["year"].unique())
            year_colors = dict(zip(years, PALETTE))
            col_t1, col_t2, col_t3 = st.columns(3)
            for col, metric, label in [
                (col_t1, "occupancy", "Occupancy (%)"),
                (col_t2, "adr",       f"ADR ({curr_sym})"),
                (col_t3, "revpar",    f"RevPAR ({curr_sym})"),
            ]:
                with col:
                    fig = px.line(df_trends, x="day_of_year", y=metric, color="year",
                                  title=label,
                                  labels={"day_of_year": "", metric: label, "year": "Year"},
                                  color_discrete_map=year_colors)
                    fig.update_xaxes(tickvals=tick_df["day_of_year"].tolist(),
                                     ticktext=tick_df["month_label"].tolist())
                    st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(
                df_trends[["date", "year", "occupancy", "adr", "revpar"]],
                "historical_trends.xlsx")
        else:
            st.info("No trend data for selected filters.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Regional Overview
# Always shows all 5 regions + Global regardless of sidebar selection.
# ═══════════════════════════════════════════════════════════════════════════════
with tab3:
    with st.spinner("Loading regional data…"):
        df_reg_ann = load_regional_annual((), (), (), local=False, fx_rate=1.0)
        df_reg_mon = load_regional_monthly(
            (), str(PUB_START), str(PUB_END), (), (), local=False, fx_rate=1.0)

    all_regions = [r for r in REGION_ORDER if r in df_reg_ann["region"].unique()]
    region_color_map = {r: PALETTE[i % len(PALETTE)] for i, r in enumerate(all_regions)}
    region_color_map["Global"] = GLOBAL_COLOR

    st.subheader("Annual Averages by Region")
    st.caption(
        f"Full-year averages for 2024–{date.today().year} "
        f"({date.today().year} up to {PUB_END.strftime('%b %d')}). "
        "All regions shown — independent of sidebar selection."
    )
    render_annual_tiles(df_reg_ann, "region", all_regions,
                        years=[2024, 2025, 2026],
                        global_label="🌍 **Global**", item_icon="🗺️")

    st.subheader("Monthly Trends by Region")
    st.caption(f"Jan 2025 – {PUB_END.strftime('%b %Y')}. All regions compared to global average.")
    render_monthly_lines(df_reg_mon, "region", all_regions, region_color_map)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Booking Behaviour
# ═══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.info("ℹ️ Booking behaviour data covers full calendar years 2024 and 2025 only.")

    with st.spinner("Loading annual averages…"):
        beh = load_behaviour_annual(
            tuple(filters["region"]), tuple(filters["country"]), tuple(filters["segment"]))
    avgs_df      = beh.get("averages", pd.DataFrame())
    channel_pcts = beh.get("channel_pcts", {})

    BEHAVIOUR_YEARS = [2024, 2025]

    def annual_behaviour_tiles(metric_key: str, fmt):
        if avgs_df.empty:
            return
        cols = st.columns(2)
        for ci, yr in enumerate(BEHAVIOUR_YEARS):
            yr_row = avgs_df[avgs_df["year"] == yr]
            if yr_row.empty or pd.isna(yr_row[metric_key].iloc[0]):
                cols[ci].metric(f"{yr}", "—")
            else:
                val = float(yr_row[metric_key].iloc[0])
                prev = avgs_df[avgs_df["year"] == yr - 1]
                if not prev.empty and not pd.isna(prev[metric_key].iloc[0]):
                    pv = float(prev[metric_key].iloc[0])
                    delta = f"{val-pv:+.2f} vs {yr-1}"
                else:
                    delta = None
                cols[ci].metric(f"{yr}", fmt(val), delta)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Reservations by Length of Stay")
        st.caption("**Annual avg LOS (nights)**")
        annual_behaviour_tiles("avg_los", lambda v: f"{v:.1f} nights")
        with st.spinner("Loading…"):
            df_los = load_length_of_stay(filters)
        if not df_los.empty:
            df_los["pct"] = df_los["reservations"] / df_los["reservations"].sum() * 100
            fig = px.bar(df_los, x="los_bucket", y="pct",
                         labels={"los_bucket": "Nights", "pct": "% of Reservations"},
                         color_discrete_sequence=[ORANGE],
                         text=df_los["pct"].apply(lambda x: f"{x:.1f}%"))
            fig.update_traces(textposition="outside")
            fig.update_xaxes(categoryorder="array",
                             categoryarray=["1 night","2 nights","3 nights",
                                            "4-7 nights","8-14 nights","15+ nights"])
            fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_los, "length_of_stay.xlsx")
        else:
            st.info("No data.")

    with col_b:
        st.subheader("Reservations by Group Size")
        st.caption("**Annual avg group size (guests)**")
        annual_behaviour_tiles("avg_group_size", lambda v: f"{v:.1f} guests")
        with st.spinner("Loading…"):
            df_gs = load_group_size(filters)
        if not df_gs.empty:
            df_gs["pct"] = df_gs["reservations"] / df_gs["reservations"].sum() * 100
            fig = px.bar(df_gs, x="group_size_bucket", y="pct",
                         labels={"group_size_bucket": "Guests per reservation",
                                 "pct": "% of Reservations"},
                         color_discrete_sequence=[YELLOW],
                         text=df_gs["pct"].apply(lambda x: f"{x:.1f}%"))
            fig.update_traces(textposition="outside")
            fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_gs, "group_size.xlsx")
        else:
            st.info("No data.")

    st.divider()
    col_c, col_d = st.columns(2)
    with col_c:
        st.subheader("Reservations by Lead Time")
        st.caption("**Annual avg lead time (days)**")
        annual_behaviour_tiles("avg_lead_time", lambda v: f"{v:.0f} days")
        with st.spinner("Loading…"):
            df_lt = load_lead_time(filters)
        if not df_lt.empty:
            df_lt["pct"] = df_lt["reservations"] / df_lt["reservations"].sum() * 100
            fig = px.bar(df_lt, x="lead_time_bucket", y="pct",
                         labels={"lead_time_bucket": "Days before check-in",
                                 "pct": "% of Reservations"},
                         color_discrete_sequence=[PINK],
                         text=df_lt["pct"].apply(lambda x: f"{x:.1f}%"))
            fig.update_traces(textposition="outside")
            fig.update_xaxes(categoryorder="array",
                             categoryarray=["0 - Same day","1-3 days","4-7 days","8-14 days",
                                            "15-30 days","31-60 days","61-90 days","90+ days"])
            fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_lt, "lead_time.xlsx")
        else:
            st.info("No data.")

    with col_d:
        st.subheader("Reservations by Channel")
        if channel_pcts:
            st.caption("**Annual channel split (%)**")
            ch_labels = ["Third-Party","Online Direct","Offline Direct"]
            header = st.columns([2, 1, 1])
            header[0].markdown("**Channel**")
            for ci, yr in enumerate(BEHAVIOUR_YEARS, 1):
                header[ci].markdown(f"**{yr}**")
            for ch in ch_labels:
                row_cols = st.columns([2, 1, 1])
                row_cols[0].markdown(ch)
                for ci, yr in enumerate(BEHAVIOUR_YEARS, 1):
                    val = channel_pcts.get(yr, {}).get(ch)
                    row_cols[ci].metric("", f"{val:.1f}%" if val is not None else "—")
        with st.spinner("Loading…"):
            df_ch = load_channel(filters)
        if not df_ch.empty:
            fig = px.pie(df_ch, names="channel", values="reservations",
                         color_discrete_sequence=PALETTE)
            fig.update_traces(textposition="inside", textinfo="percent+label")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_ch, "channel.xlsx")
        else:
            st.info("No data.")

    st.divider()
    st.subheader("Arrivals & Departures by Day of Week")
    col_dow1, col_dow2 = st.columns(2)
    with col_dow1:
        st.markdown("**Arrivals by Day of Week**")
        with st.spinner("Loading…"):
            df_ci_dow = load_checkin_dow(filters)
        if not df_ci_dow.empty:
            fig = px.bar(df_ci_dow, x="day_of_week", y="pct",
                         labels={"day_of_week": "", "pct": "% of Arrivals"},
                         color_discrete_sequence=[PINK],
                         text=df_ci_dow["pct"].apply(lambda x: f"{x:.1f}%"))
            fig.update_traces(textposition="outside")
            fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_ci_dow, "arrivals_dow.xlsx")
        else:
            st.info("No data.")
    with col_dow2:
        st.markdown("**Departures by Day of Week**")
        with st.spinner("Loading…"):
            df_co_dow = load_checkout_dow(filters)
        if not df_co_dow.empty:
            fig = px.bar(df_co_dow, x="day_of_week", y="pct",
                         labels={"day_of_week": "", "pct": "% of Departures"},
                         color_discrete_sequence=[BLUE],
                         text=df_co_dow["pct"].apply(lambda x: f"{x:.1f}%"))
            fig.update_traces(textposition="outside")
            fig.update_yaxes(ticksuffix="%")
            st.plotly_chart(fig, use_container_width=True)
            excel_download_btn(df_co_dow, "departures_dow.xlsx")
        else:
            st.info("No data.")

    st.divider()
    st.subheader("Cancellation Insights")
    st.caption("Annual figures — full calendar years 2024 and 2025.")
    with st.spinner("Loading cancellation data…"):
        df_canc = load_cancellation_stats(filters)

    if not df_canc.empty:
        st.markdown("**Cancellation Rate (% of total bookings)**")
        canc_cols = st.columns(len(df_canc))
        for i, row in df_canc.iterrows():
            yr  = int(row["year"])
            val = float(row["cancel_rate"])
            prev = df_canc[df_canc["year"] == yr - 1]
            delta = None
            if not prev.empty:
                pv = float(prev["cancel_rate"].iloc[0])
                delta = f"{val-pv:+.1f}pp vs {yr-1}"
            canc_cols[i].metric(str(yr), f"{val:.1f}%", delta)

        st.markdown("**Avg Cancellation Window (days before arrival)**")
        win_cols = st.columns(len(df_canc))
        for i, row in df_canc.iterrows():
            yr  = int(row["year"])
            val = row["avg_cancel_window"]
            if pd.isna(val):
                win_cols[i].metric(str(yr), "—")
            else:
                val = float(val)
                prev = df_canc[df_canc["year"] == yr - 1]
                delta = None
                if not prev.empty and not pd.isna(prev["avg_cancel_window"].iloc[0]):
                    pv = float(prev["avg_cancel_window"].iloc[0])
                    delta = f"{val-pv:+.1f} days vs {yr-1}"
                win_cols[i].metric(str(yr), f"{val:.0f} days", delta)

        excel_download_btn(
            df_canc[["year","total_bookings","cancellations","cancel_rate","avg_cancel_window"]],
            "cancellations.xlsx")
    else:
        st.info("No cancellation data for selected filters.")

# ── Footer disclaimer ─────────────────────────────────────────────────────────
st.divider()
st.markdown(
    f"""<div style="font-size:0.75rem; color:#888; padding-top:0.5rem;">
    <strong>About this report:</strong> This benchmark report is published by Mews and based on
    aggregated, anonymised data from Mews PMS customers. Individual property data is never disclosed.
    Metrics are suppressed where fewer than {MIN_PROPERTIES} properties contribute to a data point.<br><br>
    <strong>Data scope:</strong> Active Mews customers as of each period start date that joined at
    least one day before the start date and have been live on Mews for at least 90 days.
    Data covers {PUB_START.strftime('%B %d, %Y')} to {PUB_END.strftime('%B %d, %Y')}.
    Updated on the first of each month with a 30-day data lag.<br><br>
    <strong>Methodology:</strong> Occupancy, ADR and RevPAR are room-weighted market aggregates.
    Occupancy = total occupied rooms ÷ total available rooms. ADR = total room revenue ÷ total
    occupied rooms. RevPAR = total room revenue ÷ total available rooms. Days on which a property
    has available rooms but zero occupied rooms are excluded to avoid seasonal distortion.<br><br>
    <strong>Filters applied:</strong>
    Region: {selected_region or 'Global (all)'} |
    Country: {selected_country or 'All'}<br><br>
    <strong>Last updated:</strong> {date.today().strftime("%B %d, %Y")}
    </div>""",
    unsafe_allow_html=True,
)
