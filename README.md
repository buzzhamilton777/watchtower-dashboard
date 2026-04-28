# WATCHTOWER

Investment signal intelligence. Aggregates congressional trades, 13F whale filings, 13D activist stakes, ARK ETF moves, Reddit buzz, and Google Trends into a daily scored ticker list.

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/watchtower.git
cd watchtower
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your credentials
```

### 2. Run locally

```bash
python watchtower.py
# output → data/watchtower-data.json
# logs   → logs/watchtower.log
```

Open `index.html` in your browser to see the dashboard.

### 3. GitHub Actions (daily auto-run)

Add these secrets in **Settings → Secrets and variables → Actions**:

| Secret | Where to get it |
|--------|----------------|
| `DISCORD_WEBHOOK_URL` | Discord server → Settings → Integrations → Webhooks |
| `REDDIT_CLIENT_ID` | reddit.com/prefs/apps → create "script" app |
| `REDDIT_CLIENT_SECRET` | same as above |
| `REDDIT_USER_AGENT` | e.g. `watchtower:v1.0 (by /u/yourname)` |

The workflow runs daily at **8:00 PM CDT** and commits updated data automatically.

### 4. GitHub Pages (live dashboard)

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch** → branch: `main`, folder: `/` (root)
3. Visit `https://YOUR_USERNAME.github.io/watchtower/`

## Scoring

| Signal | Points |
|--------|--------|
| 13D Activist new stake | 4 |
| 13F whale new/+20% position | 3 |
| ARK new position | 2 |
| Congressional buy | 1 |
| ARK increase >10% | 1 |
| Reddit spike (>50 mentions, 2x baseline) | 1 |
| Google Trends +20% WoW | 1 |

**Tiers:** 8+ = Tier 1 (Max Conviction) · 5-7 = Tier 2 · 3-4 = Tier 3 · 1-2 = Tier 4

Discord alerts fire automatically for any Tier 1 tickers.
