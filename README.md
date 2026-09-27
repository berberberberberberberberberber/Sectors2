# IDX Sector Performance Bot

Daily job that tracks Indonesian Stock Exchange (IDX) sector performance,
posts a summary to Telegram, and publishes a dashboard via GitHub Pages.

## Files

| Path | Purpose |
|---|---|
| `scripts/fetch_and_report.py` | The whole pipeline: fetch → store → notify → render dashboard |
| `sql/schema.sql` | Postgres schema (`sector_performance` table), run once in Supabase's SQL Editor |
| `.github/workflows/daily.yml` | Scheduled GitHub Actions workflow |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for local runs — copy to `.env` and fill in |

## Pipeline stages

1. **Fetch subsectors** — `GET /subsectors/` from the [Sectors API](https://sectors.app/api).
2. **Sample companies per subsector** — `GET /companies/?sub_sector=...`, capped at
   `MAX_COMPANIES_PER_SUBSECTOR` (default 8) to keep API usage reasonable.
3. **Get each company's daily change** — `GET /daily/{ticker}`, comparing the two most
   recent close prices. The Sectors API has no built-in "sector performance" number, so
   this script computes one itself as the average % change across the sampled companies
   in each subsector.
4. **Store** — upserts one row per `(trade_date, subsector)` into Postgres via
   `DATABASE_URL`. Re-running for the same day overwrites that day's row.
5. **Notify** — sends the full ranked report to Telegram (`TELEGRAM_BOT_TOKEN` /
   `TELEGRAM_CHAT_ID`).
6. **Dashboard** — renders `public/index.html` (a table of the latest day + a Chart.js
   line chart of the last `DASHBOARD_HISTORY_DAYS` days), which the workflow deploys to
   GitHub Pages via `actions/upload-pages-artifact` + `actions/deploy-pages`. The file
   is never committed to the repo — it's built fresh and deployed each run.

## Schedule

Runs weekdays at **12:00 UTC / 19:00 WIB**, after IDX market close:

```yaml
cron: "0 12 * * 1-5"
```

It also supports manual runs via the "Run workflow" button (`workflow_dispatch`).

## Required secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | Where to get it |
|---|---|
| `SECTORS_API_KEY` | https://sectors.app/api |
| `DATABASE_URL` | Supabase → Connect → Session pooler connection string |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram |
| `TELEGRAM_CHAT_ID` | The chat/channel the bot should post to |

**GitHub Pages setup:** Settings → Pages → Source → **GitHub Actions**.

## Running it

**One-time setup:**
1. Create the Supabase project, run `sql/schema.sql` in its SQL Editor.
2. Add the four secrets above.
3. Enable GitHub Pages with Source = GitHub Actions.

**Manually testing:** Go to the Actions tab → "Daily IDX Sector Report" → **Run workflow**.
Confirm it goes green, the Telegram message arrives, and the Pages URL shows the
updated dashboard.

**Locally:**
```bash
cp .env.example .env   # fill in real values
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python scripts/fetch_and_report.py
```

## Monitoring

Check the Actions tab after each scheduled run. A failed run shows red immediately;
re-running is safe since the DB upsert is idempotent per day.
