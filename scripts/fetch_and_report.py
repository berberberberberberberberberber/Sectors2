"""
Daily IDX sector performance pipeline.

1. Pull the list of IDX subsectors from the Sectors API.
2. For each subsector, pull a sample of its companies and each company's
   most recent daily price change.
3. Average those changes per subsector -> that's our "sector performance"
   number (the Sectors API has no single endpoint that already gives this).
4. Upsert the results into Postgres (sector_performance table).
5. Send a full daily summary to Telegram.
6. Render a static dashboard (table + chart) to ./public for GitHub Pages.

Required environment variables:
    SECTORS_API_KEY      - from https://sectors.app/api
    DATABASE_URL          - Supabase Session pooler connection string
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional:
    MAX_COMPANIES_PER_SUBSECTOR  (default 8)  - caps API calls per run
    REQUEST_DELAY_SECONDS        (default 0.3) - politeness delay between calls
    DASHBOARD_HISTORY_DAYS       (default 30)  - how many days the dashboard charts
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import requests

API_BASE = "https://api.sectors.app/v1"

SECTORS_API_KEY = os.environ.get("SECTORS_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_COMPANIES_PER_SUBSECTOR = int(os.environ.get("MAX_COMPANIES_PER_SUBSECTOR", "8"))
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.3"))
DASHBOARD_HISTORY_DAYS = int(os.environ.get("DASHBOARD_HISTORY_DAYS", "30"))

SESSION = requests.Session()
SESSION.headers.update({"Authorization": SECTORS_API_KEY or ""})


def require_env():
    missing = [
        name
        for name, val in [
            ("SECTORS_API_KEY", SECTORS_API_KEY),
            ("DATABASE_URL", DATABASE_URL),
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
        ]
        if not val
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def api_get(path, params=None):
    """GET from the Sectors API with basic error handling. Returns None on failure."""
    url = f"{API_BASE}{path}"
    try:
        resp = SESSION.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        print(f"  request error for {url}: {exc}", file=sys.stderr)
        return None
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)

    if resp.status_code == 403:
        print(f"  403 Forbidden for {url} — check SECTORS_API_KEY / subscription.", file=sys.stderr)
        return None
    if resp.status_code == 404:
        print(f"  404 Not Found for {url}", file=sys.stderr)
        return None
    if not resp.ok:
        print(f"  API error {resp.status_code} for {url}: {resp.text[:200]}", file=sys.stderr)
        return None

    try:
        return resp.json()
    except ValueError:
        print(f"  non-JSON response from {url}", file=sys.stderr)
        return None


def normalize_ticker(ticker):
    return ticker.upper().replace(".JK", "").strip()


def fetch_subsectors():
    """Returns a list of subsector slugs, e.g. ['banks', 'financing-service', ...]."""
    data = api_get("/subsectors/")
    if not data:
        return []
    slugs = []
    for item in data:
        if isinstance(item, str):
            slugs.append(item)
        elif isinstance(item, dict):
            slug = item.get("sub_sector") or item.get("sector") or item.get("slug") or item.get("name")
            if slug:
                slugs.append(slug)
    return slugs


def fetch_companies_for_subsector(subsector):
    """Returns a list of ticker symbols for a subsector, capped at MAX_COMPANIES_PER_SUBSECTOR."""
    data = api_get("/companies/", params={"sub_sector": subsector})
    if not data:
        return []
    tickers = []
    items = data if isinstance(data, list) else data.get("results", [])
    for item in items:
        if isinstance(item, str):
            tickers.append(item)
        elif isinstance(item, dict):
            sym = item.get("symbol") or item.get("ticker")
            if sym:
                tickers.append(sym)
        if len(tickers) >= MAX_COMPANIES_PER_SUBSECTOR:
            break
    return tickers


def fetch_daily_change_pct(ticker):
    """
    Returns the most recent day-over-day % change for a ticker, or None if
    unavailable. Looks back 10 calendar days to comfortably cover weekends
    and public holidays.
    """
    ticker = normalize_ticker(ticker)
    end = date.today()
    start = end - timedelta(days=10)
    data = api_get(f"/daily/{ticker}", params={"start": start.isoformat(), "end": end.isoformat()})
    if not data:
        return None

    rows = data if isinstance(data, list) else data.get("results", [])
    rows = [r for r in rows if isinstance(r, dict) and r.get("close") is not None]
    if len(rows) < 2:
        return None

    rows.sort(key=lambda r: r.get("date", ""))
    prev_close = rows[-2]["close"]
    last_close = rows[-1]["close"]
    if not prev_close:
        return None
    return (last_close - prev_close) / prev_close * 100.0


def build_sector_report():
    """
    Returns a list of dicts, one per subsector:
    {subsector, avg_change_pct, sample_size, top_gainer, top_gainer_pct, top_loser, top_loser_pct}
    """
    subsectors = fetch_subsectors()
    print(f"Found {len(subsectors)} subsectors.")
    report = []

    for subsector in subsectors:
        tickers = fetch_companies_for_subsector(subsector)
        changes = []
        for ticker in tickers:
            pct = fetch_daily_change_pct(ticker)
            if pct is not None:
                changes.append((ticker, pct))

        if not changes:
            print(f"  {subsector}: no usable price data, skipping.")
            continue

        avg_change = sum(pct for _, pct in changes) / len(changes)
        gainer = max(changes, key=lambda c: c[1])
        loser = min(changes, key=lambda c: c[1])

        report.append(
            {
                "subsector": subsector,
                "avg_change_pct": round(avg_change, 3),
                "sample_size": len(changes),
                "top_gainer": gainer[0],
                "top_gainer_pct": round(gainer[1], 3),
                "top_loser": loser[0],
                "top_loser_pct": round(loser[1], 3),
            }
        )
        print(f"  {subsector}: avg {avg_change:+.2f}% over {len(changes)} companies")

    report.sort(key=lambda r: r["avg_change_pct"], reverse=True)
    return report


def save_to_db(report, trade_date):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            for row in report:
                cur.execute(
                    """
                    INSERT INTO sector_performance
                        (trade_date, subsector, avg_change_pct, sample_size,
                         top_gainer, top_gainer_pct, top_loser, top_loser_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trade_date, subsector) DO UPDATE SET
                        avg_change_pct = EXCLUDED.avg_change_pct,
                        sample_size = EXCLUDED.sample_size,
                        top_gainer = EXCLUDED.top_gainer,
                        top_gainer_pct = EXCLUDED.top_gainer_pct,
                        top_loser = EXCLUDED.top_loser,
                        top_loser_pct = EXCLUDED.top_loser_pct
                    """,
                    (
                        trade_date,
                        row["subsector"],
                        row["avg_change_pct"],
                        row["sample_size"],
                        row["top_gainer"],
                        row["top_gainer_pct"],
                        row["top_loser"],
                        row["top_loser_pct"],
                    ),
                )
        print(f"Saved {len(report)} rows for {trade_date} to Postgres.")
    finally:
        conn.close()


def fetch_history(days):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT trade_date, subsector, avg_change_pct
                FROM sector_performance
                WHERE trade_date >= %s
                ORDER BY trade_date ASC
                """,
                (date.today() - timedelta(days=days),),
            )
            return cur.fetchall()
    finally:
        conn.close()


def send_telegram_report(report, trade_date):
    if not report:
        text = f"*IDX Sector Report — {trade_date.isoformat()}*\n\nNo data could be retrieved today."
    else:
        lines = [f"*IDX Sector Report — {trade_date.isoformat()}*", ""]
        for row in report:
            arrow = "🟢" if row["avg_change_pct"] >= 0 else "🔴"
            lines.append(
                f"{arrow} *{row['subsector']}*: {row['avg_change_pct']:+.2f}% "
                f"(n={row['sample_size']}, top: {row['top_gainer']} {row['top_gainer_pct']:+.2f}%, "
                f"worst: {row['top_loser']} {row['top_loser_pct']:+.2f}%)"
            )
        text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=20,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
    else:
        print("Telegram report sent.")


def render_dashboard(history, trade_date, output_dir="public"):
    os.makedirs(output_dir, exist_ok=True)

    dates = sorted({row["trade_date"].isoformat() for row in history})
    subsectors = sorted({row["subsector"] for row in history})

    series = {s: {d: None for d in dates} for s in subsectors}
    for row in history:
        series[row["subsector"]][row["trade_date"].isoformat()] = float(row["avg_change_pct"])

    latest_date = dates[-1] if dates else trade_date.isoformat()
    latest_rows = sorted(
        (row for row in history if row["trade_date"].isoformat() == latest_date),
        key=lambda r: r["avg_change_pct"],
        reverse=True,
    )

    chart_datasets = json.dumps(
        [
            {
                "label": s,
                "data": [series[s][d] for d in dates],
                "borderWidth": 2,
                "fill": False,
                "tension": 0.2,
            }
            for s in subsectors
        ]
    )
    chart_labels = json.dumps(dates)

    table_rows = "\n".join(
        f"""<tr>
            <td>{r['subsector']}</td>
            <td class="{'pos' if r['avg_change_pct'] >= 0 else 'neg'}">{r['avg_change_pct']:+.2f}%</td>
            <td>{r['sample_size']}</td>
        </tr>"""
        for r in latest_rows
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IDX Sector Performance Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  .updated {{ color: #888; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem; border-bottom: 1px solid #ddd; }}
  .pos {{ color: #16a34a; font-weight: 600; }}
  .neg {{ color: #dc2626; font-weight: 600; }}
  canvas {{ max-width: 100%; }}
</style>
</head>
<body>
  <h1>IDX Sector Performance</h1>
  <div class="updated">Latest trading day: {latest_date}</div>

  <canvas id="chart" height="120"></canvas>

  <h2>Latest day, ranked</h2>
  <table>
    <thead><tr><th>Subsector</th><th>Avg change</th><th>Sample size</th></tr></thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>

  <script>
    const ctx = document.getElementById('chart');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: {chart_labels},
        datasets: {chart_datasets}
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ display: {str(len(subsectors) <= 12).lower()} }} }},
        scales: {{ y: {{ title: {{ display: true, text: '% change' }} }} }}
      }}
    }});
  </script>
</body>
</html>
"""
    out_path = os.path.join(output_dir, "index.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"Dashboard written to {out_path}")


def main():
    require_env()
    trade_date = date.today()

    print(f"=== IDX sector pipeline: {trade_date.isoformat()} ===")
    report = build_sector_report()

    if report:
        save_to_db(report, trade_date)
    else:
        print("No sector data retrieved; skipping DB write.", file=sys.stderr)

    send_telegram_report(report, trade_date)

    history = fetch_history(DASHBOARD_HISTORY_DAYS)
    render_dashboard(history, trade_date)

    print("=== Done ===")


if __name__ == "__main__":
    main()
