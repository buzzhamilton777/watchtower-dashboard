# WATCHTOWER v2 — Build Spec
*Authored by Hamilton + Opus strategic session | April 29, 2026*
*Approved by K | Status: BUILD*

---

## Mission
A pure Chris Camillo social arbitrage engine. Find cultural/consumer trends accelerating in social data BEFORE Wall Street prices them in. Map trends to public companies. Alert K when conviction is high and institutional coverage is zero.

**Core belief:** The edge is the interpretation gap — a signal that's real but that Wall Street's models can't yet price.

---

## What We're Replacing
The old WATCHTOWER tracked congressional trades, 13F whales, ARK, Form 4 insiders. This is following institutions AFTER they've positioned. That's the opposite of Camillo's edge. All of that gets stripped.

---

## Signal Architecture

### Tier A — High Trust Signals (leading indicators)

**1. Google Trends Rate-of-Change**
- Tool: `pytrends`
- Metric: week-over-week delta, NOT absolute score
- Signal trigger: keyword crosses a 90-day high on weekly value
- A keyword at 5 going to 25 in 60 days = far more interesting than one sitting at 70
- Pull top "rising" queries in relevant categories daily

**2. Reddit Keyword Velocity**
- Tool: `PRAW`
- NOT subscriber count — post frequency + comment velocity on SPECIFIC keywords inside niche communities
- Target subreddits: r/peptides, r/biohacking, r/longevity, r/supplements, r/fitness, r/pickleball, r/skincareaddiction, r/solotravel, r/DIY, r/homebrewing, r/nootropics, r/Testosterone, r/intermittentfasting, r/veganfitness, r/functionalbeverages
- Signal trigger: keyword appears >2x its 30-day average frequency in a 7-day window
- Pull new posts + comments, extract keywords, track frequency over time

**3. Amazon BSR Delta**
- Tool: `requests` + `BeautifulSoup`
- Track product categories, not individual products
- Signal trigger: product climbing from >500 to <200 BSR in 30 days with no media coverage
- Categories: supplements, fitness equipment, wellness devices, beverages, skincare, sports
- This is REAL consumer dollars moving, not just talk

### Tier B — Confirming Signals (not leading)
- **News keyword frequency** (NewsAPI or Google News RSS) — use as EXIT signal. Rising = window closing.
- **Subreddit growth rate** — useful for identifying emerging communities, lags post velocity

### Explicitly Excluded
- Congressional trades (institutional, lagging)
- 13F filings (quarterly lag, already priced)
- ARK holdings (institutional)
- Form 4 insiders (event-driven, not trend-driven)
- TikTok virality alone (fads, not investments)
- Twitter/X volume (noisy, manipulated)

---

## Company Mapping Module

### The Keyword→Ticker Map (`mapper.json`)
A curated JSON file mapping trend keywords to tickers. Human-maintained, easy to update. This is the secret sauce.

Structure:
```json
{
  "peptides": {
    "direct": ["HIMS", "AMGN", "LLY", "NVO"],
    "picks_and_shovels": ["BCHMY", "BRKR"],
    "description": "Peptide self-administration trend — GLP-1, BPC-157, TB-500"
  },
  "longevity": {
    "direct": ["UNITY", "NVTA"],
    "picks_and_shovels": ["GLBC", "CHROMAD"],
    "description": "Anti-aging/senolytic supplements and therapies"
  },
  "pickleball": {
    "direct": ["ACSH", "LTH"],
    "picks_and_shovels": ["NKE", "UA"],
    "description": "Pickleball equipment, court construction, apparel"
  },
  "water_quality": {
    "direct": ["PNR", "AOS"],
    "picks_and_shovels": ["WTS", "POWI"],
    "description": "PFAS/microplastics anxiety driving home filtration"
  },
  "red_light_therapy": {
    "direct": ["JOOV"],
    "picks_and_shovels": [],
    "description": "At-home red light therapy devices"
  },
  "creatine_wellness": {
    "direct": [],
    "picks_and_shovels": ["GLB", "BRID"],
    "description": "Creatine crossing from gym to mainstream wellness"
  },
  "functional_beverages": {
    "direct": ["MNST", "CELH"],
    "picks_and_shovels": ["APD", "BRID"],
    "description": "Adaptogen, nootropic, electrolyte drink explosion"
  },
  "sleep_optimization": {
    "direct": ["AAPL", "FITB"],
    "picks_and_shovels": [],
    "description": "Sleep tracking, magnesium, optimization products"
  },
  "home_energy": {
    "direct": ["ENPH", "SEDG", "RUN"],
    "picks_and_shovels": ["ALB", "LAC"],
    "description": "Residential battery/solar anxiety driven by grid concerns"
  }
}
```

### Mapping Logic
- When trend detected → look up keywords in mapper.json → pull both direct + picks_and_shovels tickers
- For each ticker: fetch current price, 30d/90d performance, market cap
- Market cap filter: prefer <$10B (not yet on heavy institutional radar) but don't exclude large caps with clear exposure
- "Wall Street awareness" check: search NewsAPI for ticker + trend keyword combo. Low coverage = green. Rising coverage = yellow/red.

---

## Scoring Framework

| Signal | Points |
|--------|--------|
| Google Trends breakout (new 90-day weekly high) | 3 |
| Reddit keyword velocity spike (>2x 30-day avg) | 3 |
| Amazon BSR delta (climbing >50% in 30d, rank <500) | 2 |
| Multi-platform confirmation (2+ Tier A signals on same trend) | +2 bonus |
| Zero news coverage on trend+ticker combo | +1 bonus |
| News coverage rising (mainstream awareness) | -2 (exit flag) |

**Alert Thresholds:**
- **8+ pts** = Tier 1 → Hamilton pings K on Discord immediately. Real money conversation.
- **5-7 pts** = Tier 2 → Daily brief. Watch closely.
- **3-4 pts** = Tier 3 → Watch list. No action yet.
- **<3 pts** = Noise. Log but don't display.

---

## Auto-Generated Thesis (Key Feature)
For every Tier 1 hit, generate a `thesis.md` snippet:

```
TREND: [Trend Name]
SCORE: 9/10 | TIER 1

WHAT'S HAPPENING:
[Trend description in plain English]

SIGNALS FIRING:
- Google Trends: [keyword] up X% in 30 days, new 90d high
- Reddit: [keyword] appearing 3x avg frequency in r/[subreddit] this week
- Amazon BSR: [product category] climbed from #X to #Y in 30 days

COMPANIES:
Direct play: [TICKER] — [why they benefit]
Picks & shovels: [TICKER] — [why they win regardless of who wins]

WALL STREET AWARENESS: LOW (X news mentions in 30d)
ESTIMATED WINDOW: 6-12 months before mainstream

CAMILLO FILTER:
☑ Durable behavior change (not a fad)
☑ Demographic with spending power
☑ Under Wall Street radar
☑ Public company exposure exists
```

---

## Architecture

### File Structure
```
watchtower/
├── watchtower_v2.py       # Main scanner (replaces watchtower.py)
├── mapper.json            # Keyword→ticker map (human-maintained)
├── requirements.txt       # Updated dependencies
├── index.html             # Dashboard (full rebuild)
├── data/
│   ├── watchtower-data.json    # Current signal output
│   ├── watchtower-previous.json # Yesterday's data for delta
│   └── thesis/            # Auto-generated thesis files
└── .github/workflows/
    └── watchtower.yml     # Existing cron (update script name)
```

### Data Flow
1. GitHub Actions cron fires (morning + hourly market hours + close)
2. `watchtower_v2.py` runs → scans Google Trends, Reddit, Amazon BSR
3. Scores each trend → maps to companies → generates thesis for Tier 1
4. Writes `data/watchtower-data.json`
5. If Tier 1 hit → fires Discord webhook with thesis summary
6. Git commit → GitHub Pages updates dashboard

### Cron Schedule (unchanged)
- Morning: 9:30 AM ET (14:30 UTC) weekdays
- Fast: hourly 10:30 AM–4 PM ET weekdays
- Full: 4:15 PM ET (21:15 UTC) weekdays

---

## Dashboard Redesign

**Layout:**
- Header: WATCHTOWER v2 | Last updated: [timestamp] | [X] trends tracked | [Y] Tier 1 alerts
- **Tier 1 section** (prominent, red border): Cards with trend name, score, companies, thesis preview
- **Tier 2 section**: Condensed cards
- **Watch list**: Compact table
- **Trend detail**: Expandable — shows all signals, full thesis, price chart link

**Each card shows:**
- Trend name + emoji category icon
- Score badge (color coded)
- Signals firing (Google Trends / Reddit / Amazon BSR icons)
- Direct play ticker(s) with current price
- Picks & shovels ticker(s) with current price
- Wall Street awareness meter (green=low, yellow=medium, red=high/exit)
- "Camillo window" estimate

---

## Seed Keywords to Track at Launch

**Health & Wellness:**
peptides, BPC-157, TB-500, semaglutide, tirzepatide, longevity supplements, NMN, NAD+, rapamycin, methylene blue, red light therapy, creatine wellness, magnesium glycinate, sleep optimization

**Consumer Lifestyle:**
pickleball, padel, gravel cycling, cold plunge, sauna, breathwork, functional beverages, adaptogen drinks, electrolytes, hydroxyapatite toothpaste

**Home & Environment:**
PFAS filter, microplastics, water quality, home battery, solar panels, EV charging home

**Tech & Productivity:**
AI wearable, ambient computing, spatial audio, smart home health

---

## Success Criteria
- Surfaces a Tier 1 signal that leads to a profitable trade within 90 days of launch
- K can look at the dashboard and immediately understand what to consider buying and why
- Zero false positives from institutional/lagging data
- Thesis quality: K should be able to send the thesis to a financial advisor and have it make sense

---

## What Claude Code Needs to Build
1. `watchtower_v2.py` — full rewrite from scratch
   - Google Trends scanner (pytrends, rate-of-change logic)
   - Reddit scanner (PRAW, keyword velocity across target subreddits)
   - Amazon BSR scanner (requests + BS4, category tracking)
   - Company mapper (reads mapper.json, fetches prices via yfinance)
   - Scoring engine
   - Thesis generator (auto-generates text for Tier 1 hits)
   - Discord webhook for Tier 1 alerts
   - Outputs data/watchtower-data.json
2. `mapper.json` — pre-seeded with all keywords above
3. `index.html` — full dashboard rebuild (clean design, trend-focused)
4. Update `.github/workflows/watchtower.yml` — point to watchtower_v2.py
5. Update `requirements.txt`

**Keep:** `.env`, `.env.example`, `data/` folder structure, GitHub Actions secrets setup
**Replace:** `watchtower.py` → `watchtower_v2.py`, `index.html`

---

## Environment Variables Needed
```
DISCORD_WEBHOOK_URL=  # existing
REDDIT_CLIENT_ID=     # existing
REDDIT_CLIENT_SECRET= # existing
REDDIT_USER_AGENT=    # existing
NEWSAPI_KEY=          # NEW - for wall street awareness check (newsapi.org, free tier)
```

K needs to create a free NewsAPI key at newsapi.org if not already done.

---

*This spec is the single source of truth. Claude Code builds from this. Hamilton QCs. K approves.*
