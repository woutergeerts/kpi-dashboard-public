import os
import streamlit as st
import pandas as pd
from datetime import date, timedelta
from databricks import sql
from dotenv import load_dotenv

load_dotenv()

DATABRICKS_HOST      = os.getenv("DATABRICKS_HOST")
DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
DATABRICKS_TOKEN     = os.getenv("DATABRICKS_TOKEN")

MIN_PROPERTIES = 5

# ── Region mapping — based on country_name ────────────────────────────────────
COUNTRY_TO_REGION = {
    # North America
    "United States": "North America", "Canada": "North America",
    # Europe
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
    # APAC
    "Australia": "APAC", "New Zealand": "APAC", "Japan": "APAC",
    "Thailand": "APAC", "Indonesia": "APAC", "Philippines": "APAC",
    "Singapore": "APAC", "Malaysia": "APAC", "Hong Kong": "APAC",
    "Cambodia": "APAC", "Chinese Taipei": "APAC", "Fiji": "APAC",
    "French Polynesia": "APAC", "Korea, Republic of": "APAC",
    "Samoa": "APAC", "Tonga": "APAC", "Vanuatu": "APAC",
    # South America
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
    # MEA
    "South Africa": "MEA", "Morocco": "MEA", "Reunion": "MEA", "Réunion": "MEA",
    "Georgia": "MEA", "Mauritius": "MEA", "Egypt": "MEA",
    "Namibia": "MEA", "Congo, the Democratic Republic of the": "MEA",
    "Israel": "MEA", "Kenya": "MEA",
    "Cote d'Ivoire": "MEA", "Côte d'Ivoire": "MEA",
    "Ghana": "MEA", "Nigeria": "MEA", "Turkey": "MEA",
}
REGION_ORDER = ["North America", "South America", "Europe", "APAC", "MEA"]


def _build_region_case(alias: str = "p") -> str:
    lines = []
    for country, region in COUNTRY_TO_REGION.items():
        safe = country.replace("'", "''")
        lines.append(f"        WHEN '{safe}' THEN '{region}'")
    return "CASE " + alias + ".country_name\n" + "\n".join(lines) + "\n        ELSE 'Other'\n    END"

REGION_CASE = _build_region_case()

# ── Room-weighted metric expressions ─────────────────────────────────────────
# These replace AVG(adr_eur), AVG(revpar_eur), and the per-property occupancy
# ratio average used throughout the original queries.
#
#   ADR       = total room revenue / total occupied rooms
#   RevPAR    = total room revenue / total available rooms
#   Occupancy = total occupied rooms / total available rooms  (× 100 for %)
#
# Seasonality fix: days where a property has available rooms but zero occupied
# rooms are excluded from the denominator. These are days when a seasonal hotel
# is effectively closed (it hasn't blocked its rooms in Mews, so num_available
# remains non-zero, but it has no guests at all). Including those days would
# inflate the available room count and artificially depress occupancy and RevPAR
# for seasonal markets like Greece and Spain.
#
# A day with even one occupied room is kept — only fully empty open-inventory
# days are excluded. This is equivalent to the STR "open hotels only" approach.
#
# Both the actuals table and the OTB table share the same column names, so this
# constant works for both.

ROOM_METRICS_SQL = """\
    NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.total_adjusted_net_accommodation_revenue_eur END), 0)
        / NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.num_directly_occupied_accommodation_resources END), 0)  AS adr,
    NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.total_adjusted_net_accommodation_revenue_eur END), 0)
        / NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.num_available_accommodation_resources END), 0)          AS revpar,
    NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.num_directly_occupied_accommodation_resources END), 0)
        / NULLIF(SUM(CASE WHEN m.num_directly_occupied_accommodation_resources > 0
                    THEN m.num_available_accommodation_resources END), 0) * 100    AS occupancy"""

# Same formula but using local currency revenue instead of EUR.
ROOM_METRICS_SQL_LOCAL = ROOM_METRICS_SQL.replace(
    "m.total_adjusted_net_accommodation_revenue_eur",
    "m.total_adjusted_net_accommodation_revenue",
)


# ── DB connection ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    return sql.connect(
        server_hostname=DATABRICKS_HOST,
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_TOKEN,
    )

TTL = 3600 * 24 * 31  # ~monthly; busts naturally when pub_end date arg changes

@st.cache_data(ttl=TTL)
def query(sql_str: str) -> pd.DataFrame:
    with get_conn().cursor() as cur:
        cur.execute(sql_str)
        return cur.active_result_set.fetchall_arrow().to_pandas()


# ── Private helpers ───────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return s.replace("'", "''")


def _go_live_clause(period_start: str) -> str:
    return (f"(p.go_live_date IS NULL OR "
            f"CAST(p.go_live_date AS DATE) <= DATE_SUB('{period_start}', 90))")


def _region_country_clause(region: tuple, country: tuple) -> str:
    parts = []
    if region:
        countries_in_region = [c for c, r in COUNTRY_TO_REGION.items() if r in region]
        if countries_in_region:
            c_list = ", ".join(f"'{_esc(c)}'" for c in countries_in_region)
            parts.append(f"AND p.country_name IN ({c_list})")
    if country:
        c_list = ", ".join(f"'{_esc(c)}'" for c in country)
        parts.append(f"AND p.country_name IN ({c_list})")
    return " ".join(parts)


def prop_filter(f: dict) -> str:
    parts = [
        "p.is_deleted = FALSE",
        "p.subscription_state = 'Enabled'",
        f"CAST(p.pms_property_created_at AS DATE) < '{f['start']}'",
        _go_live_clause(f['start']),
    ]
    extra = _region_country_clause(
        tuple(f.get("region") or []),
        tuple(f.get("country") or []),
    )
    if extra:
        parts.append(extra.lstrip("AND "))
    if f.get("segment"):
        s_list = ", ".join(f"'{s}'" for s in f["segment"])
        parts.append(f"p.commercial_segment IN ({s_list})")
    return " AND ".join(parts)


def res_date_filter(f: dict) -> str:
    return (
        f"r.reservation_planned_start_at >= '{f['start']}' "
        f"AND r.reservation_planned_start_at < '{f['end']}' "
        f"AND r.reservation_state_code NOT IN (4) "
        f"AND r.is_reservation_deleted = FALSE"
    )


def _check_min_properties(df: pd.DataFrame, col: str = "property_count") -> bool:
    if df.empty:
        return False
    if col in df.columns:
        return df[col].sum() >= MIN_PROPERTIES
    return True


# ── Filter options ────────────────────────────────────────────────────────────

def get_regions() -> list:
    return REGION_ORDER


@st.cache_data(ttl=TTL)
def get_countries() -> list:
    df = query("""
        SELECT DISTINCT country_name
        FROM product.dimensions.dim_pms_properties
        WHERE country_name IS NOT NULL
          AND country_name != ''
          AND is_deleted = FALSE
          AND subscription_state = 'Enabled'
        ORDER BY country_name
    """)
    return df["country_name"].tolist()


@st.cache_data(ttl=TTL)
def get_segments() -> list:
    df = query("""
        SELECT DISTINCT commercial_segment
        FROM product.dimensions.dim_pms_properties
        WHERE commercial_segment IS NOT NULL AND is_deleted = FALSE
        ORDER BY commercial_segment
    """)
    return df["commercial_segment"].tolist()


# ── Property count check ──────────────────────────────────────────────────────

def count_properties(f: dict) -> int:
    pf = prop_filter(f)
    df = query(f"""
        SELECT COUNT(DISTINCT p.pms_property_id) AS n
        FROM product.dimensions.dim_pms_properties p
        WHERE {pf}
    """)
    return int(df["n"].iloc[0]) if not df.empty else 0


# ── KPIs ──────────────────────────────────────────────────────────────────────

def load_kpis(f: dict) -> dict:
    pf = prop_filter(f)
    _rms = ROOM_METRICS_SQL_LOCAL if f.get("local") else ROOM_METRICS_SQL
    df_res = query(f"""
        SELECT COUNT(DISTINCT r.reservation_id)  AS reservations,
               COUNT(DISTINCT r.pms_property_id) AS enterprises
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf} AND {res_date_filter(f)}
    """)
    df_m = query(f"""
        SELECT
            {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.calendar_date_local >= '{f['start']}'
          AND m.calendar_date_local <  '{f['end']}'
    """)
    return {
        "reservations": int(df_res["reservations"].iloc[0]) if not df_res.empty else 0,
        "enterprises":  int(df_res["enterprises"].iloc[0])  if not df_res.empty else 0,
        "adr":       float(df_m["adr"].iloc[0])       if not df_m.empty and df_m["adr"].iloc[0]       else 0,
        "occupancy": float(df_m["occupancy"].iloc[0]) if not df_m.empty and df_m["occupancy"].iloc[0] else 0,
        "revpar":    float(df_m["revpar"].iloc[0])    if not df_m.empty and df_m["revpar"].iloc[0]    else 0,
    }


# ── YTD growth ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=TTL)
def load_ytd_growth(region: tuple, country: tuple, segment: tuple,
                    cohort_start: str = "2024-01-01", local: bool = False) -> dict:
    extra = _region_country_clause(region, country)
    parts = [
        "p.is_deleted = FALSE", "p.subscription_state = 'Enabled'",
        f"CAST(p.pms_property_created_at AS DATE) < '{cohort_start}'",
        _go_live_clause(cohort_start),
    ]
    if extra:
        parts.append(extra.lstrip("AND "))
    if segment:
        parts.append(f"p.commercial_segment IN ({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})")
    pf = " AND ".join(parts)

    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL
    today = date.today()
    today_mmdd = today.strftime("%m%d")
    df = query(f"""
        SELECT YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND YEAR(m.calendar_date_local) IN (2025, 2026)
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
        GROUP BY year ORDER BY year
    """)
    if df.empty or len(df) < 2:
        return {}
    if df["property_count"].min() < MIN_PROPERTIES:
        return {"too_few": True}
    r25, r26 = df[df["year"] == 2025].iloc[0], df[df["year"] == 2026].iloc[0]
    def pct(n, o): return (float(n) - float(o)) / float(o) * 100 if float(o) else 0
    return {
        "adr_2025": float(r25["adr"]),       "adr_2026": float(r26["adr"]),
        "adr_chg":  pct(r26["adr"], r25["adr"]),
        "occ_2025": float(r25["occupancy"]),  "occ_2026": float(r26["occupancy"]),
        "occ_chg":  pct(r26["occupancy"], r25["occupancy"]),
        "revpar_2025": float(r25["revpar"]),  "revpar_2026": float(r26["revpar"]),
        "revpar_chg":  pct(r26["revpar"], r25["revpar"]),
        "as_of": today.strftime("%b %d"),
    }


# ── OTB growth ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=TTL)
def load_otb_growth(region: tuple, country: tuple, segment: tuple,
                    cohort_start: str = "2024-01-01", local: bool = False) -> dict:
    extra = _region_country_clause(region, country)
    parts = [
        "p.is_deleted = FALSE", "p.subscription_state = 'Enabled'",
        f"CAST(p.pms_property_created_at AS DATE) < '{cohort_start}'",
        _go_live_clause(cohort_start),
    ]
    if extra:
        parts.append(extra.lstrip("AND "))
    if segment:
        parts.append(f"p.commercial_segment IN ({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})")
    pf = " AND ".join(parts)

    snap_df = query("SELECT MAX(snapshot_date_local) AS latest FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property_on_the_books")
    if snap_df.empty or snap_df["latest"].iloc[0] is None:
        return {}
    snap    = date.fromisoformat(str(snap_df["latest"].iloc[0]))
    snap_ly = date(snap.year - 1, snap.month, snap.day)

    otb_metrics = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL

    df = query(f"""
        SELECT CASE WHEN m.snapshot_date_local='{snap}' THEN 'this_year'
                    WHEN m.snapshot_date_local='{snap_ly}' THEN 'last_year' END AS period,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {otb_metrics}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property_on_the_books m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.snapshot_date_local IN ('{snap}','{snap_ly}')
          AND DATEDIFF(m.on_the_books_date_local, m.snapshot_date_local) BETWEEN 0 AND 274
        GROUP BY period
    """)
    if df.empty or len(df) < 2:
        return {}
    if df["property_count"].min() < MIN_PROPERTIES:
        return {"too_few": True}
    ty = df[df["period"] == "this_year"].iloc[0]
    ly = df[df["period"] == "last_year"].iloc[0]
    def pct(n, o): return (float(n) - float(o)) / float(o) * 100 if float(o) else 0
    return {
        "adr_ly": float(ly["adr"]),       "adr_ty": float(ty["adr"]),
        "adr_chg": pct(ty["adr"], ly["adr"]),
        "occ_ly": float(ly["occupancy"]), "occ_ty": float(ty["occupancy"]),
        "occ_chg": pct(ty["occupancy"], ly["occupancy"]),
        "revpar_ly": float(ly["revpar"]), "revpar_ty": float(ty["revpar"]),
        "revpar_chg": pct(ty["revpar"], ly["revpar"]),
        "snap_date": str(snap),
    }


# ── Historical trends ─────────────────────────────────────────────────────────

def load_trends(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    _rms = ROOM_METRICS_SQL_LOCAL if f.get("local") else ROOM_METRICS_SQL
    df = query(f"""
        SELECT m.calendar_date_local AS date,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.calendar_date_local >= '{f['start']}'
          AND m.calendar_date_local < '{f['end']}'
        GROUP BY m.calendar_date_local ORDER BY m.calendar_date_local
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["year"].astype(str)
    df = df.sort_values("date")
    for col in ["adr", "occupancy", "revpar"]:
        df[col] = df.groupby("year")[col].transform(lambda x: x.rolling(7, min_periods=1).mean())
    df["day_of_year"] = df["date"].dt.dayofyear
    df["month_label"] = df["date"].dt.strftime("%b")
    return df


# ── OTB trends ────────────────────────────────────────────────────────────────

def load_otb_trends(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    snap_df = query("SELECT MAX(snapshot_date_local) AS latest FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property_on_the_books")
    if snap_df.empty or snap_df["latest"].iloc[0] is None:
        return pd.DataFrame()
    snap    = date.fromisoformat(str(snap_df["latest"].iloc[0]))
    snap_ly = date(snap.year - 1, snap.month, snap.day)

    otb_metrics = ROOM_METRICS_SQL_LOCAL if f.get("local") else ROOM_METRICS_SQL

    df = query(f"""
        SELECT DATEDIFF(m.on_the_books_date_local, m.snapshot_date_local) AS days_ahead,
               CASE WHEN m.snapshot_date_local='{snap}' THEN 'This Year (OTB)'
                    WHEN m.snapshot_date_local='{snap_ly}' THEN 'Last Year (OTB)' END AS year_label,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {otb_metrics}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property_on_the_books m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.snapshot_date_local IN ('{snap}','{snap_ly}')
          AND DATEDIFF(m.on_the_books_date_local, m.snapshot_date_local) BETWEEN 0 AND 274
        GROUP BY days_ahead, year_label ORDER BY days_ahead
    """)
    if df.empty:
        return df
    df = df[df["year_label"].notna() & (df["property_count"] >= MIN_PROPERTIES)]
    if df.empty:
        return df
    df = df.sort_values(["year_label", "days_ahead"])
    for col in ["adr", "occupancy", "revpar"]:
        df[col] = df.groupby("year_label")[col].transform(lambda x: x.rolling(7, min_periods=1).mean())
    df["display_date"] = df["days_ahead"].apply(
        lambda d: (snap + timedelta(days=int(d))).strftime("%b %d"))
    return df


# ── Shared helper: mrt_reservations_and_guests filter ────────────────────────
# Used by lead time, LOS, and group size queries.
# mrt_reservations_and_guests already contains commercial_segment and country_code,
# but we still join dim_pms_properties for country_name, customer_status, and go_live_date.
# Date filtering uses backfilled_reservation_started_at (the mart's preferred arrival date).

def _mrt_res_filter(f: dict) -> str:
    """WHERE clause for mrt_reservations_and_guests + dim_pms_properties join."""
    parts = [
        "p.is_deleted = FALSE",
        "p.subscription_state = 'Enabled'",
        f"CAST(p.pms_property_created_at AS DATE) < '{f['start']}'",
        _go_live_clause(f['start']),
        "r.reservation_state != 'Canceled'",
        f"r.backfilled_reservation_started_at >= '{f['start']}'",
        f"r.backfilled_reservation_started_at < '{f['end']}'",
    ]
    extra = _region_country_clause(
        tuple(f.get("region") or []),
        tuple(f.get("country") or []),
    )
    if extra:
        parts.append(extra.lstrip("AND "))
    if f.get("segment"):
        s_list = ", ".join(f"'{s}'" for s in f["segment"])
        parts.append(f"p.commercial_segment IN ({s_list})")
    return " AND ".join(parts)


# ── Lead time ─────────────────────────────────────────────────────────────────
LEAD_TIME_ORDER = ["0 - Same day","1-3 days","4-7 days","8-14 days",
                   "15-30 days","31-60 days","61-90 days","90+ days"]

def load_lead_time(f: dict) -> pd.DataFrame:
    mf = _mrt_res_filter(f)
    df = query(f"""
        SELECT CASE WHEN r.lead_time_days = 0 THEN '0 - Same day'
                    WHEN r.lead_time_days BETWEEN 1 AND 3 THEN '1-3 days'
                    WHEN r.lead_time_days BETWEEN 4 AND 7 THEN '4-7 days'
                    WHEN r.lead_time_days BETWEEN 8 AND 14 THEN '8-14 days'
                    WHEN r.lead_time_days BETWEEN 15 AND 30 THEN '15-30 days'
                    WHEN r.lead_time_days BETWEEN 31 AND 60 THEN '31-60 days'
                    WHEN r.lead_time_days BETWEEN 61 AND 90 THEN '61-90 days'
                    ELSE '90+ days' END AS lead_time_bucket,
               SUM(r.count_reservations) AS reservations
        FROM product.marts.mrt_reservations_and_guests r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {mf} AND r.lead_time_days IS NOT NULL
        GROUP BY lead_time_bucket
    """)
    df["lead_time_bucket"] = pd.Categorical(
        df["lead_time_bucket"], categories=LEAD_TIME_ORDER, ordered=True)
    return df.sort_values("lead_time_bucket")


# ── Length of stay ────────────────────────────────────────────────────────────
LOS_ORDER = ["1 night","2 nights","3 nights","4-7 nights","8-14 nights","15+ nights"]

def load_length_of_stay(f: dict) -> pd.DataFrame:
    mf = _mrt_res_filter(f)
    df = query(f"""
        SELECT CASE WHEN r.stay_length_days = 1 THEN '1 night'
                    WHEN r.stay_length_days = 2 THEN '2 nights'
                    WHEN r.stay_length_days = 3 THEN '3 nights'
                    WHEN r.stay_length_days BETWEEN 4 AND 7 THEN '4-7 nights'
                    WHEN r.stay_length_days BETWEEN 8 AND 14 THEN '8-14 nights'
                    ELSE '15+ nights' END AS los_bucket,
               SUM(r.count_reservations) AS reservations
        FROM product.marts.mrt_reservations_and_guests r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {mf} AND r.stay_length_days IS NOT NULL AND r.stay_length_days > 0
        GROUP BY los_bucket
    """)
    df["los_bucket"] = pd.Categorical(
        df["los_bucket"], categories=LOS_ORDER, ordered=True)
    return df.sort_values("los_bucket")


# ── Group size ────────────────────────────────────────────────────────────────

def load_group_size(f: dict) -> pd.DataFrame:
    mf = _mrt_res_filter(f)
    return query(f"""
        SELECT CASE WHEN r.person_count = 1 THEN '1 guest'
                    WHEN r.person_count = 2 THEN '2 guests'
                    WHEN r.person_count = 3 THEN '3 guests'
                    WHEN r.person_count BETWEEN 4 AND 6 THEN '4-6 guests'
                    ELSE '7+ guests' END AS group_size_bucket,
               SUM(r.count_reservations) AS reservations
        FROM product.marts.mrt_reservations_and_guests r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {mf} AND r.person_count IS NOT NULL
        GROUP BY group_size_bucket ORDER BY MIN(r.person_count)
    """)


# ── Channel ───────────────────────────────────────────────────────────────────

def load_channel(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT ra.reservation_origin AS origin,
               ra.reservation_commander_origin AS commander_origin,
               COUNT(DISTINCT r.reservation_id) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        JOIN product.dimensions.dim_reservation_attributes ra
          ON r.reservation_attributes_key = ra.reservation_attributes_key
        WHERE {pf} AND {res_date_filter(f)} AND ra.reservation_origin != 'Import'
        GROUP BY ra.reservation_origin, ra.reservation_commander_origin
    """)
    if df.empty:
        return df
    def bucket(row):
        o, co = row["origin"] or "", row["commander_origin"] or ""
        if o in ("ChannelManager"): return "Third-Party Channels"
        if o in ("Distributor","Connector"):    return "Online Direct"
        if o == "Commander" and co == "Website": return "Online Direct"
        if o == "Commander":                    return "Offline Direct"
        return None
    df["channel"] = df.apply(bucket, axis=1)
    return df[df["channel"].notna()].groupby("channel")["reservations"].sum().reset_index()


# ── Payment type ──────────────────────────────────────────────────────────────

def load_payment_type(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT td.detailed_transaction_type AS payment_type, COUNT(*) AS transactions
        FROM fintech.public.obt_transactions__all_transactions t
        JOIN fintech.public.dim_transactions__transaction_details td
          ON t.transaction_details_id = td.transaction_details_id
        JOIN product.dimensions.dim_pms_properties p ON t.enterprise_id = p.pms_property_id
        WHERE {pf} AND t.transaction_state = 'Charged'
          AND CAST(t.created_at AS DATE) >= '{f['start']}'
          AND CAST(t.created_at AS DATE) < '{f['end']}'
          AND td.detailed_transaction_type IS NOT NULL
        GROUP BY td.detailed_transaction_type ORDER BY transactions DESC
    """)
    if df.empty:
        return df
    def bucket(t):
        if t in {"Mews Card Payment","Card","Digital Wallet","Mews Alternative Payment"}:
            return "Card & Digital Payments"
        if t in {"WireTransfer","CrossSettlement","Prepayment"}:
            return "Bank Transfer & Settlement"
        if t in {"Invoice","Commission"}:
            return "Invoice & Corporate Billing"
        if t in {"Voucher","Cheque"}:
            return "Voucher & Cheque"
        if t == "Cash":
            return "Cash"
        return "Other"
    df["category"] = df["payment_type"].apply(bucket)
    return df.groupby("category")["transactions"].sum().reset_index()


# ── Card network ──────────────────────────────────────────────────────────────

def load_card_network(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    return query(f"""
        SELECT CASE WHEN cd.payment_card_network IN ('Visa','VPay','CarteBleue') THEN 'Visa'
                    WHEN cd.payment_card_network IN ('MasterCard','Maestro','Bancontact','Bancomat','Giro') THEN 'Mastercard'
                    WHEN cd.payment_card_network = 'Amex' THEN 'Amex'
                    ELSE 'Other' END AS card_network,
               COUNT(*) AS transactions
        FROM fintech.public.obt_transactions__all_transactions t
        JOIN fintech.public.dim_transactions__card_details cd
          ON t.card_details_id = cd.card_details_id
        JOIN product.dimensions.dim_pms_properties p ON t.enterprise_id = p.pms_property_id
        WHERE {pf} AND t.transaction_state = 'Charged' AND t.is_card_transaction = TRUE
          AND CAST(t.created_at AS DATE) >= '{f['start']}'
          AND CAST(t.created_at AS DATE) < '{f['end']}'
        GROUP BY card_network ORDER BY transactions DESC
    """)


# ── P&L ───────────────────────────────────────────────────────────────────────
PNL_LABEL_MAP = {
    "Accommodation":"Rooms","FoodAndBeverage":"Food & Beverage",
    "Events":"Events & Meetings","Wellness":"Wellness & Spa",
    "Facilities":"Facilities","Sport":"Sport & Recreation",
    "Tourism":"Tourism","Technology":"Technology",
    "SundryIncome":"Sundry Income","ExternalRevenue":"External Revenue",
    "NotAssigned":"Not Assigned",
}
PNL_ORDER = ["Rooms","Food & Beverage","Events & Meetings","Wellness & Spa","Facilities",
             "Sport & Recreation","Tourism","Technology","Sundry Income",
             "External Revenue","Not Assigned"]

def load_pnl_monthly(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    _rev_col = "m.total_adjusted_net_value" if f.get("local") else "m.total_adjusted_net_value_eur"
    df = query(f"""
        SELECT DATE_TRUNC('MONTH', m.revenue_date_local) AS month,
               m.accounting_category_classification AS category,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               SUM({_rev_col}) AS net_revenue_eur
        FROM product.marts.mrt_daily_property_revenue_per_accounting_category_classification m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.revenue_date_local >= '{f['start']}' AND m.revenue_date_local < '{f['end']}'
          AND m.accounting_category_revenue_type NOT IN ('PaymentsRevenue','TaxesRevenue')
        GROUP BY month, category ORDER BY month, net_revenue_eur DESC
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["month"] = pd.to_datetime(df["month"])
    df["category"] = df["category"].map(PNL_LABEL_MAP).fillna(df["category"])
    return df

def load_pnl_mix(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    _rev_col = "m.total_adjusted_net_value" if f.get("local") else "m.total_adjusted_net_value_eur"
    df = query(f"""
        SELECT m.accounting_category_classification AS category,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               SUM({_rev_col}) AS net_revenue_eur
        FROM product.marts.mrt_daily_property_revenue_per_accounting_category_classification m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND m.revenue_date_local >= '{f['start']}' AND m.revenue_date_local < '{f['end']}'
          AND m.accounting_category_revenue_type NOT IN ('PaymentsRevenue','TaxesRevenue')
        GROUP BY category ORDER BY net_revenue_eur DESC
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["category"] = df["category"].map(PNL_LABEL_MAP).fillna(df["category"])
    return df


# ── Revenue per sqm ───────────────────────────────────────────────────────────

def load_revenue_per_sqm(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    _rev_col = "rev.total_adjusted_net_value" if f.get("local") else "rev.total_adjusted_net_value_eur"
    df = query(f"""
        SELECT rev.accounting_category_classification AS category,
               COUNT(DISTINCT rev.pms_property_id) AS property_count,
               SUM({_rev_col}) AS total_revenue_eur,
               SUM({_rev_col}) / NULLIF(SUM(CASE
                   WHEN rev.accounting_category_classification='Accommodation'
                       THEN sqm.room_area_meters_squared
                   WHEN rev.accounting_category_classification='FoodAndBeverage'
                       THEN sqm.eating_and_drinking_areas_meters_squared
                   WHEN rev.accounting_category_classification='Events'
                       THEN sqm.meeting_and_event_rooms_area_meters_squared
                   WHEN rev.accounting_category_classification IN ('Wellness','Sport')
                       THEN sqm.wellness_and_sports_area_meters_squared
               END), 0) AS revenue_per_sqm_eur
        FROM product.marts.mrt_daily_property_revenue_per_accounting_category_classification rev
        JOIN tech.value_pillars.dim_scraped_property_sqm_estimations sqm
	ON rev.pms_property_id = sqm.pms_property_id
        JOIN product.dimensions.dim_pms_properties p ON rev.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND rev.revenue_date_local >= '{f['start']}' AND rev.revenue_date_local < '{f['end']}'
          AND rev.accounting_category_revenue_type NOT IN ('PaymentsRevenue','TaxesRevenue')
          AND rev.accounting_category_classification IN
              ('Accommodation','FoodAndBeverage','Events','Wellness','Sport')
          AND sqm.accuracy_grade >= 3
        GROUP BY category ORDER BY revenue_per_sqm_eur DESC
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["category"] = df["category"].map({
        "Accommodation":"Rooms","FoodAndBeverage":"Food & Beverage",
        "Events":"Events & Meetings","Wellness":"Wellness & Spa",
        "Sport":"Sport & Recreation",
    }).fillna(df["category"])
    return df


# ── Revenue per sqm by property type ─────────────────────────────────────────

PROPERTY_TYPE_GROUPS = {
    "Hotel":      "Hotel",
    "Resort":     "Hotel",
    "Motel":      "Hotel",
    "Hostel":     "Hostel",
    "Apartments": "Serviced Apartments",
    "Villas":     "Serviced Apartments",
    "Campsite":   "Campsite",
}
PROPERTY_TYPE_ORDER = ["Hotel", "Hostel", "Serviced Apartments", "Campsite"]

_PNL_LABEL_MAP_SQM = {
    "Accommodation": "Rooms",
    "FoodAndBeverage": "Food & Beverage",
    "Events": "Events & Meetings",
    "Wellness": "Wellness & Spa",
    "Sport": "Sport & Recreation",
}

@st.cache_data(ttl=TTL)
def load_revenue_per_sqm_by_property_type(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    _rev_col = "rev.total_adjusted_net_value" if f.get("local") else "rev.total_adjusted_net_value_eur"
    df = query(f"""
        SELECT category,
               property_type_group,
               COUNT(DISTINCT pms_property_id) AS property_count,
               SUM(rev) / NULLIF(SUM(sqm_area), 0) AS revenue_per_sqm_eur
        FROM (
            SELECT rev.accounting_category_classification AS category,
                   CASE p.pms_property_type
                       WHEN 'Hotel'      THEN 'Hotel'
                       WHEN 'Resort'     THEN 'Hotel'
                       WHEN 'Motel'      THEN 'Hotel'
                       WHEN 'Hostel'     THEN 'Hostel'
                       WHEN 'Apartments' THEN 'Serviced Apartments'
                       WHEN 'Villas'     THEN 'Serviced Apartments'
                       WHEN 'Campsite'   THEN 'Campsite'
                   END AS property_type_group,
                   rev.pms_property_id,
                   SUM({_rev_col}) AS rev,
                   MAX(CASE
                       WHEN rev.accounting_category_classification='Accommodation'
                           THEN sqm.room_area_meters_squared
                       WHEN rev.accounting_category_classification='FoodAndBeverage'
                           THEN sqm.eating_and_drinking_areas_meters_squared
                       WHEN rev.accounting_category_classification='Events'
                           THEN sqm.meeting_and_event_rooms_area_meters_squared
                       WHEN rev.accounting_category_classification IN ('Wellness','Sport')
                           THEN sqm.wellness_and_sports_area_meters_squared
                   END) AS sqm_area
            FROM product.marts.mrt_daily_property_revenue_per_accounting_category_classification rev
            JOIN tech.value_pillars.dim_scraped_property_sqm_estimations sqm
                ON rev.pms_property_id = sqm.pms_property_id
            JOIN product.dimensions.dim_pms_properties p ON rev.pms_property_id = p.pms_property_id
            WHERE {pf}
              AND rev.revenue_date_local >= '{f['start']}' AND rev.revenue_date_local < '{f['end']}'
              AND rev.accounting_category_revenue_type NOT IN ('PaymentsRevenue','TaxesRevenue')
              AND rev.accounting_category_classification IN
                  ('Accommodation','FoodAndBeverage','Events','Wellness','Sport')
              AND sqm.accuracy_grade >= 3
            GROUP BY category, property_type_group, rev.pms_property_id
        ) sub
        WHERE property_type_group IS NOT NULL
        GROUP BY category, property_type_group
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["category"] = df["category"].map(_PNL_LABEL_MAP_SQM).fillna(df["category"])
    df["property_type_group"] = pd.Categorical(
        df["property_type_group"], categories=PROPERTY_TYPE_ORDER, ordered=True
    )
    df = df.sort_values(["category", "property_type_group"])
    return df


# ── Upsells ───────────────────────────────────────────────────────────────────

def load_upsell_by_channel(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT COUNT(DISTINCT u.pms_property_id) AS property_count,
               SUM(u.count_booking_engine_upsells) AS booking_engine,
               SUM(u.count_kiosk_upsells) AS kiosk,
               SUM(u.count_guest_portal_upsells) AS online_checkin,
               SUM(u.count_front_desk_upsells) AS front_desk
        FROM product.marts.mrt_daily_upsells_channel_shares_and_weights_per_accounting_category u
        JOIN product.dimensions.dim_pms_properties p ON u.pms_property_id = p.pms_property_id
        WHERE {pf} AND u.checkin_date >= '{f['start']}' AND u.checkin_date < '{f['end']}'
    """)
    if df.empty or int(df["property_count"].iloc[0] or 0) < MIN_PROPERTIES:
        return pd.DataFrame()
    row = df.iloc[0]
    result = pd.DataFrame({
        "channel": ["Booking Engine","Kiosk","Online Check-in (OCI)","Front Desk (PMS)"],
        "upsells": [float(row["booking_engine"] or 0), float(row["kiosk"] or 0),
                    float(row["online_checkin"] or 0), float(row["front_desk"] or 0)],
    })
    total = result["upsells"].sum()
    result["pct"] = result["upsells"] / total * 100 if total > 0 else 0
    return result

def load_upsell_by_category(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    return query(f"""
        SELECT COALESCE(u.accounting_category_classification,'NotAssigned') AS category,
               SUM(u.count_total_upsells) AS upsells,
               SUM(u.total_upsell_gross_value_eur) AS total_value_eur,
               CASE WHEN SUM(u.count_total_upsells) > 0
                    THEN SUM(u.total_upsell_gross_value_eur) / SUM(u.count_total_upsells)
                    ELSE 0 END AS avg_value_eur
        FROM product.marts.mrt_daily_upsells_by_product u
        JOIN product.dimensions.dim_pms_properties p ON u.pms_property_id = p.pms_property_id
        WHERE {pf} AND u.checkin_date >= '{f['start']}' AND u.checkin_date < '{f['end']}'
          AND u.accounting_category_classification NOT IN ('Payments','Taxes','NotAssigned')
        GROUP BY category ORDER BY upsells DESC LIMIT 10
    """)

def load_upsell_avg_value_by_channel(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT COUNT(DISTINCT u.pms_property_id) AS property_count,
               AVG(u.avg_booking_engine_upsell_value_per_reservation_eur) AS booking_engine,
               AVG(u.avg_kiosk_upsell_value_per_reservation_eur) AS kiosk,
               AVG(u.avg_guest_portal_upsell_value_per_reservation_eur) AS online_checkin,
               AVG(u.avg_front_desk_upsell_value_per_reservation_eur) AS front_desk
        FROM product.marts.mrt_daily_upsells_channel_shares_and_weights_per_accounting_category u
        JOIN product.dimensions.dim_pms_properties p ON u.pms_property_id = p.pms_property_id
        WHERE {pf} AND u.checkin_date >= '{f['start']}' AND u.checkin_date < '{f['end']}'
    """)
    if df.empty or int(df["property_count"].iloc[0] or 0) < MIN_PROPERTIES:
        return pd.DataFrame()
    row = df.iloc[0]
    return pd.DataFrame({
        "channel": ["Booking Engine","Kiosk","Online Check-in (OCI)","Front Desk (PMS)"],
        "avg_value": [float(row["booking_engine"] or 0), float(row["kiosk"] or 0),
                      float(row["online_checkin"] or 0), float(row["front_desk"] or 0)],
    })


# ── Check-in ──────────────────────────────────────────────────────────────────

def load_checkin_prestay(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    return query(f"""
        SELECT 'Online Check-in (OCI)' AS checkin_method, COUNT(*) AS reservations
        FROM product.facts.fct_reservation_checkins__online ci
        JOIN product.facts.fct_reservations r ON ci.reservation_id = r.reservation_id
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND ci.online_check_in_finished_at >= '{f['start']}'
          AND ci.online_check_in_finished_at < '{f['end']}'
          AND r.is_reservation_deleted = FALSE
    """)

def load_checkin_athotel(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    # Kiosk and Front Desk: sourced from mrt_daily_checkins_channel_shares_and_weights,
    # which is the verified source used in the PBI model.
    # count_kiosk_checkins includes any kiosk involvement (Mews Kiosk + non-Mews Kiosk).
    # count_non_mews_kiosk_checkins is added separately so we don't double-count,
    # and we combine both into a single "Kiosk" bucket.
    df_kfd = query(f"""
        SELECT
            SUM(ci.count_kiosk_checkins + ci.count_non_mews_kiosk_checkins) AS kiosk,
            SUM(ci.count_front_desk_checkins)                                AS front_desk
        FROM product.marts.mrt_daily_checkins_channel_shares_and_weights ci
        JOIN product.dimensions.dim_pms_properties p ON ci.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND ci.checkin_date >= '{f['start']}'
          AND ci.checkin_date < '{f['end']}'
    """)
    # Digital Key: sourced from mrt_digital_key_checkin_metrics.
    # Only rows where checkin_type = 'Mews Digital Key' AND has_opened_door = 'opened door'
    # represent a successful DK check-in.
    df_dk = query(f"""
        SELECT SUM(dk.count_reservations) AS digital_key
        FROM product.marts.mrt_digital_key_checkin_metrics dk
        JOIN product.dimensions.dim_pms_properties p ON dk.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND dk.arrival_date >= '{f['start']}'
          AND dk.arrival_date < '{f['end']}'
          AND dk.checkin_type = 'Mews Digital Key'
          AND dk.has_opened_door = 'opened door'
    """)
    rows = []
    if not df_kfd.empty:
        rows.append({"checkin_method": "Kiosk",
                     "reservations": float(df_kfd["kiosk"].iloc[0] or 0)})
        rows.append({"checkin_method": "Front Desk (PMS)",
                     "reservations": float(df_kfd["front_desk"].iloc[0] or 0)})
    if not df_dk.empty:
        rows.append({"checkin_method": "Digital Key",
                     "reservations": float(df_dk["digital_key"].iloc[0] or 0)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ── Day of week ───────────────────────────────────────────────────────────────
DOW_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

def load_checkin_dow(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT DATE_FORMAT(r.reservation_planned_start_at, 'EEEE') AS day_of_week,
               COUNT(*) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf} AND {res_date_filter(f)}
        GROUP BY day_of_week
    """)
    df["day_of_week"] = pd.Categorical(df["day_of_week"], categories=DOW_ORDER, ordered=True)
    df["pct"] = df["reservations"] / df["reservations"].sum() * 100
    return df.sort_values("day_of_week")

def load_checkout_dow(f: dict) -> pd.DataFrame:
    pf = prop_filter(f)
    df = query(f"""
        SELECT DATE_FORMAT(r.reservation_planned_end_at, 'EEEE') AS day_of_week,
               COUNT(*) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf} AND {res_date_filter(f)}
        GROUP BY day_of_week
    """)
    df["day_of_week"] = pd.Categorical(df["day_of_week"], categories=DOW_ORDER, ordered=True)
    df["pct"] = df["reservations"] / df["reservations"].sum() * 100
    return df.sort_values("day_of_week")


# ── Cancellations ─────────────────────────────────────────────────────────────

# Shared base filter for both cancellation queries.
# Does NOT include the go-live or property-created-at filters — those are applied
# per-year using MAKE_DATE() inside the query, consistent with the original logic.
def _canc_base_filter(f: dict) -> str:
    extra = _region_country_clause(
        tuple(f.get("region") or []),
        tuple(f.get("country") or []),
    )
    pf = "p.is_deleted = FALSE AND p.subscription_state = 'Enabled'"
    if extra:
        pf += " " + extra
    if f.get("segment"):
        s_list = ", ".join(f"'{s}'" for s in f["segment"])
        pf += f" AND p.commercial_segment IN ({s_list})"
    return pf


def load_cancellation_stats(f: dict) -> pd.DataFrame:
    pf_base = _canc_base_filter(f)
    df = query(f"""
        SELECT YEAR(r.reservation_created_at) AS year,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               COUNT(*) AS total_bookings,
               SUM(CASE WHEN r.reservation_state_code = 4 THEN 1 ELSE 0 END) AS cancellations,
               AVG(CASE WHEN r.reservation_state_code = 4
                   THEN DATEDIFF(r.reservation_planned_start_at, r.reservation_canceled_at)
                   ELSE NULL END) AS avg_cancel_window
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_base}
          AND r.is_reservation_deleted = FALSE
          AND YEAR(r.reservation_created_at) IN (2024, 2025)
          AND CAST(p.pms_property_created_at AS DATE)
              < MAKE_DATE(YEAR(r.reservation_created_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_created_at), 1, 1), 90))
        GROUP BY year ORDER BY year
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["cancel_rate"] = df["cancellations"] / df["total_bookings"] * 100
    return df


def load_channel_adr(f: dict) -> pd.DataFrame:
    """
    ADR by booking channel and year, using room-weighted methodology consistent
    with the rest of the dashboard.

    Approach: for each non-cancelled reservation, we fan out one row per night it
    occupies (join to the daily metrics table on property + date range). For each
    such room-night we attribute the property's per-occupied-room revenue that day
    (total_adjusted_net_accommodation_revenue_eur / num_directly_occupied).
    Summing that per channel and dividing by the count of room-nights gives a
    channel-weighted ADR that is directly comparable to the market-level ADR shown
    elsewhere in the dashboard.

    Days where a property has zero occupied rooms are excluded (seasonality fix,
    consistent with ROOM_METRICS_SQL).
    """
    extra = _region_country_clause(
        tuple(f.get("region") or []),
        tuple(f.get("country") or []),
    )
    parts = [
        "p.is_deleted = FALSE", "p.subscription_state = 'Enabled'",
        _go_live_clause(f["start"]),
        f"CAST(p.pms_property_created_at AS DATE) < '{f['start']}'",
    ]
    if extra:
        parts.append(extra.lstrip("AND "))
    if f.get("segment"):
        s_list = ", ".join(f"'{_esc(s)}'" for s in f["segment"])
        parts.append(f"p.commercial_segment IN ({s_list})")
    pf = " AND ".join(parts)
    _adr_rev_col = ("m.total_adjusted_net_accommodation_revenue"
                    if f.get("local")
                    else "m.total_adjusted_net_accommodation_revenue_eur")

    df = query(f"""
        SELECT YEAR(m.calendar_date_local) AS year,
               CASE
                   WHEN ra.reservation_origin IN ('ChannelManager','Connector')
                       THEN 'Third-Party'
                   WHEN ra.reservation_origin IN ('Distributor','Navigator')
                       THEN 'Direct'
                   WHEN ra.reservation_origin = 'Commander'
                        AND ra.reservation_commander_origin = 'Website'
                       THEN 'Direct'
                   WHEN ra.reservation_origin = 'Commander'
                       THEN 'Direct'
               END AS channel,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               COUNT(*)  AS room_nights,
               SUM({_adr_rev_col}
                   / NULLIF(m.num_directly_occupied_accommodation_resources, 0))
                   / NULLIF(COUNT(*), 0)  AS adr_eur
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_reservation_attributes ra
          ON r.reservation_attributes_key = ra.reservation_attributes_key
        JOIN product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
          ON m.pms_property_id      = r.pms_property_id
         AND m.calendar_date_local >= CAST(r.reservation_planned_start_at AS DATE)
         AND m.calendar_date_local  < CAST(r.reservation_planned_end_at   AS DATE)
        JOIN product.dimensions.dim_pms_properties p
          ON r.pms_property_id = p.pms_property_id
        WHERE {pf}
          AND r.is_reservation_deleted = FALSE
          AND r.reservation_state_code NOT IN (4)
          AND ra.reservation_origin != 'Import'
          AND m.num_directly_occupied_accommodation_resources > 0
          AND m.calendar_date_local >= '{f['start']}'
          AND m.calendar_date_local  < '{f['end']}'
        GROUP BY year, channel
        HAVING channel IS NOT NULL
        ORDER BY year, channel
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    return df


def load_channel_behaviour(f: dict) -> pd.DataFrame:
    """
    Average LOS, lead time, and group size broken down by booking channel and year.
    Uses mrt_reservations_and_guests with weighted averages:
    SUM(metric × count_reservations) / SUM(count_reservations).
    Applies the same cohort filter (property created before year start, go-live buffer)
    as the existing annual behaviour query, but adds channel dimension.
    """
    extra = _region_country_clause(
        tuple(f.get("region") or []),
        tuple(f.get("country") or []),
    )
    parts = ["p.is_deleted = FALSE", "p.subscription_state = 'Enabled'"]
    if extra:
        parts.append(extra.lstrip("AND "))
    if f.get("segment"):
        s_list = ", ".join(f"'{_esc(s)}'" for s in f["segment"])
        parts.append(f"p.commercial_segment IN ({s_list})")
    pf_base = " AND ".join(parts)

    df = query(f"""
        SELECT YEAR(r.reservation_created_date) AS year,
               CASE
                   WHEN r.reservation_origin IN ('ChannelManager','Connector')
                       THEN 'Third-Party'
                   WHEN r.reservation_origin IN ('Distributor','Navigator')
                       THEN 'Direct'
                   WHEN r.reservation_origin = 'Commander'
                        AND r.reservation_commander_origin = 'Website'
                       THEN 'Direct'
                   WHEN r.reservation_origin = 'Commander'
                       THEN 'Direct'
               END AS channel,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               SUM(r.count_reservations) AS total_reservations,
               SUM(r.stay_length_days * r.count_reservations)
                   / NULLIF(SUM(r.count_reservations), 0) AS avg_los,
               SUM(r.lead_time_days * r.count_reservations)
                   / NULLIF(SUM(r.count_reservations), 0) AS avg_lead_time,
               SUM(r.person_count * r.count_reservations)
                   / NULLIF(SUM(r.count_reservations), 0) AS avg_group_size
        FROM product.marts.mrt_reservations_and_guests r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_base}
          AND r.reservation_state != 'Canceled'
          AND r.reservation_origin != 'Import'
          AND YEAR(r.reservation_created_date) IN (2024, 2025, 2026)
          AND CAST(p.pms_property_created_at AS DATE)
              < MAKE_DATE(YEAR(r.reservation_created_date), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_created_date), 1, 1), 90))
        GROUP BY year, channel
        HAVING channel IS NOT NULL
        ORDER BY year, channel
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    return df


def load_cancellation_by_channel(f: dict) -> pd.DataFrame:
    """
    Cancellation rate and avg cancellation window broken down by booking channel
    and year (2024, 2025, 2026). Uses fct_reservations + dim_reservation_attributes
    because the cancellation window requires individual-reservation-level date maths
    that cannot be reproduced from pre-aggregated mart tables.
    Channel buckets mirror the rest of the dashboard:
      Third-Party   = ChannelManager, Connector
      Online Direct = Distributor, Navigator, Commander/Website
      Offline Direct = Commander (all other commander origins)
    """
    pf_base = _canc_base_filter(f)
    df = query(f"""
        SELECT YEAR(r.reservation_created_at) AS year,
               CASE
                   WHEN ra.reservation_origin IN ('ChannelManager','Connector')
                       THEN 'Third-Party'
                   WHEN ra.reservation_origin IN ('Distributor','Navigator')
                       THEN 'Direct'
                   WHEN ra.reservation_origin = 'Commander'
                        AND ra.reservation_commander_origin = 'Website'
                       THEN 'Direct'
                   WHEN ra.reservation_origin = 'Commander'
                       THEN 'Direct'
               END AS channel,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               COUNT(*) AS total_bookings,
               SUM(CASE WHEN r.reservation_state_code = 4 THEN 1 ELSE 0 END) AS cancellations,
               AVG(CASE WHEN r.reservation_state_code = 4
                   THEN DATEDIFF(r.reservation_planned_start_at, r.reservation_canceled_at)
                   ELSE NULL END) AS avg_cancel_window
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_reservation_attributes ra
          ON r.reservation_attributes_key = ra.reservation_attributes_key
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_base}
          AND r.is_reservation_deleted = FALSE
          AND ra.reservation_origin != 'Import'
          AND YEAR(r.reservation_created_at) IN (2024, 2025, 2026)
          AND CAST(p.pms_property_created_at AS DATE)
              < MAKE_DATE(YEAR(r.reservation_created_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_created_at), 1, 1), 90))
        GROUP BY year, channel
        HAVING channel IS NOT NULL
        ORDER BY year, channel
    """)
    if df.empty:
        return df
    df = df[df["property_count"] >= MIN_PROPERTIES]
    if df.empty:
        return df
    df["cancel_rate"] = df["cancellations"] / df["total_bookings"] * 100
    return df


# ── Annual booking behaviour averages ─────────────────────────────────────────

@st.cache_data(ttl=TTL)
def load_behaviour_annual(region: tuple, country: tuple, segment: tuple) -> dict:
    extra = _region_country_clause(region, country)
    parts = ["p.is_deleted = FALSE", "p.subscription_state = 'Enabled'"]
    if extra:
        parts.append(extra.lstrip("AND "))
    if segment:
        parts.append(f"p.commercial_segment IN ({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})")
    pf_base = " AND ".join(parts)

    df_avg = query(f"""
        SELECT YEAR(r.reservation_planned_start_at) AS year,
               COUNT(DISTINCT r.pms_property_id) AS property_count,
               AVG(DATEDIFF(r.reservation_planned_end_at, r.reservation_planned_start_at)) AS avg_los,
               AVG(r.person_count) AS avg_group_size,
               AVG(DATEDIFF(r.reservation_planned_start_at, r.reservation_created_at)) AS avg_lead_time
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        WHERE {pf_base}
          AND r.reservation_state_code NOT IN (4)
          AND r.is_reservation_deleted = FALSE
          AND YEAR(r.reservation_planned_start_at) IN (2024, 2025)
          AND CAST(p.pms_property_created_at AS DATE)
              < MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1), 90))
        GROUP BY year ORDER BY year
    """)
    if not df_avg.empty:
        df_avg = df_avg[df_avg["property_count"] >= MIN_PROPERTIES]

    df_ch = query(f"""
        SELECT YEAR(r.reservation_planned_start_at) AS year,
               ra.reservation_origin AS origin,
               ra.reservation_commander_origin AS commander_origin,
               COUNT(DISTINCT r.reservation_id) AS reservations
        FROM product.facts.fct_reservations r
        JOIN product.dimensions.dim_pms_properties p ON r.pms_property_id = p.pms_property_id
        JOIN product.dimensions.dim_reservation_attributes ra
          ON r.reservation_attributes_key = ra.reservation_attributes_key
        WHERE {pf_base}
          AND r.reservation_state_code NOT IN (4)
          AND r.is_reservation_deleted = FALSE
          AND ra.reservation_origin != 'Import'
          AND YEAR(r.reservation_planned_start_at) IN (2024, 2025)
          AND CAST(p.pms_property_created_at AS DATE)
              < MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1)
          AND (p.go_live_date IS NULL OR CAST(p.go_live_date AS DATE)
              <= DATE_SUB(MAKE_DATE(YEAR(r.reservation_planned_start_at), 1, 1), 90))
        GROUP BY year, origin, commander_origin
    """)

    def bucket(row):
        o, co = row["origin"] or "", row["commander_origin"] or ""
        if o in ("ChannelManager","Connector"): return "Third-Party"
        if o in ("Distributor","Navigator"):    return "Online Direct"
        if o == "Commander" and co == "Website": return "Online Direct"
        if o == "Commander":                    return "Offline Direct"
        return None

    channel_pcts = {}
    if not df_ch.empty:
        df_ch["channel"] = df_ch.apply(bucket, axis=1)
        df_ch = df_ch[df_ch["channel"].notna()]
        for yr in [2024, 2025]:
            yr_df = df_ch[df_ch["year"] == yr]
            if yr_df.empty:
                channel_pcts[yr] = {}
                continue
            total = yr_df["reservations"].sum()
            channel_pcts[yr] = {
                row["channel"]: row["reservations"] / total * 100
                for _, row in yr_df.groupby("channel")["reservations"]
                .sum().reset_index().iterrows()
            }
    return {"averages": df_avg, "channel_pcts": channel_pcts}


# ── Regional Overview ─────────────────────────────────────────────────────────

@st.cache_data(ttl=TTL)
def load_regional_annual(segment: tuple, region: tuple, country: tuple,
                          local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    extra = _region_country_clause(region, country)
    rc = REGION_CASE
    today_mmdd = date.today().strftime("%m%d")
    go_live = _go_live_clause("2024-01-01")
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL

    df = query(f"""
        SELECT {rc} AS region,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause} {extra}
        GROUP BY {rc}, year ORDER BY {rc}, year
    """)
    global_df = query(f"""
        SELECT 'Global' AS region,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause}
        GROUP BY year ORDER BY year
    """)
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    return pd.concat([global_df, df], ignore_index=True)


@st.cache_data(ttl=TTL)
def load_regional_monthly(segment: tuple, start: str, end: str,
                           region: tuple, country: tuple,
                           local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    extra = _region_country_clause(region, country)
    rc = REGION_CASE
    go_live = _go_live_clause(start)
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL
    base = (f"p.is_deleted=FALSE AND p.subscription_state='Enabled' "
            f"AND CAST(p.pms_property_created_at AS DATE)<'{start}' "
            f"AND {go_live} "
            f"AND m.calendar_date_local>='{start}' AND m.calendar_date_local<'{end}' "
            f"{seg_clause} {extra}")
    global_base = (f"p.is_deleted=FALSE AND p.subscription_state='Enabled' "
                   f"AND CAST(p.pms_property_created_at AS DATE)<'{start}' "
                   f"AND {go_live} "
                   f"AND m.calendar_date_local>='{start}' AND m.calendar_date_local<'{end}' "
                   f"{seg_clause}")
    df = query(f"""
        SELECT {rc} AS region,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {base} GROUP BY {rc}, month ORDER BY {rc}, month
    """)
    global_df = query(f"""
        SELECT 'Global' AS region,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {global_base} GROUP BY month ORDER BY month
    """)
    if df.empty and global_df.empty:
        return pd.DataFrame()
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    combined = pd.concat([global_df, df], ignore_index=True)
    combined["month"] = pd.to_datetime(combined["month"])
    return combined.sort_values(["region", "month"])


@st.cache_data(ttl=TTL)
def load_country_annual(segment: tuple, country: tuple,
                         local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    today_mmdd = date.today().strftime("%m%d")
    go_live = _go_live_clause("2024-01-01")
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL

    if country:
        c_list = ", ".join(f"'{_esc(c)}'" for c in country)
        country_clause = f"AND p.country_name IN ({c_list})"
    else:
        country_clause = ""

    df = query(f"""
        SELECT p.country_name,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause} {country_clause}
        GROUP BY p.country_name, year ORDER BY p.country_name, year
    """)
    global_df = query(f"""
        SELECT 'Global' AS country_name,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause}
        GROUP BY year ORDER BY year
    """)
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    return pd.concat([global_df, df], ignore_index=True)


@st.cache_data(ttl=TTL)
def load_country_monthly(segment: tuple, start: str, end: str,
                         country: tuple,
                         local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    go_live = _go_live_clause(start)
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL

    if country:
        c_list = ", ".join(f"'{_esc(c)}'" for c in country)
        country_clause = f"AND p.country_name IN ({c_list})"
    else:
        country_clause = ""

    base = (f"p.is_deleted=FALSE AND p.subscription_state='Enabled' "
            f"AND CAST(p.pms_property_created_at AS DATE)<'{start}' "
            f"AND {go_live} "
            f"AND m.calendar_date_local>='{start}' AND m.calendar_date_local<'{end}' "
            f"{seg_clause}")

    df = query(f"""
        SELECT p.country_name,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {base} {country_clause}
        GROUP BY p.country_name, month ORDER BY p.country_name, month
    """)
    global_df = query(f"""
        SELECT 'Global' AS country_name,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {base}
        GROUP BY month ORDER BY month
    """)
    if df.empty and global_df.empty:
        return pd.DataFrame()
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    combined = pd.concat([global_df, df], ignore_index=True)
    combined["month"] = pd.to_datetime(combined["month"])
    return combined.sort_values(["country_name", "month"])


# ── Hotel Class ───────────────────────────────────────────────────────────────
HOTEL_CLASS_COL   = "hotel_class"
HOTEL_CLASS_ORDER = ["Luxury","Upscale","Midscale","Economy"]

@st.cache_data(ttl=TTL)
def load_hotelclass_annual(segment: tuple, region: tuple = (),
                            country: tuple = (),
                            local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    extra = _region_country_clause(region, country)
    today_mmdd = date.today().strftime("%m%d")
    go_live = _go_live_clause("2024-01-01")
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL

    df = query(f"""
        SELECT p.{HOTEL_CLASS_COL} AS hotel_class,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND p.{HOTEL_CLASS_COL} IS NOT NULL
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause} {extra}
        GROUP BY hotel_class, year ORDER BY hotel_class, year
    """)
    global_df = query(f"""
        SELECT 'Global' AS hotel_class,
               YEAR(m.calendar_date_local) AS year,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE p.is_deleted=FALSE AND p.subscription_state='Enabled'
          AND YEAR(m.calendar_date_local) IN (2024,2025,2026)
          AND CAST(p.pms_property_created_at AS DATE) < '2024-01-01'
          AND {go_live}
          AND DATE_FORMAT(m.calendar_date_local, 'MMdd') <= '{today_mmdd}'
          {seg_clause}
        GROUP BY year ORDER BY year
    """)
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    return pd.concat([global_df, df], ignore_index=True)


@st.cache_data(ttl=TTL)
def load_hotelclass_monthly(segment: tuple, start: str, end: str,
                             region: tuple = (), country: tuple = (),
                             local: bool = False, fx_rate: float = 1.0) -> pd.DataFrame:
    seg_clause = (f"AND p.commercial_segment IN "
                  f"({', '.join(f'{chr(39)}{s}{chr(39)}' for s in segment)})"
                  if segment else "")
    extra = _region_country_clause(region, country)
    go_live = _go_live_clause(start)
    _rms = ROOM_METRICS_SQL_LOCAL if local else ROOM_METRICS_SQL
    base = (f"p.is_deleted=FALSE AND p.subscription_state='Enabled' "
            f"AND CAST(p.pms_property_created_at AS DATE)<'{start}' "
            f"AND {go_live} "
            f"AND m.calendar_date_local>='{start}' AND m.calendar_date_local<'{end}' "
            f"AND p.{HOTEL_CLASS_COL} IS NOT NULL {seg_clause} {extra}")
    global_base = (f"p.is_deleted=FALSE AND p.subscription_state='Enabled' "
                   f"AND CAST(p.pms_property_created_at AS DATE)<'{start}' "
                   f"AND {go_live} "
                   f"AND m.calendar_date_local>='{start}' AND m.calendar_date_local<'{end}' "
                   f"{seg_clause}")
    df = query(f"""
        SELECT p.{HOTEL_CLASS_COL} AS hotel_class,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {_rms}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {base} GROUP BY hotel_class, month ORDER BY hotel_class, month
    """)
    global_df = query(f"""
        SELECT 'Global' AS hotel_class,
               DATE_TRUNC('MONTH', m.calendar_date_local) AS month,
               COUNT(DISTINCT m.pms_property_id) AS property_count,
               {ROOM_METRICS_SQL}
        FROM product.marts.mrt_daily_resource_and_revenue_metrics_per_property m
        JOIN product.dimensions.dim_pms_properties p ON m.pms_property_id = p.pms_property_id
        WHERE {global_base} GROUP BY month ORDER BY month
    """)
    if df.empty and global_df.empty:
        return pd.DataFrame()
    if not df.empty:
        df = df[df["property_count"] >= MIN_PROPERTIES]
    if local and fx_rate != 1.0:
        for col in ("adr", "revpar"):
            if col in global_df.columns:
                global_df[col] = global_df[col].astype(float) * fx_rate
    combined = pd.concat([global_df, df], ignore_index=True)
    combined["month"] = pd.to_datetime(combined["month"])
    return combined.sort_values(["hotel_class", "month"])


# ── Country → currency mapping (non-EUR only) ─────────────────────────────────
# Values: (ISO currency code, display symbol/prefix)
COUNTRY_CURRENCY: dict[str, tuple[str, str]] = {
    "United States":                        ("USD", "$"),
    "Canada":                               ("CAD", "CA$"),
    "United Kingdom":                       ("GBP", "£"),
    "Jersey":                               ("GBP", "£"),
    "Switzerland":                          ("CHF", "CHF "),
    "Sweden":                               ("SEK", "kr "),
    "Norway":                               ("NOK", "kr "),
    "Denmark":                              ("DKK", "kr "),
    "Iceland":                              ("ISK", "kr "),
    "Czech Republic":                       ("CZK", "Kč "),
    "Hungary":                              ("HUF", "Ft "),
    "Poland":                               ("PLN", "zł "),
    "Ukraine":                              ("UAH", "₴"),
    "Russian Federation":                   ("RUB", "₽"),
    "Faroe Islands":                        ("DKK", "kr "),
    "Australia":                            ("AUD", "A$"),
    "New Zealand":                          ("NZD", "NZ$"),
    "Japan":                                ("JPY", "¥"),
    "Thailand":                             ("THB", "฿"),
    "Indonesia":                            ("IDR", "Rp "),
    "Philippines":                          ("PHP", "₱"),
    "Singapore":                            ("SGD", "S$"),
    "Malaysia":                             ("MYR", "RM "),
    "Hong Kong":                            ("HKD", "HK$"),
    "Cambodia":                             ("KHR", "KHR "),
    "Korea, Republic of":                   ("KRW", "₩"),
    "Chinese Taipei":                       ("TWD", "NT$"),
    "South Africa":                         ("ZAR", "R "),
    "Turkey":                               ("TRY", "₺"),
    "Israel":                               ("ILS", "₪"),
    "Morocco":                              ("MAD", "MAD "),
    "Georgia":                              ("GEL", "₾"),
    "Egypt":                                ("EGP", "E£"),
    "Kenya":                                ("KES", "KSh "),
    "Nigeria":                              ("NGN", "₦"),
    "Brazil":                               ("BRL", "R$"),
    "Mexico":                               ("MXN", "MX$"),
    "Colombia":                             ("COP", "COP "),
    "Costa Rica":                           ("CRC", "₡"),
    "Peru":                                 ("PEN", "S/ "),
    "Chile":                                ("CLP", "CLP "),
    "Argentina":                            ("ARS", "AR$"),
    "Dominican Republic":                   ("DOP", "RD$"),
}


@st.cache_data(ttl=TTL)
def load_fx_rate(currency_code: str) -> float:
    """Returns units of currency_code per 1 EUR (multiply EUR value to get local)."""
    if currency_code == "EUR":
        return 1.0
    df = query(f"""
        SELECT exchange_rate_value
        FROM product.facts.fct_exchange_rates
        WHERE source_currency_code = '{currency_code}'
          AND valid_to = '9999-12-31'
        LIMIT 1
    """)
    if df.empty or pd.isna(df["exchange_rate_value"].iloc[0]):
        return 1.0
    return float(df["exchange_rate_value"].iloc[0])