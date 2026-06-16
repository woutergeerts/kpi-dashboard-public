#!/usr/bin/env python3
"""
Monthly data refresh — Mews Public Benchmark Dashboard.

Queries Databricks and writes pre-computed JSON files to public/data/.
Run by GitHub Actions on the 1st of each month (or manually via workflow_dispatch).

Output files:
  public/data/meta.json       — date range, FX rates, generated timestamp
  public/data/kpis.json       — ADR / Occupancy / RevPAR + YTD growth, by entity
  public/data/trends.json     — daily 7-day rolling averages, global + per region
  public/data/regional.json   — annual tiles + monthly lines for all 5 regions
  public/data/behaviour.json  — lead time, LOS, cancellations, DOW (2024 & 2025)
"""

import os
import json
from datetime import date, timedelta

import pandas as pd
from databricks import sql
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DATABRICKS_HOST      = os.getenv("DATABRICKS_HOST")
DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
DATABRICKS_TOKEN     = os.getenv("DATABRICKS_TOKEN")
MIN_PROPERTIES       = 5
OUTPUT_DIR           = os.path.join(os.path.dirname(__file__), "..", "public", "data")

# ── Date range (30-day lag) ───────────────────────────────────────────────────

_today    = date.today()
_lag      = _today - timedelta(days=30)
PUB_END   = _lag.replace(day=1) - timedelta(days=1)   # last day of month ≥30d ago
PUB_START = date(2025, 1, 1)

print(f"Date range: {PUB_START} → {PUB_END}  (generated {_today})")

# ── Region / currency mappings ────────────────────────────────────────────────

COUNTRY_TO_REGION = {
    "United States": "North America", "Canada": "North America",
    "Germany": "Europe", "Switzerland": "Europe", "Austria": "Europe",
    "France": "Europe", "Netherlands": "Europe", "Belgium": "Europe",
    "Luxembourg": "Europe", "Sweden": "Europe", "Norway": "Europe",
    "Finland": "Europe", "Denmark": "Europe", "Iceland": "Europe",
    "Faroe Islands": "Europe", "Svalbard and Jan Mayen": "Europe",
    "United Kingdom": "Europe", "Ireland": "Europe", "Jersey": "Europe",
    "Spain": "Europe", "Andorra": "Europe", "Portugal": "Europe",
    "Czech Republic": "Europe", "Greece": "Europe", "Estonia": "Europe",
    "Hungary": "Europe", "Slovakia": "Europe", "Poland": "Europe",
    "Malta": "Europe", "Cyprus": "Europe", "Latvia": "Europe",
    "Ukraine": "Europe", "Russian Federation": "Europe", "Italy": "Europe",
    "Australia": "APAC", "New Zealand": "APAC", "Japan": "APAC",
    "Thailand": "APAC", "Indonesia": "APAC", "Philippines": "APAC",
    "Singapore": "APAC", "Malaysia": "APAC", "Hong Kong": "APAC",
    "Cambodia": "APAC", "Chinese Taipei": "APAC", "Fiji": "APAC",
    "French Polynesia": "APAC", "Korea, Republic of": "APAC",
    "Samoa": "APAC", "Tonga": "APAC", "Vanuatu": "APAC",
    "Mexico": "South America", "Bonaire, Sint Eustatius and Saba": "South America",
    "Colombia": "South America", "Costa Rica": "South America",
    "Curacao": "South America", "Curaçao": "South America",
    "Panama": "South America", "Peru": "South America",
    "Guatemala": "South America", "Brazil": "South America",
    "Argentina": "South America", "Ecuador": "South America",
    "Guadeloupe": "South America", "Chile": "South America",
    "Dominican Republic": "South America", "Aruba": "South America",
    "Bahamas": "South America", "Martinique": "South America",
    "Bolivia, Plurinational State of": "South America",
    "Honduras": "South America", "Paraguay": "South America",
    "Saint Barthelemy": "South America", "Saint Barthélemy": "South America",
    "Saint Kitts and Nevis": "South America",
    "Saint Martin (French part)": "South America", "Uruguay": "South America",
    "South Africa": "MEA", "Morocco": "MEA", "Reunion": "MEA", "Réunion": "MEA",
    "Georgia": "MEA", "Mauritius": "MEA", "Egypt": "MEA",
    "Namibia": "MEA", "Congo, the Democratic Republic of the": "MEA",
    "Israel": "MEA", "Kenya": "MEA",
    "Cote d'Ivoire": "MEA", "Côte d'Ivoire": "MEA",
    "Ghana": "MEA", "Nigeria": "MEA", "Turkey": "MEA",
}

REGION_ORDER = ["North America", "South America", "Europe", "APAC", "MEA"]

COUNTRY_CURRENCY = {
    "United States": ("USD", "$"), "Canada": ("CAD", "CA$"),
    "United Kingdom": ("GBP", "£"), "Jersey": ("GBP", "£"),
    "Switzerland": ("CHF", "CHF "), "Sweden": ("SEK", "kr "),
    "Norway": ("NOK", "kr "), "Denmark": ("DKK", "kr "),
    "Iceland": ("ISK", "kr "), "Faroe Islands": ("DKK", "kr "),
    "Czech Republic": ("CZK", "Kč "), "Hungary": ("HUF", "Ft "),
    "Poland": ("PLN", "zł "), "Ukraine": ("UAH", "₴"),
    "Russian Federation": ("RUB", "₽"),
    "Australia": ("AUD", "A$"), "New Zealand": ("NZD", "NZ$"),
    "Japan": ("JPY", "¥"), "Thailand": ("THB", "฿"),
    "Indonesia": ("IDR", "Rp "), "Philippines": ("PHP", "₱"),
    "Singapore": ("SGD", "S$"), "Malaysia": ("MYR", "RM "),
    "Hong Kong": ("HKD", "HK$"), "Cambodia": ("KHR", "KHR "),
    "Korea, Republic of": ("KRW", "₩"), "Chinese Taipei": ("TWD", "NT$"),
    "South Africa": ("ZAR", "R "), "Turkey": ("TRY", "₺"),
    "Israel": ("ILS", "₪"), "Morocco": ("MAD", "MAD "),
    "Georgia": ("GEL", "₾"), "Egypt": ("EGP", "E£"),
    "Kenya": ("KES", "KSh "), "Nigeria": ("NGN", "₦"),
    "Brazil": ("BRL", "R$"), "Mexico": ("MXN", "MX$"),
    "Colombia": ("COP", "COP "), "Costa Rica": ("CRC", "₡"),
    "Peru": ("PEN", "S/ "), "Chile": ("CLP", "CLP "),
    "Argentina": ("ARS", "AR$"), "Dominican Republic": ("DOP", "RD$"),
}

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return sql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_TOKEN,
    )

def query(sql_str: str, conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql_str)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(rows, columns=cols)
    return df

# ── SQL building blocks ───────────────────────────────────────────────────────

def _build_region_case() -> str:
    lines = []
    for country, region in COUNTRY_TO_REGION.items():
        safe = country.replace("'", "''")
        lines.append(f"        WHEN '{safe}' THEN '{region}'")
    return "CASE p.country_name\n" + "\n".join(lines) + "\n        ELSE 'Other'\n    END"

REGION_CASE = _build_region_case()

# Numerators/denominators stored separately so we can re-aggregate correctly
# (ADR = SUM(revenue) / SUM(occupied), not AVG of per-property ADRs)
ROOM_NUMS = """\
    SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
             THEN m.total_adjusted_net_accommodation_revenue_eur END)    AS rev_eur,
    SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
             THEN m.total_adjusted_net_accommodation_revenue END)        AS rev_local,
    SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
             THEN m.num_directly_occupied_accommodation_resources END)   AS occ,
    SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
             THEN m.num_available_accommodation_resources END)           AS avail"""

def _prop_base(start: str) -> str:
    return (
        f"p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        f"AND CAST(p.pms_property_created_at AS DATE) < '{start}' "
        f"AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE) <= DATE_SUB('{start}', 90))"
    )

def _metrics(rev, occ, avail) -> dict:
    rev, occ, avail = float(rev or 0), float(occ or 0), float(avail or 0)
    return {
        "adr":       round(rev / occ * 1.0,        2) if occ  > 0 else 0,
        "revpar":    round(rev / avail * 1.0,       2) if avail > 0 else 0,
        "occupancy": round(occ / avail * 100.0,     1) if avail > 0 else 0,
    }

def _num(df: pd.DataFrame, col: str):
    return pd.to_numeric(df[col], errors="coerce").fillna(0)

# ── KPIs (global, by region, by country) ─────────────────────────────────────

def fetch_kpis(conn) -> dict:
    print("  Fetching KPIs...")
    pf = _prop_base(str(PUB_START))
    df = query(f"""
        SELECT {REGION_CASE} AS region,
               p.country_name,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_NUMS}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.calendar_date_local >= '{PUB_START}'
          AND m.calendar_date_local <  '{PUB_END}'
          AND ({REGION_CASE}) != 'Other'
        GROUP BY region, p.country_name
    """, conn)

    for c in ["rev_eur", "rev_local", "occ", "avail", "property_count"]:
        df[c] = _num(df, c)

    out = {"global": {}, "regions": {}, "countries": {}}

    # Global
    if not df.empty:
        out["global"] = {
            **_metrics(df["rev_eur"].sum(), df["occ"].sum(), df["avail"].sum()),
            "adr_eur": _metrics(df["rev_eur"].sum(), df["occ"].sum(), df["avail"].sum())["adr"],
            "property_count": int(df["property_count"].sum()),
            "currency": "EUR", "currency_symbol": "€",
        }

    # By region
    for region in REGION_ORDER:
        rdf = df[df["region"] == region]
        if rdf.empty or rdf["property_count"].sum() < MIN_PROPERTIES:
            continue
        out["regions"][region] = {
            **_metrics(rdf["rev_eur"].sum(), rdf["occ"].sum(), rdf["avail"].sum()),
            "property_count": int(rdf["property_count"].sum()),
            "currency": "EUR", "currency_symbol": "€",
        }

    # By country
    for country, grp in df.groupby("country_name"):
        if grp["property_count"].sum() < MIN_PROPERTIES:
            continue
        m_eur   = _metrics(grp["rev_eur"].sum(),   grp["occ"].sum(), grp["avail"].sum())
        m_local = _metrics(grp["rev_local"].sum(), grp["occ"].sum(), grp["avail"].sum())
        cur_code, cur_sym = COUNTRY_CURRENCY.get(country, ("EUR", "€"))
        out["countries"][country] = {
            "region": COUNTRY_TO_REGION.get(country, "Other"),
            "adr": m_local["adr"], "revpar": m_local["revpar"],
            "occupancy": m_local["occupancy"],
            "adr_eur": m_eur["adr"], "revpar_eur": m_eur["revpar"],
            "property_count": int(grp["property_count"].sum()),
            "currency": cur_code, "currency_symbol": cur_sym,
        }

    return out

# ── YTD growth ────────────────────────────────────────────────────────────────

def fetch_ytd(conn) -> dict:
    print("  Fetching YTD growth...")
    cohort = "2024-01-01"
    mmdd   = _today.strftime("%m%d")
    as_of  = _today.strftime("%b %d")
    pf = (
        f"p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        f"AND CAST(p.pms_property_created_at AS DATE) < '{cohort}' "
        f"AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE) <= DATE_SUB('{cohort}', 90)) "
        f"AND ({REGION_CASE}) != 'Other'"
    )
    df = query(f"""
        SELECT YEAR(m.calendar_date_local) AS year,
               {REGION_CASE} AS region,
               p.country_name,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_NUMS}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND YEAR(m.calendar_date_local) IN (2025, 2026)
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{mmdd}'
        GROUP BY year, region, p.country_name
    """, conn)

    if df.empty:
        return {"global": {}, "regions": {}, "countries": {}}

    for c in ["rev_eur", "occ", "avail", "property_count"]:
        df[c] = _num(df, c)

    def pct(n, o):
        return round((n - o) / o * 100, 1) if o else 0

    def _ytd(d25, d26):
        m25 = _metrics(d25["rev_eur"].sum(), d25["occ"].sum(), d25["avail"].sum())
        m26 = _metrics(d26["rev_eur"].sum(), d26["occ"].sum(), d26["avail"].sum())
        return {
            "adr_2025": m25["adr"],       "adr_2026": m26["adr"],       "adr_chg": pct(m26["adr"], m25["adr"]),
            "occ_2025": m25["occupancy"], "occ_2026": m26["occupancy"], "occ_chg": pct(m26["occupancy"], m25["occupancy"]),
            "rev_2025": m25["revpar"],    "rev_2026": m26["revpar"],    "rev_chg": pct(m26["revpar"], m25["revpar"]),
            "as_of": as_of,
        }

    out = {"global": {}, "regions": {}, "countries": {}}

    d25g, d26g = df[df["year"] == 2025], df[df["year"] == 2026]
    if not d25g.empty and not d26g.empty:
        out["global"] = _ytd(d25g, d26g)

    for region in REGION_ORDER:
        r25 = df[(df["year"] == 2025) & (df["region"] == region)]
        r26 = df[(df["year"] == 2026) & (df["region"] == region)]
        if not r25.empty and not r26.empty and r25["property_count"].sum() >= MIN_PROPERTIES:
            out["regions"][region] = _ytd(r25, r26)

    for country in df["country_name"].dropna().unique():
        c25 = df[(df["year"] == 2025) & (df["country_name"] == country)]
        c26 = df[(df["year"] == 2026) & (df["country_name"] == country)]
        if not c25.empty and not c26.empty and c25["property_count"].sum() >= MIN_PROPERTIES:
            out["countries"][country] = _ytd(c25, c26)

    return out

# ── Historical trends (global + per region, 7-day rolling) ───────────────────

def fetch_trends(conn) -> dict:
    print("  Fetching historical trends...")
    pf = _prop_base(str(PUB_START))
    df = query(f"""
        SELECT dt, region,
               COUNT(DISTINCT pms_property_id) AS property_count,
               SUM(rev_eur) AS rev_eur,
               SUM(occ)     AS occ,
               SUM(avail)   AS avail
        FROM (
            SELECT m.calendar_date_local AS dt,
                   ({REGION_CASE})       AS region,
                   m.pms_property_id,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.total_adjusted_net_accommodation_revenue_eur END AS rev_eur,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_directly_occupied_accommodation_resources END AS occ,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_available_accommodation_resources END AS avail
            FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
            JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
            WHERE {pf}
              AND m.calendar_date_local >= '{PUB_START}'
              AND m.calendar_date_local <  '{PUB_END}'
        ) t
        WHERE region != 'Other'
        GROUP BY dt, region
        ORDER BY dt
    """, conn)

    if df.empty:
        return {"global": [], "regions": {r: [] for r in REGION_ORDER}}

    for c in ["rev_eur", "occ", "avail", "property_count"]:
        df[c] = _num(df, c)
    df["dt"] = pd.to_datetime(df["dt"])
    df = df[df["property_count"] >= MIN_PROPERTIES]

    def _rolling(sub: pd.DataFrame):
        sub = sub.copy().sort_values("dt")
        sub["year"] = sub["dt"].dt.year.astype(str)
        for col in ["rev_eur", "occ", "avail"]:
            sub[col] = sub.groupby("year")[col].transform(
                lambda x: x.rolling(7, min_periods=1).mean()
            )
        rows = []
        for _, r in sub.iterrows():
            m = _metrics(r["rev_eur"], r["occ"], r["avail"])
            rows.append({"date": r["dt"].strftime("%Y-%m-%d"), "year": r["year"], **m})
        return rows

    out = {"global": [], "regions": {}}

    global_df = df.groupby("dt")[["rev_eur", "occ", "avail"]].sum().reset_index()
    out["global"] = _rolling(global_df)

    for region in REGION_ORDER:
        rdf = df[df["region"] == region].groupby("dt")[["rev_eur", "occ", "avail"]].sum().reset_index()
        out["regions"][region] = _rolling(rdf)

    return out

# ── Regional overview (Tab 3 — always all regions) ───────────────────────────

def fetch_regional(conn) -> dict:
    print("  Fetching regional overview...")
    pf = _prop_base(str(PUB_START))

    df_ann = query(f"""
        SELECT year, region,
               COUNT(DISTINCT pms_property_id) AS property_count,
               SUM(rev_eur) AS rev_eur,
               SUM(occ)     AS occ,
               SUM(avail)   AS avail
        FROM (
            SELECT YEAR(m.calendar_date_local) AS year,
                   ({REGION_CASE})             AS region,
                   m.pms_property_id,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.total_adjusted_net_accommodation_revenue_eur END AS rev_eur,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_directly_occupied_accommodation_resources END AS occ,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_available_accommodation_resources END AS avail
            FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
            JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
            WHERE {_prop_base('2024-01-01')}
              AND YEAR(m.calendar_date_local) IN (2024, 2025, 2026)
        ) t
        WHERE region != 'Other'
        GROUP BY year, region
        ORDER BY region, year
    """, conn)

    df_mon = query(f"""
        SELECT month, region,
               COUNT(DISTINCT pms_property_id) AS property_count,
               SUM(rev_eur) AS rev_eur,
               SUM(occ)     AS occ,
               SUM(avail)   AS avail
        FROM (
            SELECT DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
                   ({REGION_CASE})                            AS region,
                   m.pms_property_id,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.total_adjusted_net_accommodation_revenue_eur END AS rev_eur,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_directly_occupied_accommodation_resources END AS occ,
                   CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                        THEN m.num_available_accommodation_resources END AS avail
            FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
            JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
            WHERE {pf}
              AND m.calendar_date_local >= '{PUB_START}'
              AND m.calendar_date_local <  '{PUB_END}'
        ) t
        WHERE region != 'Other'
        GROUP BY month, region
        ORDER BY month, region
    """, conn)

    out = {"annual": [], "monthly": []}

    if not df_ann.empty:
        for c in ["rev_eur", "occ", "avail", "property_count"]:
            df_ann[c] = _num(df_ann, c)
        for _, r in df_ann.iterrows():
            if r["property_count"] < MIN_PROPERTIES or r["region"] == "Other":
                continue
            out["annual"].append({
                "region": r["region"], "year": int(r["year"]),
                "property_count": int(r["property_count"]),
                **_metrics(r["rev_eur"], r["occ"], r["avail"]),
            })

    if not df_mon.empty:
        for c in ["rev_eur", "occ", "avail", "property_count"]:
            df_mon[c] = _num(df_mon, c)
        df_mon["month"] = pd.to_datetime(df_mon["month"])
        for _, r in df_mon.iterrows():
            if r["property_count"] < MIN_PROPERTIES or r["region"] == "Other":
                continue
            out["monthly"].append({
                "region": r["region"], "month": r["month"].strftime("%Y-%m"),
                "property_count": int(r["property_count"]),
                **_metrics(r["rev_eur"], r["occ"], r["avail"]),
            })

    return out

# ── Booking behaviour (Tab 4 — global, 2024 & 2025 only) ─────────────────────

def fetch_behaviour(conn) -> dict:
    print("  Fetching booking behaviour...")

    LEAD_TIME_ORDER = ["0 - Same day","1-3 days","4-7 days","8-14 days",
                       "15-30 days","31-60 days","61-90 days","90+ days"]
    DOW_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    pf_cohort = (
        "p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        "AND CAST(p.pms_property_created_at AS DATE) < MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1) "
        "AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE) "
        "    <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1), 90))"
    )

    # Annual averages
    df_avg = query(f"""
        SELECT YEAR(r.reservation_planned_start_at) AS year,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               AVG(DATEDIFF(r.reservation_planned_end_at, r.reservation_planned_start_at)) AS avg_los,
               AVG(r.person_count) AS avg_group_size,
               AVG(DATEDIFF(r.reservation_planned_start_at, r.reservation_created_at)) AS avg_lead_time
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_cohort}
          AND r.reservation_state_code NOT IN (4) AND r.is_reservation_deleted = FALSE
          AND YEAR(r.reservation_planned_start_at) IN (2024, 2025)
        GROUP BY year ORDER BY year
    """, conn)

    # Cancellations
    pf_canc = (
        "p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        "AND r.is_reservation_deleted = FALSE "
        "AND CAST(p.pms_property_created_at AS DATE) < MAKE_DATE(YEAR(r.reservation_created_at), 1, 1) "
        "AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE) "
        "    <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_created_at), 1, 1), 90))"
    )
    df_canc = query(f"""
        SELECT YEAR(r.reservation_created_at) AS year,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               COUNT(*) AS total_bookings,
               SUM(CASE WHEN r.reservation_state_code = 4 THEN 1 ELSE 0 END) AS cancellations,
               AVG(CASE WHEN r.reservation_state_code = 4
                   THEN DATEDIFF(r.reservation_planned_start_at, r.reservation_canceled_at)
                   ELSE NULL END) AS avg_cancel_window
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_canc}
          AND YEAR(r.reservation_created_at) IN (2024, 2025)
        GROUP BY year ORDER BY year
    """, conn)

    # Lead time distribution (% share per bucket per year)
    pf_lt = (
        "p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        "AND r.reservation_state != 'Canceled' AND r.lead_time_days IS NOT NULL "
        "AND CAST(p.pms_property_created_at AS DATE) < MAKE_DATE(YEAR(r.backfilled_reservation_started_at), 1, 1) "
        "AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE) "
        "    <= DATE_SUB(MAKE_DATE(YEAR(r.backfilled_reservation_started_at), 1, 1), 90))"
    )
    df_lt = query(f"""
        SELECT YEAR(r.backfilled_reservation_started_at) AS year,
               CASE WHEN r.lead_time_days = 0 THEN '0 - Same day'
                    WHEN r.lead_time_days BETWEEN 1  AND 3  THEN '1-3 days'
                    WHEN r.lead_time_days BETWEEN 4  AND 7  THEN '4-7 days'
                    WHEN r.lead_time_days BETWEEN 8  AND 14 THEN '8-14 days'
                    WHEN r.lead_time_days BETWEEN 15 AND 30 THEN '15-30 days'
                    WHEN r.lead_time_days BETWEEN 31 AND 60 THEN '31-60 days'
                    WHEN r.lead_time_days BETWEEN 61 AND 90 THEN '61-90 days'
                    ELSE '90+ days' END AS bucket,
               SUM(r.count_reservations) AS reservations
        FROM product.marts.mrt_reservations_and_guests r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_lt}
          AND YEAR(r.backfilled_reservation_started_at) IN (2024, 2025)
        GROUP BY year, bucket
    """, conn)

    # Check-in / Check-out DOW
    pf_dow = (
        "p.is_deleted = FALSE AND p.subscription_state = 'Enabled' "
        "AND r.is_reservation_deleted = FALSE AND r.reservation_state_code NOT IN (4)"
    )
    df_cin = query(f"""
        SELECT YEAR(r.reservation_planned_start_at) AS year,
               DATE_FORMAT(r.reservation_planned_start_at, 'EEEE') AS dow,
               COUNT(*) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_dow}
          AND YEAR(r.reservation_planned_start_at) IN (2024, 2025)
          AND CAST(p.pms_property_created_at AS DATE) < MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1), 90))
        GROUP BY year, dow
    """, conn)

    df_cout = query(f"""
        SELECT YEAR(r.reservation_planned_end_at) AS year,
               DATE_FORMAT(r.reservation_planned_end_at, 'EEEE') AS dow,
               COUNT(*) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_dow}
          AND YEAR(r.reservation_planned_end_at) IN (2024, 2025)
          AND CAST(p.pms_property_created_at AS DATE) < MAKE_DATE(YEAR(r.reservation_planned_end_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_planned_end_at), 1, 1), 90))
        GROUP BY year, dow
    """, conn)

    # ── Structure output ──────────────────────────────────────────────────────

    out = {"annual": [], "cancellations": [], "lead_time": [], "checkin_dow": [], "checkout_dow": []}

    if not df_avg.empty:
        for c in ["avg_los", "avg_group_size", "avg_lead_time", "property_count"]:
            df_avg[c] = _num(df_avg, c)
        for _, r in df_avg.iterrows():
            if r["property_count"] >= MIN_PROPERTIES:
                out["annual"].append({
                    "year": int(r["year"]),
                    "avg_los":        round(float(r["avg_los"]), 1),
                    "avg_group_size": round(float(r["avg_group_size"]), 1),
                    "avg_lead_time":  round(float(r["avg_lead_time"]), 1),
                })

    if not df_canc.empty:
        for c in ["total_bookings", "cancellations", "avg_cancel_window", "property_count"]:
            df_canc[c] = _num(df_canc, c)
        for _, r in df_canc.iterrows():
            if r["property_count"] >= MIN_PROPERTIES and r["total_bookings"] > 0:
                out["cancellations"].append({
                    "year": int(r["year"]),
                    "cancel_rate":       round(float(r["cancellations"]) / float(r["total_bookings"]) * 100, 1),
                    "avg_cancel_window": round(float(r["avg_cancel_window"]), 1),
                })

    if not df_lt.empty:
        df_lt["reservations"] = _num(df_lt, "reservations")
        totals = df_lt.groupby("year")["reservations"].sum().to_dict()
        pivot = {}
        for _, r in df_lt.iterrows():
            b, yr = r["bucket"], str(int(r["year"]))
            pivot.setdefault(b, {})[yr] = float(r["reservations"])
        for bucket in LEAD_TIME_ORDER:
            if bucket in pivot:
                entry = {"bucket": bucket}
                for yr in ["2024", "2025"]:
                    cnt = pivot[bucket].get(yr, 0)
                    tot = totals.get(int(yr), 1)
                    entry[yr] = round(cnt / tot * 100, 1)
                out["lead_time"].append(entry)

    def _pivot_dow(df_dow):
        if df_dow.empty:
            return []
        df_dow["reservations"] = _num(df_dow, "reservations")
        totals = df_dow.groupby("year")["reservations"].sum().to_dict()
        pivot = {}
        for _, r in df_dow.iterrows():
            pivot.setdefault(r["dow"], {})[str(int(r["year"]))] = float(r["reservations"])
        rows = []
        for day in DOW_ORDER:
            if day in pivot:
                entry = {"day": day}
                for yr in ["2024", "2025"]:
                    cnt = pivot[day].get(yr, 0)
                    tot = totals.get(int(yr), 1)
                    entry[yr] = round(cnt / tot * 100, 1)
                rows.append(entry)
        return rows

    out["checkin_dow"]  = _pivot_dow(df_cin)
    out["checkout_dow"] = _pivot_dow(df_cout)

    return out

# ── FX rate ───────────────────────────────────────────────────────────────────

def fetch_fx_usd(conn) -> float:
    try:
        df = query("""
            SELECT exchange_rate_value
            FROM product.facts.fct_exchange_rates
            WHERE source_currency_code = 'USD' AND valid_to = '9999-12-31'
            LIMIT 1
        """, conn)
        if not df.empty:
            return round(float(df["exchange_rate_value"].iloc[0]), 4)
    except Exception as e:
        print(f"  FX rate fetch failed ({e}), using fallback 1.10")
    return 1.10

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = get_conn()

    kpis      = fetch_kpis(conn)
    ytd       = fetch_ytd(conn)
    trends    = fetch_trends(conn)
    regional  = fetch_regional(conn)
    behaviour = fetch_behaviour(conn)
    usd_rate  = fetch_fx_usd(conn)

    conn.close()

    # Merge YTD into KPIs
    kpis["global"]["ytd"] = ytd.get("global", {})
    for region in REGION_ORDER:
        if region in kpis["regions"]:
            kpis["regions"][region]["ytd"] = ytd.get("regions", {}).get(region, {})
    for country in kpis["countries"]:
        kpis["countries"][country]["ytd"] = ytd.get("countries", {}).get(country, {})

    meta = {
        "pub_start":    str(PUB_START),
        "pub_end":      str(PUB_END),
        "generated_at": _today.isoformat(),
        "fx_rates":     {"USD": usd_rate},
    }

    files = {
        "meta.json":      meta,
        "kpis.json":      kpis,
        "trends.json":    trends,
        "regional.json":  regional,
        "behaviour.json": behaviour,
    }

    for name, data in files.items():
        path = os.path.join(OUTPUT_DIR, name)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Wrote {path}")

    print("Done.")


if __name__ == "__main__":
    main()
