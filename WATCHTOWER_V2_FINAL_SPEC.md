# WATCHTOWER 2.0 — Final Build Spec
*Opus strategic lead | Claude Code builder | Hamilton QC | April 29, 2026*
*K approved — build now*

---

## What This Is
A pure Chris Camillo social arbitrage engine. Finds cultural/consumer trends accelerating in social data BEFORE Wall Street prices them in. Maps trends to companies. Alerts K when conviction is high.

**Core question the system answers:** Does this represent a durable behavior change in a demographic with money to spend, and is the public company exposure mispriced relative to where this trend is going?

---

## Files to Build

```
watchtower/
├── watchtower_v2.py              # Main scanner — full rewrite
├── mapper.json                    # Keyword→ticker brain
├── index.html                     # Dashboard — full rebuild
├── requirements.txt               # Updated deps
├── data/
│   ├── watchtower-data.json      # Scanner output (dashboard reads this)
│   ├── watchtower-previous.json  # Yesterday's data for delta scoring
│   └── thesis/                   # Opus-generated thesis files
│       └── YYYY-MM-DD_keyword_TICKER.md
└── .github/workflows/watchtower.yml  # Keep but disable schedule — local cron takes over
```

**Rename:** `watchtower.py` → `watchtower_v1_legacy.py`

---

## watchtower_v2.py — Exact Behavior

### Run Modes
- `--mode fast` : Google Trends + Reddit only (~45 sec)
- `--mode morning` : All signals + company mapping + thesis generation
- `--mode full` : Everything + full delta comparison

### Scanner 1: Google Trends (~50 sec)

```python
# For each keyword in mapper.json:
# 1. pytrends.interest_over_time(keyword, timeframe="today 3-m")
# 2. Calculate rate-of-change: current_week / avg_of_previous_12_weeks
# 3. Check if current week is 90-day high
# 4. Score:
#    - ratio > 2.0 AND new 90-day high → 3 points
#    - ratio > 1.5 AND new 90-day high → 1 point
#    - else → 0
#
# Also run: pytrends.trending_searches() for discovery
# Pull top "rising" queries in categories: health, wellness, sports, consumer
# Cross-reference rising queries against mapper.json keywords
# NEW rising query not in mapper.json → log to data/new_trends.json for human review
#
# Rate limiting: batch 5 keywords per request, sleep 6s between batches
# Exit signal: keyword absolute score > 75 → flag as "mainstream" (exit warning)
```

### Scanner 2: Reddit Keyword Velocity (~20 sec)

```python
# Target subreddits (hardcoded list):
SUBREDDITS = [
    "biohacking", "peptides", "longevity", "supplements", "fitness",
    "pickleball", "skincareaddiction", "solotravel", "DIY", "nootropics",
    "Testosterone", "intermittentfasting", "investing", "stocks", "wallstreetbets"
]

# For each subreddit:
# 1. PRAW: fetch last 100 posts (hot + new, last 7 days)
# 2. For each mapper.json keyword: count occurrences in title + selftext
# 3. Load 30-day rolling avg from watchtower-previous.json
# 4. Score:
#    - this_week_count > (avg_30d * 2.0) → 2 points (reduced from 3 per Opus)
#    - this_week_count > (avg_30d * 1.5) → 1 point
#
# NOVELTY FILTER (Opus requirement):
# Track keyword migration ACROSS subreddits
# If keyword appears in 3+ subreddits it doesn't normally appear in → +1 bonus
# (e.g., "peptides" in r/fitness + r/investing + r/solotravel = real crossover signal)
#
# Update rolling avg in output data
```

### Scanner 3: Amazon BSR (~10 sec)

```python
# Hardcoded category URLs (8-10 Amazon bestseller pages):
# /Best-Sellers-Health-Personal-Care/
# /Best-Sellers-Sports-Fitness/
# /Best-Sellers-Beauty-Personal-Care/
# /Best-Sellers-Grocery-Gourmet-Food/ (functional beverages)
# etc.
#
# Also scrape: amazon.com/gp/movers-and-shakers/ by category
# (24-hour BSR movers — pre-filtered by Amazon, highly signal-dense)
#
# For each product found:
# 1. Compare BSR to stored value in watchtower-previous.json
# 2. If climbed from >500 to <200 in 30 days AND maps to a keyword → 3 points
# 3. Amazon Movers & Shakers appearance → 2 points
#
# FRAGILE: wrap in try/except, log errors, return 0 signals on failure
# System continues running if this scanner fails
```

### Scoring Engine

```python
# For each keyword that fired at least one signal:
# Sum raw points from all 3 scanners
#
# Bonuses:
# +3 if 2+ Tier A scanners fired on same keyword (multi-platform confirmation)
# +2 if keyword has zero financial media coverage
#     (proxy: Google Trends absolute score < 30 for keyword + ticker combo)
#
# Penalties:
# -3 if Google Trends absolute score > 75 (mainstream = exit warning)
# -2 if keyword appearing heavily in r/investing or r/wallstreetbets
#
# Time decay:
# Score × decay_factor where decay = 1.0 for signals <48h old
# Halve score for signals >7 days old that haven't sustained
# (load signal_first_seen from watchtower-previous.json)
#
# Tiers:
# 8+ → Tier 1 (generate Opus thesis + Discord alert)
# 5-7 → Tier 2 (include in daily data)
# 3-4 → Tier 3 (watch list)
# <3  → noise (log but don't include in output)
#
# PERSISTENCE: Tier 1 signals carry forward day-to-day
# Track: signal_first_seen, days_active, peak_score, current_score
```

### Company Mapping

```python
# For each trend scoring 3+:
# 1. Load mapper.json
# 2. Find all matching keywords (exact + fuzzy match against keyword variants)
# 3. For each ticker in direct[] and picks_and_shovels[]:
#    - yfinance: current price, 1d change, 30d change, 90d change, market cap
#    - Validate ticker is real (yfinance returns data) — log invalid tickers
# 4. Build company_exposure object for output
```

### Thesis Generation (Tier 1 only — Opus writes it)

```python
# For each NEW Tier 1 hit (not seen in watchtower-previous.json):
# 1. Build signal_summary dict with all scanner data
# 2. Call Anthropic API with claude-opus-4-6 model
# 3. System prompt: "You are an investment thesis writer using Chris Camillo's
#    social arbitrage methodology. Write a sharp, actionable investment thesis."
# 4. User prompt: pass full signal_summary as structured data
# 5. Requested output format:
#    - TREND: name
#    - SCORE: X/10 | TIER 1 | Day N of signal
#    - WHAT'S HAPPENING: plain English, 2-3 sentences
#    - SIGNALS FIRING: bullet list of what fired and at what level
#    - PLAYS: direct tickers with 1-line thesis each, picks & shovels with thesis
#    - WALL STREET AWARENESS: LOW/MEDIUM/HIGH with reasoning
#    - ESTIMATED WINDOW: X months before mainstream
#    - CAMILLO FILTER: checklist (durable change / spending demographic / under radar / company exposure)
# 6. Save to data/thesis/YYYY-MM-DD_keyword_TICKER.md
# 7. Include thesis text in watchtower-data.json under the trend entry
#
# ANTHROPIC_API_KEY: load from .env
# Model: claude-opus-4-6 (NOT claude-sonnet, NOT claude-haiku — K specifically requested Opus)
# Cost: ~$0.10-0.20 per thesis, fires only on NEW Tier 1 hits (~1-3/week)
```

### Discord Alert (Tier 1 only)

```python
# POST to DISCORD_WEBHOOK_URL from .env
# Format:
# 🗼 WATCHTOWER TIER 1
# Trend: [name] | Score: [X]/10 | Day [N]
# 📈 Direct: [TICKER] $[price] ([30d change]%)
# ⚒️ Picks & shovels: [TICKER] $[price]
# 👁️ Wall St awareness: [LOW/MEDIUM/HIGH]
# 🔗 [dashboard URL]
# Full thesis attached as text snippet (first 500 chars)
```

### Output: watchtower-data.json

```json
{
  "generated_at": "ISO timestamp",
  "mode": "morning",
  "market_status": "open/closed",
  "active_signals": [
    {
      "keyword": "peptides",
      "display_name": "Peptide Self-Administration",
      "tier": 1,
      "score": 9,
      "score_breakdown": {"google_trends": 3, "reddit": 2, "amazon_bsr": 3, "bonuses": 2, "penalties": -1},
      "signals_firing": ["google_trends", "reddit", "amazon_bsr"],
      "signal_first_seen": "2026-04-29",
      "days_active": 1,
      "peak_score": 9,
      "google_trends": {"ratio": 2.61, "current": 47, "avg_12w": 18, "is_90d_high": true, "absolute_score": 47},
      "reddit": {"subreddits_firing": ["biohacking", "fitness"], "total_mentions_7d": 43, "avg_30d": 14, "novelty_bonus": true},
      "amazon_bsr": {"product": "BPC-157 capsules", "bsr_now": 142, "bsr_30d_ago": 873},
      "wall_street_awareness": "LOW",
      "exit_warning": false,
      "companies": {
        "direct": [
          {"ticker": "HIMS", "price": 30.56, "change_30d": -2.1, "thesis": "..."},
          {"ticker": "LLY", "price": 812.40, "change_30d": 5.2, "thesis": "..."}
        ],
        "picks_and_shovels": [
          {"ticker": "BCHMY", "price": 9.14, "change_30d": 1.3, "thesis": "..."}
        ]
      },
      "thesis": "Full Opus-generated thesis text here...",
      "thesis_file": "data/thesis/2026-04-29_peptides_BCHMY.md"
    }
  ],
  "new_trends_discovered": ["methylene blue", "oral GLP-1"],
  "exit_warnings": [],
  "run_stats": {"duration_sec": 87, "keywords_scanned": 42, "tier1_count": 1, "tier2_count": 2}
}
```

---

## mapper.json — Full Starting Seed

```json
{
  "peptides": {
    "keywords": ["peptides", "peptide", "BPC-157", "BPC157", "TB-500", "TB500", "GLP-1", "GLP1", "semaglutide", "tirzepatide", "peptide therapy"],
    "direct": [
      {"ticker": "HIMS", "thesis": "Telehealth platform dispensing compounded GLP-1s. Novo Nordisk partnership is key catalyst."},
      {"ticker": "NVO", "thesis": "Ozempic/Wegovy maker. Category leader, may be partially priced."},
      {"ticker": "LLY", "thesis": "Mounjaro/Zepbound. Dominant GLP-1 pipeline."},
      {"ticker": "AMGN", "thesis": "MariTide obesity drug in late trials."}
    ],
    "picks_and_shovels": [
      {"ticker": "BCHMY", "thesis": "Bachem. Peptide API manufacturer. Wins regardless of which drug or brand wins. ~$3B cap, minimal analyst coverage on this angle."},
      {"ticker": "BRKR", "thesis": "Bruker. Analytical instruments used in peptide R&D and quality testing."}
    ],
    "category": "health",
    "description": "Peptide self-administration trend — GLP-1 weight loss, BPC-157 healing, TB-500 recovery. Moving from bodybuilding niche into mainstream wellness."
  },
  "longevity": {
    "keywords": ["longevity", "NMN", "NAD+", "rapamycin", "senolytic", "anti-aging supplements", "lifespan", "healthspan", "methylene blue"],
    "direct": [
      {"ticker": "UNITY", "thesis": "Unity Biotechnology. Senolytic pipeline. Micro-cap, speculative, real science."}
    ],
    "picks_and_shovels": [
      {"ticker": "GLBC", "thesis": "Globe Life — verify ticker validity before use."}
    ],
    "category": "health",
    "description": "Anti-aging and longevity supplement/therapy trend. NMN, NAD+ precursors, rapamycin interest growing in biohacking communities."
  },
  "pickleball": {
    "keywords": ["pickleball", "pickleball court", "pickleball paddle", "padel", "paddle sport"],
    "direct": [
      {"ticker": "LTH", "thesis": "Life Time Health. Building dedicated pickleball courts at locations nationwide."}
    ],
    "picks_and_shovels": [
      {"ticker": "NKE", "thesis": "Nike. Court shoes and apparel expanding into pickleball/padel."},
      {"ticker": "UA", "thesis": "Under Armour. Racquet sports apparel expansion."}
    ],
    "category": "lifestyle",
    "description": "Fastest-growing sport in America. Court construction boom. Equipment market fragmented — no dominant pure-play public company yet."
  },
  "water_quality": {
    "keywords": ["PFAS filter", "microplastics", "water filter", "water quality", "forever chemicals", "reverse osmosis home"],
    "direct": [
      {"ticker": "PNR", "thesis": "Pentair. Water treatment and filtration systems. Direct beneficiary of PFAS anxiety."},
      {"ticker": "AOS", "thesis": "A.O. Smith. Water heaters and filtration. Consumer home water quality play."}
    ],
    "picks_and_shovels": [
      {"ticker": "WTS", "thesis": "Watts Water Technologies. Flow control and water quality infrastructure."}
    ],
    "category": "home",
    "description": "PFAS/microplastics anxiety driving home water filtration upgrades. Regulatory tailwinds adding urgency."
  },
  "red_light_therapy": {
    "keywords": ["red light therapy", "photobiomodulation", "infrared therapy", "red light panel"],
    "direct": [],
    "picks_and_shovels": [],
    "category": "health",
    "description": "At-home red light therapy devices exploding on Amazon. No clear pure-play public company yet — monitor for IPOs."
  },
  "creatine_wellness": {
    "keywords": ["creatine", "creatine monohydrate", "creatine wellness", "creatine women", "creatine brain"],
    "direct": [],
    "picks_and_shovels": [
      {"ticker": "CELH", "thesis": "Celsius Holdings. Functional supplement brand expanding into creatine-adjacent wellness."}
    ],
    "category": "health",
    "description": "Creatine crossing from gym culture into mainstream wellness — brain health, women's fitness, aging. Massive category expansion."
  },
  "functional_beverages": {
    "keywords": ["adaptogen drink", "nootropic drink", "electrolyte drink", "mushroom coffee", "functional beverage", "LMNT", "athletic greens"],
    "direct": [
      {"ticker": "MNST", "thesis": "Monster Beverage. Functional energy category leader. Expanding into wellness drinks."},
      {"ticker": "CELH", "thesis": "Celsius Holdings. Fitness/wellness functional energy drink. Strong Gen Z adoption."}
    ],
    "picks_and_shovels": [],
    "category": "consumer",
    "description": "Adaptogen, nootropic, and electrolyte drink explosion. Moving from niche health stores to mainstream grocery."
  },
  "sleep_optimization": {
    "keywords": ["sleep optimization", "sleep tracking", "magnesium glycinate", "sleep supplement", "circadian rhythm", "sleep quality"],
    "direct": [],
    "picks_and_shovels": [
      {"ticker": "AAPL", "thesis": "Apple Watch sleep tracking. Sleep feature driving device stickiness — not a pure play but beneficiary."}
    ],
    "category": "health",
    "description": "Sleep optimization moving from biohackers to mainstream. Wearables, supplements, and environment products all growing."
  },
  "home_energy": {
    "keywords": ["home battery", "residential solar", "EV charger home", "Powerwall", "home energy storage", "grid outage prep"],
    "direct": [
      {"ticker": "ENPH", "thesis": "Enphase Energy. Residential solar microinverters and battery storage. Market leader."},
      {"ticker": "SEDG", "thesis": "SolarEdge. Residential solar optimizer. Competing with Enphase."},
      {"ticker": "RUN", "thesis": "Sunrun. Residential solar installer. Direct consumer play."}
    ],
    "picks_and_shovels": [
      {"ticker": "ALB", "thesis": "Albemarle. Lithium producer. Wins on battery storage growth regardless of brand."}
    ],
    "category": "home",
    "description": "Grid anxiety and energy independence driving residential battery + solar adoption. Outage prep accelerating post-storm events."
  },
  "oral_health": {
    "keywords": ["hydroxyapatite toothpaste", "water flosser", "oral health", "remineralizing toothpaste", "tongue scraper"],
    "direct": [
      {"ticker": "CHD", "thesis": "Church & Dwight. Arm & Hammer and Waterpik brands. Direct oral health beneficiary."}
    ],
    "picks_and_shovels": [],
    "category": "consumer",
    "description": "Oral health reinvention — hydroxyapatite replacing fluoride, water flossers going mainstream, holistic oral care trend."
  },
  "cold_plunge": {
    "keywords": ["cold plunge", "cold water immersion", "ice bath", "cold therapy", "cryotherapy home"],
    "direct": [],
    "picks_and_shovels": [],
    "category": "lifestyle",
    "description": "Cold plunge going from elite athlete recovery to mainstream wellness. Home cold plunge unit market emerging."
  },
  "continuous_glucose_monitor": {
    "keywords": ["CGM", "continuous glucose monitor", "glucose tracking", "metabolic health", "Levels Health", "Dexcom non-diabetic"],
    "direct": [
      {"ticker": "DXCM", "thesis": "Dexcom. CGM market leader expanding beyond diabetics into wellness monitoring."},
      {"ticker": "ABBV", "thesis": "AbbVie (via FreeStyle Libre). CGM device for metabolic wellness monitoring."}
    ],
    "picks_and_shovels": [],
    "category": "health",
    "description": "Continuous glucose monitors crossing from diabetes management to mainstream metabolic health tracking."
  }
}
```

---

## index.html — Dashboard Design

Single static HTML file. No framework. GitHub Pages serves it. Reads `data/watchtower-data.json` via fetch.

### Layout

**Header:** `🗼 WATCHTOWER 2.0 | Last updated: [timestamp] | [N] active signals | [X] Tier 1`

**Section 1 — ACTIVE CONVICTION (Tier 1)**
Red/orange border cards. Prominent. This section is the whole point.
Each card:
- Trend name + category emoji (🧬 health / 🏃 lifestyle / 🏠 home / 🥤 consumer)
- Score badge (e.g., "9/10") + "Day 12 of signal" + trend direction arrow
- Signal icons: 📈 Google Trends | 💬 Reddit | 📦 Amazon BSR (filled = firing, grey = not)
- Direct plays: ticker, price, 30d change, 1-line thesis
- Picks & shovels: ticker, price, 30d change, 1-line thesis
- Wall Street awareness bar: 🟢 LOW / 🟡 MEDIUM / 🔴 HIGH (exit)
- [View Full Thesis] button — expands Opus thesis inline
- **PERSISTENT**: stays on board until score drops below Tier 1 or exit warning fires

**Section 2 — WATCHING (Tier 2)**
Condensed cards, less prominent. Same structure, smaller.

**Section 3 — RADAR (Tier 3)**
Compact table: keyword | score | signals firing | top ticker

**Section 4 — NEW TRENDS DISCOVERED**
Keywords the discovery engine surfaced that aren't in mapper.json yet.
Simple list: "methylene blue | Google Trends rising +340% | Not yet mapped"
K reviews these and adds to mapper.json manually.

**Section 5 — EXIT WARNINGS**
Any active signal where Google Trends absolute score > 75 OR news coverage accelerating.
Red banner: "PEPTIDES approaching mainstream — consider exit timeline"

---

## Cron Setup (Mac mini — NOT GitHub Actions)

Add to crontab:
```
30 9  * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode morning >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 10 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 11 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 12 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 13 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 14 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
30 15 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode fast >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
15 16 * * 1-5   /usr/bin/python3 /Users/kurtafarmer/Documents/watchtower/watchtower_v2.py --mode full >> /Users/kurtafarmer/Documents/watchtower/logs/cron.log 2>&1
```

GitHub Actions workflow: keep the file but comment out the schedule triggers. Use workflow_dispatch only (manual trigger as backup).

---

## .env Variables Required

```
DISCORD_WEBHOOK_URL=     # K to provide — channel for Tier 1 alerts
REDDIT_CLIENT_ID=        # Already exists
REDDIT_CLIENT_SECRET=    # Already exists
REDDIT_USER_AGENT=       # Already exists
ANTHROPIC_API_KEY=       # For Opus thesis generation — load from existing OpenClaw secrets
```

---

## requirements.txt

```
pytrends>=4.9.0
praw>=7.7.0
beautifulsoup4>=4.12.0
requests>=2.31.0
yfinance>=0.2.36
python-dotenv>=1.0.0
anthropic>=0.25.0
lxml>=4.9.0
```

---

## Build Instructions for Claude Code

1. Read this spec completely before writing any code
2. Rename `watchtower.py` → `watchtower_v1_legacy.py`
3. Build `mapper.json` exactly as specified above
4. Build `watchtower_v2.py` implementing all scanners, scoring, thesis generation, Discord webhook, and git commit logic
5. Build `index.html` — full rebuild, trend-focused, persistent conviction list
6. Update `requirements.txt`
7. Update `.github/workflows/watchtower.yml` — comment out schedule triggers, keep workflow_dispatch
8. Create `logs/` directory if not exists
9. Test run: `python3 watchtower_v2.py --mode fast` — must complete without errors
10. If ANTHROPIC_API_KEY not in .env, thesis generation should gracefully skip (log warning, don't crash)
11. If DISCORD_WEBHOOK_URL not in .env, webhook should gracefully skip (log warning, don't crash)
12. Git commit all new files

---

## Success Criteria

- `python3 watchtower_v2.py --mode fast` runs clean in <90 seconds
- `python3 watchtower_v2.py --mode morning` runs clean in <3 minutes
- Dashboard loads and renders signal cards from data.json
- Tier 1 signal → Opus thesis written → Discord webhook fires → dashboard shows persistent card
- No silent failures — every error logged, system continues running

---

*Opus strategic lead. Claude Code builds. Hamilton QCs. K approves.*
*Discord webhook URL pending from K — system builds without it, gracefully skips until provided.*
