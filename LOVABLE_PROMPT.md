# Lovable prompt — Mews Public Benchmark Dashboard

Paste the text below into Lovable when creating the project connected to this repo.

---

Build a public-facing hotel industry benchmark dashboard for Mews. The app reads pre-computed JSON data files from `/public/data/` — no backend queries needed.

## Data files (already in repo)

| File | Contents |
|---|---|
| `/public/data/meta.json` | `pub_start`, `pub_end`, `generated_at`, `fx_rates.USD` |
| `/public/data/kpis.json` | ADR / Occupancy / RevPAR for `global`, `regions{}`, `countries{}` — each with `ytd` sub-object |
| `/public/data/trends.json` | `global` (array), `regions{}` (arrays) — items: `{date, year, adr, revpar, occupancy}` in EUR; `countries{}` — each: `{region, currency, currency_symbol, data[]}` where `data` uses the same item shape in local currency |
| `/public/data/regional.json` | `annual[]` and `monthly[]` for all 5 regions: `{region, year/month, adr, revpar, occupancy, property_count}` |
| `/public/data/behaviour.json` | `global{annual[], cancellations[], lead_time[], checkin_dow[], checkout_dow[]}` and `regions{"North America": {...}, "Europe": {...}, ...}` — same shape per region |
| `/public/analyst_insight.md` | Markdown text for Tab 1. Fetch and render as formatted text. |

Load all JSON files with `fetch('/data/filename.json')` on mount.

---

## Layout

Full-width page, Mews brand colours (primary: `#CC3535`, secondary: `#1A1A2E`). Clean, professional — this is public-facing.

### Sidebar (left, collapsible on mobile)

**Region selector** — pill/button group, single-select:

```
[ 🌍 Global ]  [ North America ]  [ South America ]  [ Europe ]  [ APAC ]  [ MEA ]
```

- Default: Global selected
- When a non-Global region is selected, show a second row of country buttons for that region (from `kpis.countries` filtered by `country.region`)
- Only one region or country active at a time; clicking the active region deselects back to Global

**Date range** — read-only text showing `meta.pub_start` → `meta.pub_end`, with a small "Updated monthly" caption underneath.

**No date pickers, no segment filter.**

---

## Currency logic

| Selection | Currency | How |
|---|---|---|
| Global or any region except North America | EUR (€) | Use `adr`, `revpar` from data directly |
| North America region | USD ($) | Multiply EUR values by `meta.fx_rates.USD` |
| Any single country | Local currency always | Use `adr`, `revpar` from `kpis.countries[country]` directly; symbol from `currency_symbol` |

---

## Tabs (4 total)

### Tab 1 — 💡 Analyst Insights

Fetch `/public/analyst_insight.md`, render as formatted Markdown inside a card. Full width.

### Tab 2 — 📊 Market KPIs

**Three metric tiles** (side by side):
- ADR (with currency symbol)
- Occupancy (%)
- RevPAR (with currency symbol)

Source: `kpis[selectedEntity]` where `selectedEntity` is `global`, `regions[name]`, or `countries[name]` based on sidebar selection.

**YTD Growth table** — compact 3-column table:

| Metric | 2025 (full year) | 2026 YTD | Change |
|---|---|---|---|
| ADR | €X | €Y | +X.X% ↑ |
| Occupancy | X% | Y% | +X.X% ↑ |
| RevPAR | €X | €Y | +X.X% ↑ |

Source: `kpis[selectedEntity].ytd`. Show a small `as_of` caption ("as of Jun 15").

**Historical Performance** — two line charts side by side (ADR left, Occupancy right), with RevPAR below full width. Each chart overlays 2025 (grey line) and 2026 (coloured line). X-axis: Jan–Dec with month labels. Resolve trend data as follows:
- Global selected → `trends.global` (EUR)
- Region selected → `trends.regions[name]` (EUR)
- Country selected → `trends.countries[name].data` in local currency (`trends.countries[name].currency_symbol`); if the country is not present in `trends.countries`, fall back to the parent region's data in EUR

### Tab 3 — 🗺️ Regional Overview

**Always shows all 5 regions regardless of sidebar selection.**

**Annual tiles** — a grid of cards, one per region, with columns for 2024 / 2025 / 2026 (YTD). Show ADR, Occupancy, RevPAR per cell. Source: `regional.annual`.

**Monthly trend lines** — three line charts (ADR, Occupancy, RevPAR) each overlaying all 5 regions as separate coloured lines. X-axis: months from `meta.pub_start` to `meta.pub_end`. Source: `regional.monthly`.

Region colour map:
- North America: `#3B82F6` (blue)
- South America: `#10B981` (green)
- Europe: `#CC3535` (Mews red)
- APAC: `#F59E0B` (amber)
- MEA: `#8B5CF6` (purple)

### Tab 4 — 🔍 Booking Behaviour

Show a note at the top: *"Booking behaviour data covers full calendar years 2024 and 2025."*

**Annual averages** — two-column tile grid:

| Year | Avg Lead Time | Avg LOS | Avg Group Size |
|---|---|---|---|
| 2024 | X days | X nights | X guests |
| 2025 | X days | X nights | X guests |

Source: `behaviourSlice.annual` where `behaviourSlice` is resolved as described below.

**Cancellations** — two metric tiles per year (Cancellation Rate %, Avg Cancellation Window days). Source: `behaviourSlice.cancellations`.

**Reservations by Lead Time** — grouped bar chart, 2024 vs 2025, X-axis = lead time buckets (`behaviourSlice.lead_time`). Y-axis = % share.

**Arrivals by Day of Week** — bar chart, 2024 vs 2025. Source: `behaviourSlice.checkin_dow`. Y-axis = % share.

**Departures by Day of Week** — bar chart, 2024 vs 2025. Source: `behaviourSlice.checkout_dow`. Y-axis = % share.

**Behaviour slice resolution** — `behaviour.json` has a `global` key and a `regions` object keyed by region name. Resolve `behaviourSlice` as follows:
- Global selected → `behaviour.global`
- Region selected → `behaviour.regions[selectedRegion]`
- Country selected → `behaviour.regions[parentRegionOfCountry]` (fall back to the parent region; behaviour data is not computed per country)

---

## Footer

Small text: *"Data covers Mews-connected properties that were live for the full period shown. Updated monthly with a 30-day lag. All room revenue metrics use room-weighted averages."*

---

## Tech notes

- React + TypeScript
- Recharts for all charts
- Tailwind CSS for styling
- Data loaded once on mount via `Promise.all(fetch(...))`, stored in state
- No authentication, no backend — pure static site
- Must work when deployed to GitHub Pages or Netlify
