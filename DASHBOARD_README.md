# WATCHTOWER 2.0 - Investment Dashboard

**Real-time investment signals from earnings acceleration, hiring surges, and volume momentum**

## 🎯 Overview

The Watchtower 2.0 Dashboard is the visual hub for K's investment decision-making. It displays high-conviction investment picks with a confidence score, signal breakdown, and entry/exit targets.

**Live Dashboard:** https://buzzhamilton777.github.io/watchtower-dashboard/

## 🚀 Key Features

### 1. **Investment-Focused Layout**
- **HIGH-Conviction Picks** (≥75% confidence) - Primary focus
- **MEDIUM-Conviction Picks** (55-74% confidence) - Selective entry
- **WATCH List** (<55% confidence) - Monitor only

### 2. **Real-Time Auto-Refresh**
- Fetches latest signals every 5 minutes
- No manual page refresh needed
- Loading indicator while fetching
- Fallback to local test data for development

### 3. **Signal Breakdown**
Each pick shows three weighted signals:
- **📈 Earnings Acceleration** (40% weight) - EPS beats + forward guidance
- **👥 Hiring Surge** (35% weight) - Job posting velocity growth
- **📊 Volume Momentum** (25% weight) - Volume >1.5x baseline + positive price

Composite confidence formula:
```
Confidence = (Earnings × 0.40) + (Hiring × 0.35) + (Volume × 0.25)
```

### 4. **Entry & Exit Targets**
- **Entry Price** - Current price (shown in green)
- **Target Price** - Exit target with expected upside (shown in yellow)
- Visual confidence badge showing conviction level

### 5. **Live Signals Only**
- Shows signals from **FRI 5/15 onwards**
- Filters out historical clutter
- Timestamp: "Last updated: HH:MM EST"
- Next run indicator

### 6. **Professional Design**
- Dark theme (investment-grade professional look)
- Mobile-responsive (works on all devices)
- Color-coded by conviction level
- Clean typography optimized for scanning

## 📊 Dashboard Structure

### Header Stats
```
├─ HIGH-Conviction Picks: [count]
├─ MEDIUM-Conviction Picks: [count]
├─ Total Analyzed: [count]
├─ System Status: LIVE
└─ Next Run: [date/time]
```

### Signal Cards (3 Layouts)

#### 1. HIGH-Conviction Card (≥75%)
```
┌─────────────────────────────────┐
│ NVDA         ┐                  │
│ NVIDIA       │ 89% ●            │
│              │                  │
├──────────────────────────────────┤
│ 📍 Entry Price        $135.42    │
│ 🎯 Target Price       $182.82    │
├──────────────────────────────────┤
│ Signal Breakdown                 │
│ 📈 Earnings (40%)  [████████░] 92 │
│ 👥 Hiring (35%)    [███████░░] 85 │
│ 📊 Volume (25%)    [████████░] 88 │
└─────────────────────────────────┘
```

#### 2. MEDIUM-Conviction Card (55-74%)
- Same structure, different color scheme
- Yellow accent for caution

#### 3. WATCH List Card (<55%)
- Blue accent for monitoring
- Lower confidence but tracked

## 🔄 Data Flow

### From Watchtower Engine to Dashboard

1. **Mac Mini Executor** (runs TUE/WED/THU @ 5:30 PM EST)
   - Runs investment analysis
   - Generates briefing with picks

2. **Investment Engine**
   - Analyzes 10 stocks (AAPL, MSFT, NVDA, TSLA, AMZN, GOOGL, META, NFLX, AVGO, CRM)
   - Calculates E/H/V scores
   - Filters by confidence threshold

3. **Thesis Generator**
   - Creates detailed theses
   - Calculates entry/exit targets
   - Structures pick JSON

4. **GitHub Publisher**
   - Writes to `data/latest.json` in watchtower-dashboard repo
   - Commits & pushes changes
   - Triggers GitHub Pages build

5. **Dashboard**
   - Fetches from GitHub raw content
   - Falls back to local data if needed
   - Auto-refreshes every 5 minutes
   - Displays live signals

## 📱 Responsive Behavior

### Desktop (>768px)
- 3-column grid layout for signal cards
- Full stats bar visible
- Optimal for scanning multiple picks

### Mobile (<768px)
- 1-column layout
- Stats stack vertically
- Touch-friendly confidence badges
- Full readability maintained

## 🔧 Configuration

Dashboard settings in `index.html`:

```javascript
const DASHBOARD_CONFIG = {
    DATA_URL: 'https://raw.githubusercontent.com/buzzhamilton777/watchtower-2.0/main/data/latest_briefing.json',
    FALLBACK_URL: './data/latest.json',
    REFRESH_INTERVAL: 5 * 60 * 1000,  // 5 minutes
    CUTOFF_DATE: new Date('2026-05-15')  // Only live signals
};
```

### Test Schedule (May 2026)
- **FRI 5/15 @ 5:30 PM EST** - Initial test run
- **MON 5/19 @ 5:30 PM EST** - Follow-up test
- **TUE/WED/THU ongoing** - Regular schedule

## 📊 JSON Data Format

Expected briefing structure from `/data/latest.json`:

```json
{
  "timestamp": "2026-05-15T21:30:00",
  "briefing_id": "20260515_213000",
  "picks": [
    {
      "ticker": "NVDA",
      "company_name": "NVIDIA",
      "confidence": 0.89,
      "confidence_score": 0.89,
      "entry_price": 135.42,
      "current_price": 135.42,
      "target_price": 182.82,
      "exit_targets": {
        "conservative": 155.73,
        "primary": 182.82
      },
      "signal_breakdown": {
        "earnings_acceleration": 0.92,
        "hiring_surge": 0.85,
        "volume_momentum": 0.88
      },
      "signals": {
        "earnings": 0.92,
        "hiring": 0.85,
        "volume": 0.88
      },
      "thesis": "...",
      "conviction_level": "HIGH"
    }
  ],
  "analysis_summary": {
    "high_conviction_total": 3,
    "medium_conviction_total": 2,
    "total_analyzed": 10
  }
}
```

## 🎯 Usage for K

### Step 1: Check Dashboard
- Open https://buzzhamilton777.github.io/watchtower-dashboard/
- See HIGH-conviction picks at a glance
- Review signal breakdown for each pick

### Step 2: Verify Entry Points
- Entry Price = current stock price (shown in green)
- Target Price = expected exit (shown in yellow)
- Confidence % = system conviction level

### Step 3: Make Investment Decision
- HIGH picks (≥75%) = strong conviction, consider buying
- MEDIUM picks (55-74%) = mixed signals, selective entry
- WATCH list = monitor, wait for confirmation

### Step 4: Execute Trade
- Use Discord notifications as trigger
- Reference dashboard for confirmation
- Entry: within 3% of shown price
- Exit: hit target or 10% stop loss

## 🔐 Live Runs

### FRI 5/15 @ 5:30 PM EST
- First production test
- Dashboard must show signals in real-time
- Discord alerts trigger on HIGH picks

### Ongoing (TUE/WED/THU)
- Regular 3x/week analysis
- Dashboard updates automatically
- New signals replace old ones

## 🛠️ Development

### Local Testing
1. Clone repo: `git clone https://github.com/buzzhamilton777/watchtower-dashboard.git`
2. Open `index.html` in browser
3. Dashboard loads test data from `data/latest.json`
4. Refresh every 5 minutes automatically

### Deployment
- GitHub Pages auto-deploys from `main` branch
- Dashboard updates via GitHubPublisher
- No manual steps needed

## 📞 Support

If dashboard doesn't load:
1. Check GitHub Pages status: https://github.com/buzzhamilton777/watchtower-dashboard
2. Verify `data/latest.json` exists in repo
3. Clear browser cache and reload
4. Check browser console for errors (F12)

---

**Built for K's investment thesis. Production ready for FRI 5/15 test run.**
