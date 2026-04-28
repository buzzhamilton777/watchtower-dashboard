#!/usr/bin/env python3
"""
WATCHTOWER — Investment Signal Intelligence
Fetches signals from SEC filings, ARK ETFs, Reddit, and Google Trends.
Runs daily via GitHub Actions. Results written to data/watchtower-data.json.
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/watchtower.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watchtower")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCORE_THRESHOLDS = {1: 8, 2: 5, 3: 3, 4: 1}  # tier: min_score
HEADERS = {"User-Agent": "watchtower/1.0 kurtafarmer@gmail.com"}

WHALE_CIKS = {
    "Berkshire Hathaway": "0001067983",
    "Pershing Square": "0001336528",
    "Druckenmiller Family Office": "0001536411",
}

ARK_FUNDS = ["ARKK", "ARKQ", "ARKW", "ARKG", "ARKF"]  # fetched via arkfunds.io API

REDDIT_SUBS = ["investing", "stocks", "options", "pennystocks", "wallstreetbets"]

PREVIOUS_DATA_PATH = Path("data/watchtower-previous.json")
OUTPUT_PATH = Path("data/watchtower-data.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago_str(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def safe_get(url, extra_headers=None, **kwargs):
    h = {**HEADERS, **(extra_headers or {})}
    try:
        r = requests.get(url, headers=h, timeout=20, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def ticker_to_company(ticker: str) -> str:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName") or ticker
    except Exception:
        return ticker


def assign_tier(score: int) -> int:
    if score >= 8:
        return 1
    if score >= 5:
        return 2
    if score >= 3:
        return 3
    return 4


# ---------------------------------------------------------------------------
# Source: Congressional trades (Capitol Trades + Senate STOCK Act)
# ---------------------------------------------------------------------------
def fetch_congressional(signals: dict, sell_signals: dict, fetched: list):
    log.info("Fetching congressional trades...")
    bought = 0

    # Senate periodic XML
    senate_url = (
        "https://efts.sec.gov/LATEST/search-index?forms=4&category=form-type&dateRange=custom"
        f"&startdt={days_ago_str(7)}&enddt={today_str()}&hits.hits._source=true"
    )
    r = safe_get(senate_url)
    if r:
        try:
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:50]:
                src = hit.get("_source", {})
                ticker = ""
                for dn in src.get("display_names", []):
                    t = dn.get("ticker", "")
                    if t:
                        ticker = t.upper()
                        break
                if not ticker:
                    continue
                entity = src.get("entity_name", "Unknown")
                filed = src.get("file_date", "")
                detail = f"{entity} Form 4 filed {filed}"
                signals[ticker].append({"source": "Congressional", "detail": detail, "pts": 1})
                bought += 1
        except Exception as e:
            log.warning("Congressional SEC parse failed: %s", e)

    # Fallback: try quiverquant public API (no key, limited)
    if bought == 0:
        qq_url = "https://api.quiverquant.com/beta/bulk/congresstrading"
        r = safe_get(qq_url)
        if r:
            try:
                data = r.json()
                cutoff = days_ago_str(14)
                for txn in (data if isinstance(data, list) else []):
                    date = str(txn.get("Date") or txn.get("transaction_date", ""))
                    if date < cutoff:
                        continue
                    txn_type = str(txn.get("Transaction") or txn.get("type", "")).lower()
                    ticker = str(txn.get("Ticker") or txn.get("ticker", "")).upper().strip()
                    if not ticker or ticker in ("", "--", "N/A"):
                        continue
                    rep = txn.get("Representative") or txn.get("representative", "Unknown Rep.")
                    amount = txn.get("Amount") or txn.get("amount", "")

                    if "purchase" in txn_type or "buy" in txn_type:
                        detail = f"{rep} purchased {amount} ({date})"
                        signals[ticker].append({"source": "Congressional", "detail": detail, "pts": 1})
                        bought += 1
                    elif "sale" in txn_type or "sell" in txn_type:
                        detail = f"{rep} sold {amount} ({date})"
                        sell_signals[ticker].append({"source": "Congressional", "detail": detail, "reason": "sell"})
            except Exception as e:
                log.warning("QuiverQuant congressional parse failed: %s", e)

    log.info("Congressional: %d buy signals found", bought)
    if bought > 0:
        fetched.append("congressional")


# ---------------------------------------------------------------------------
# Source: SEC 13F whale filings
# ---------------------------------------------------------------------------
def fetch_13f(signals: dict, fetched: list):
    log.info("Fetching 13F whale filings...")
    found_any = False

    for firm, cik in WHALE_CIKS.items():
        try:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            r = safe_get(sub_url)
            if not r:
                continue
            sub = r.json()

            # Find the most recent 13F-HR filing
            filings = sub.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            dates = filings.get("filingDate", [])
            accessions = filings.get("accessionNumber", [])

            idx = None
            for i, f in enumerate(forms):
                if f == "13F-HR":
                    idx = i
                    break

            if idx is None:
                log.warning("No 13F-HR found for %s", firm)
                continue

            acc = accessions[idx].replace("-", "")
            filing_date = dates[idx]
            cik_stripped = cik.lstrip("0")

            search_url = (
                f"https://efts.sec.gov/LATEST/search-index?q=%2213F%22"
                f"&forms=13F-HR&dateRange=custom"
                f"&startdt={days_ago_str(120)}&enddt={today_str()}"
                f"&entity={requests.utils.quote(firm)}"
            )
            safe_get(search_url)

            log.info("13F: %s latest filing %s on %s", firm, acc, filing_date)

            filing_dt = datetime.strptime(filing_date, "%Y-%m-%d")
            days_old = (datetime.now() - filing_dt).days

            if days_old <= 45:
                infotable_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/"
                idx_r = safe_get(infotable_url)
                if idx_r and "infotable" in idx_r.text.lower():
                    import re
                    links = re.findall(r'href="([^"]*infotable[^"]*)"', idx_r.text, re.I)
                    if links:
                        it_url = f"https://www.sec.gov{links[0]}" if links[0].startswith("/") else links[0]
                        it_r = safe_get(it_url)
                        if it_r:
                            _parse_infotable(it_r.text, firm, filing_date, signals)
                            found_any = True

        except Exception as e:
            log.error("13F fetch failed for %s: %s", firm, e)

    if found_any:
        fetched.append("13f")
    else:
        log.warning("13F: No holdings parsed (SEC rate limits or no fresh filings)")
        fetched.append("13f_partial")


def _parse_infotable(xml_text: str, firm: str, filing_date: str, signals: dict):
    import re
    entries = re.findall(
        r"<nameofissuer>(.*?)</nameofissuer>.*?<cusip>(.*?)</cusip>.*?<value>(.*?)</value>.*?<sshprnamt>(.*?)</sshprnamt>",
        xml_text,
        re.DOTALL | re.I,
    )
    for name, cusip, value, shares in entries[:50]:
        name = name.strip()
        shares_n = int(shares.strip().replace(",", "")) if shares.strip().replace(",", "").isdigit() else 0
        if shares_n > 100_000:
            ticker_guess = _name_to_ticker_guess(name)
            if ticker_guess:
                detail = f"{firm} holds {shares_n:,} shares of {name} (13F filed {filing_date})"
                signals[ticker_guess].append({"source": "13F Whale", "detail": detail, "pts": 3})


def _name_to_ticker_guess(name: str) -> str:
    name = name.upper()
    MAP = {
        "APPLE": "AAPL", "MICROSOFT": "MSFT", "AMAZON": "AMZN", "ALPHABET": "GOOGL",
        "NVIDIA": "NVDA", "META": "META", "TESLA": "TSLA", "BERKSHIRE": "BRK.B",
        "UNITED PARCEL": "UPS", "BANK OF AMERICA": "BAC", "AMERICAN EXPRESS": "AXP",
        "COCA-COLA": "KO", "CHEVRON": "CVX", "OCCIDENTAL": "OXY", "KRAFT HEINZ": "KHC",
        "MOODY": "MCO", "DAVITA": "DVA", "HP INC": "HPQ", "ACTIVISION": "MSFT",
        "ROBINHOOD": "HOOD", "PALANTIR": "PLTR", "NETFLIX": "NFLX", "SPOTIFY": "SPOT",
    }
    for k, v in MAP.items():
        if k in name:
            return v
    return ""


# ---------------------------------------------------------------------------
# Source: SEC 13D activist filings
# ---------------------------------------------------------------------------
def fetch_13d(signals: dict, fetched: list):
    log.info("Fetching 13D activist filings...")
    url = (
        f"https://efts.sec.gov/LATEST/search-index?forms=SC+13D"
        f"&dateRange=custom&startdt={days_ago_str(14)}&enddt={today_str()}"
        f"&hits.hits.total.value=true"
    )
    r = safe_get(url)
    if not r:
        log.error("13D source failed")
        return

    try:
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
    except Exception as e:
        log.error("13D JSON parse failed: %s", e)
        return

    for hit in hits:
        try:
            src = hit.get("_source", {})
            entity = src.get("entity_name", "Unknown filer")
            display = src.get("display_names", [])
            subject = display[0].get("name", entity) if display else entity

            ticker = ""
            for dn in display:
                t = dn.get("ticker", "")
                if t:
                    ticker = t.upper()
                    break

            if not ticker:
                continue

            filed = src.get("file_date", "")
            detail = f"SC 13D: {entity} filed on {subject} ({filed})"
            signals[ticker].append({"source": "13D Activist", "detail": detail, "pts": 4})
        except Exception:
            continue

    log.info("13D: %d filings found", len(hits))
    fetched.append("13d")


# ---------------------------------------------------------------------------
# Source: ARK Invest daily holdings (via arkfunds.io)
# ---------------------------------------------------------------------------
def fetch_ark(signals: dict, sell_signals: dict, fetched: list):
    log.info("Fetching ARK holdings...")
    prev = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            prev = prev_data.get("ark_holdings", {})
        except Exception:
            pass

    current = {}
    success = False

    for fund in ARK_FUNDS:
        url = f"https://arkfunds.io/api/v2/etf/holdings?symbol={fund}"
        r = safe_get(url)
        if not r:
            log.warning("ARK %s fetch failed", fund)
            continue

        try:
            data = r.json()
            holdings = data.get("holdings", [])
            for h in holdings:
                ticker = str(h.get("ticker") or "").upper().strip()
                if not ticker or ticker in ("-", "N/A", ""):
                    continue
                shares = float(h.get("shares") or 0)
                company = h.get("company", "")
                key = f"{fund}:{ticker}"
                current[key] = {"shares": shares, "company": company, "fund": fund, "ticker": ticker}

            success = True
            count = sum(1 for k in current if k.startswith(fund))
            log.info("ARK %s: %d holdings parsed", fund, count)

        except Exception as e:
            log.error("ARK %s parse failed: %s", fund, e)

    # Compute diffs — buy signals for increases, sell signals for reductions
    for key, data in current.items():
        ticker = data["ticker"]
        fund = data["fund"]
        shares = data["shares"]
        prev_shares = prev.get(key, {}).get("shares", 0)

        if prev_shares == 0 and shares > 0:
            detail = f"{fund} new position: {int(shares):,} shares added"
            signals[ticker].append({"source": "ARK", "detail": detail, "pts": 2})
        elif prev_shares > 0 and shares > prev_shares * 1.10:
            pct = ((shares - prev_shares) / prev_shares) * 100
            detail = f"{fund} increased position +{pct:.0f}% ({int(prev_shares):,}→{int(shares):,} shares)"
            signals[ticker].append({"source": "ARK", "detail": detail, "pts": 1})
        elif prev_shares > 0 and shares < prev_shares * 0.80:
            pct = ((prev_shares - shares) / prev_shares) * 100
            detail = f"{fund} reduced {ticker} by {pct:.0f}% ({int(prev_shares):,}→{int(shares):,} shares)"
            sell_signals[ticker].append({"source": "ARK", "detail": detail, "reason": "sell"})

    # Detect full ARK exits (was in prev, not in current)
    current_keys = set(current.keys())
    for key, prev_info in prev.items():
        if key not in current_keys and prev_info.get("shares", 0) > 0:
            ticker = prev_info["ticker"]
            fund = prev_info["fund"]
            detail = f"{fund} fully exited {ticker} (was {int(prev_info['shares']):,} shares)"
            sell_signals[ticker].append({"source": "ARK", "detail": detail, "reason": "sell"})
            log.info("ARK full exit detected: %s from %s", ticker, fund)

    # Save current for next run
    try:
        prev_data = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_data["ark_holdings"] = current
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_data))
    except Exception as e:
        log.warning("Could not save ARK snapshot: %s", e)

    if success:
        fetched.append("ark")


# ---------------------------------------------------------------------------
# Source: Reddit mention scanner
# ---------------------------------------------------------------------------
def fetch_reddit(signals: dict, fetched: list):
    log.info("Fetching Reddit signals...")

    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    user_agent = os.getenv("REDDIT_USER_AGENT", "watchtower:v1.0 (by watchtower)")

    mention_counts: dict[str, int] = defaultdict(int)
    success = False

    # Try PRAW first
    if client_id and client_secret:
        try:
            import praw
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent,
            )
            for sub in REDDIT_SUBS:
                try:
                    for post in reddit.subreddit(sub).top(time_filter="day", limit=100):
                        _count_tickers(post.title + " " + (post.selftext or ""), mention_counts)
                    success = True
                except Exception as e:
                    log.warning("PRAW r/%s failed: %s", sub, e)
        except Exception as e:
            log.warning("PRAW init failed: %s — falling back to public JSON", e)

    # Fallback: public Reddit JSON (no auth needed)
    if not success:
        for sub in REDDIT_SUBS:
            url = f"https://www.reddit.com/r/{sub}/top.json?limit=100&t=day"
            r = safe_get(url, extra_headers={"User-Agent": user_agent})
            if not r:
                continue
            try:
                posts = r.json().get("data", {}).get("children", [])
                for post in posts:
                    d = post.get("data", {})
                    _count_tickers(d.get("title", "") + " " + d.get("selftext", ""), mention_counts)
                success = True
                time.sleep(1)  # Reddit rate limiting
            except Exception as e:
                log.warning("Public Reddit r/%s failed: %s", sub, e)

    # Load baseline mention counts
    prev_reddit = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            prev_reddit = prev_data.get("reddit_baseline", {})
        except Exception:
            pass

    SPIKE_THRESHOLD = 50
    SPIKE_MULTIPLIER = 2.0

    for ticker, count in mention_counts.items():
        baseline = prev_reddit.get(ticker, max(10, count // 3))
        if count >= SPIKE_THRESHOLD and count >= baseline * SPIKE_MULTIPLIER:
            subs_str = ", ".join([f"r/{s}" for s in REDDIT_SUBS[:3]])
            detail = f"{count} mentions today vs {baseline} baseline ({subs_str})"
            signals[ticker].append({"source": "Reddit", "detail": detail, "pts": 1})

    # Save new baseline
    try:
        prev_data = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_data["reddit_baseline"] = dict(mention_counts)
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_data))
    except Exception as e:
        log.warning("Could not save Reddit baseline: %s", e)

    if success:
        fetched.append("reddit")


TICKER_PATTERN = None

def _count_tickers(text: str, counts: dict):
    global TICKER_PATTERN
    import re
    if TICKER_PATTERN is None:
        TICKER_PATTERN = re.compile(r'\b([A-Z]{2,5})\b')
    SKIP = {
        "I", "A", "THE", "AND", "OR", "FOR", "TO", "IN", "OF", "ON", "AT",
        "BY", "IS", "IT", "AS", "AN", "BE", "WE", "DO", "IF", "SO", "UP",
        "EPS", "CEO", "CFO", "IPO", "GDP", "ETF", "ATH", "ATL", "EOD",
        "IMO", "IMH", "DD", "OTC", "USD", "US", "UK", "EU", "AI", "ML",
        "API", "IT", "PE", "IV", "EV", "YOY", "QOQ", "FY", "Q1", "Q2",
        "Q3", "Q4", "YTD", "BOT", "DIP", "BUY", "SELL", "HOLD",
        "EDIT", "TLDR", "FWIW", "IIRC", "AFAIK", "LOL", "LMAO",
    }
    for m in TICKER_PATTERN.finditer(text):
        t = m.group(1)
        if t not in SKIP and len(t) >= 2:
            counts[t] += 1


# ---------------------------------------------------------------------------
# Source: Google Trends
# ---------------------------------------------------------------------------
def fetch_google_trends(signals: dict, fetched: list, watchlist: list):
    log.info("Fetching Google Trends...")
    if not watchlist:
        log.info("No watchlist tickers to check for trends")
        return

    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))

        for i in range(0, min(len(watchlist), 25), 5):
            batch = watchlist[i:i + 5]
            try:
                pytrends.build_payload(batch, cat=0, timeframe="now 7-d", geo="US")
                interest = pytrends.interest_over_time()

                if interest.empty:
                    continue

                for ticker in batch:
                    if ticker not in interest.columns:
                        continue
                    col = interest[ticker].dropna()
                    if len(col) < 2:
                        continue
                    recent = col.iloc[-1]
                    previous = col.iloc[max(0, len(col) - 8):-1].mean()
                    if previous > 0:
                        pct_change = ((recent - previous) / previous) * 100
                        if pct_change >= 20:
                            detail = f"+{pct_change:.0f}% WoW acceleration (Google Trends)"
                            signals[ticker].append({"source": "Google Trends", "detail": detail, "pts": 1})

                time.sleep(1)  # Rate limiting
            except Exception as e:
                log.warning("Google Trends batch %s failed: %s", batch, e)

        fetched.append("google_trends")
    except Exception as e:
        log.error("Google Trends failed: %s", e)


# ---------------------------------------------------------------------------
# Discord daily brief
# ---------------------------------------------------------------------------
def send_daily_brief(tickers_out: list, sell_alerts: list, stats: dict):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log.info("No DISCORD_WEBHOOK_URL set — skipping daily brief")
        return

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%a %b %-d")

    sep = "─" * 32
    lines = [
        f"🗼 WATCHTOWER DAILY BRIEF — {date_str}",
        sep,
    ]

    tier1 = [t for t in tickers_out if t["tier"] == 1]
    tier2 = [t for t in tickers_out if t["tier"] == 2]

    # Tier 1 section
    lines.append("🔴 TIER 1 — MAX CONVICTION")
    if tier1:
        for t in tier1:
            top_sigs = [s["detail"] for s in t["signals"][:3]]
            sig_str = ", ".join(top_sigs)
            lines.append(f"• {t['ticker']} (Score: {t['score']}) — {sig_str}")
    else:
        lines.append("• None today")

    lines.append("")

    # Tier 2 section (top 5 only)
    lines.append("🟠 TIER 2 — HIGH CONVICTION")
    if tier2:
        for t in tier2[:5]:
            top_sigs = [s["detail"] for s in t["signals"][:3]]
            sig_str = ", ".join(top_sigs)
            lines.append(f"• {t['ticker']} (Score: {t['score']}) — {sig_str}")
    else:
        lines.append("• None today")

    lines.append("")

    # Sell alerts section
    lines.append("🚨 SELL ALERTS")
    if sell_alerts:
        for alert in sell_alerts:
            reasons_str = ", ".join(alert["reasons"])
            lines.append(f"• {alert['ticker']} — {reasons_str}")
    else:
        lines.append("• None today")

    lines.append("")

    # Stats line
    sources_active = len(stats.get("sources_fetched", []))
    lines.append(
        f"📊 Stats: {stats['total_scanned']} tickers scanned | "
        f"{stats['tier1_count']} Tier 1 | {stats['tier2_count']} Tier 2 | "
        f"{sources_active} sources active"
    )
    lines.append(sep)
    lines.append("See everything before the market does. 🗼")

    message = "\n".join(lines)

    # Truncate Tier 2 entries if over Discord's 2000-char limit
    if len(message) > 1900:
        lines_trimmed = []
        in_tier2 = False
        tier2_count = 0
        for line in lines:
            if line.startswith("🟠 TIER 2"):
                in_tier2 = True
            elif line == "" and in_tier2:
                in_tier2 = False
            if in_tier2 and line.startswith("•"):
                tier2_count += 1
                if tier2_count > 3:
                    continue
            lines_trimmed.append(line)
        message = "\n".join(lines_trimmed)

    payload = {"content": message[:2000]}

    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Discord daily brief sent")
    except Exception as e:
        log.error("Discord daily brief failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== WATCHTOWER run started ===")
    start = datetime.now(timezone.utc)

    signals: dict[str, list] = defaultdict(list)
    sell_signals: dict[str, list] = defaultdict(list)
    fetched: list[str] = []

    # Load previous run data for comparisons
    prev_tickers: dict[str, dict] = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            for t in prev_data.get("tickers", []):
                prev_tickers[t["ticker"]] = t
        except Exception as e:
            log.warning("Could not load previous tickers for comparison: %s", e)

    # Run all sources — each is isolated; failures don't cascade
    fetch_congressional(signals, sell_signals, fetched)
    fetch_13d(signals, fetched)
    fetch_ark(signals, sell_signals, fetched)
    fetch_reddit(signals, fetched)

    fetch_13f(signals, fetched)

    watchlist = list(signals.keys())[:25]
    fetch_google_trends(signals, fetched, watchlist)

    # Resolve company names
    log.info("Resolving company names for %d tickers...", len(signals))
    companies = {}
    for ticker in signals:
        companies[ticker] = ticker_to_company(ticker)
        time.sleep(0.1)

    # Build scored ticker list
    tickers_out = []
    total_scanned = len(signals)

    for ticker, sigs in signals.items():
        score = sum(s["pts"] for s in sigs)
        if score == 0:
            continue
        tier = assign_tier(score)
        last_date = today_str()

        seen = set()
        unique_sigs = []
        for s in sorted(sigs, key=lambda x: -x["pts"]):
            key = (s["source"], s["detail"][:60])
            if key not in seen:
                seen.add(key)
                unique_sigs.append(s)

        tickers_out.append({
            "ticker": ticker,
            "company": companies.get(ticker, ticker),
            "score": score,
            "tier": tier,
            "signals": unique_sigs,
            "last_signal_date": last_date,
        })

    tickers_out.sort(key=lambda x: -x["score"])

    # Current conviction set: tickers in tier 1 or 2 this run or previous run
    curr_map = {t["ticker"]: t for t in tickers_out}
    conviction_tickers = {t["ticker"] for t in tickers_out if t["tier"] in (1, 2)} | \
                         {tk for tk, td in prev_tickers.items() if td.get("tier") in (1, 2)}

    # Build sell alerts
    sell_alerts = []
    processed = set()

    # 1. Tier drop: was tier 1/2 yesterday, now tier 3/4 or missing
    for ticker, prev_t in prev_tickers.items():
        prev_tier = prev_t.get("tier", 4)
        if prev_tier not in (1, 2):
            continue
        curr_t = curr_map.get(ticker)
        curr_tier = curr_t["tier"] if curr_t else 4
        curr_score = curr_t["score"] if curr_t else 0
        prev_score = prev_t.get("score", 0)

        reasons = []

        if curr_tier >= 3:
            reasons.append(f"Tier dropped from {prev_tier}→{curr_tier}")

        # 2. Score collapse: dropped 3+ points
        if prev_score - curr_score >= 3:
            reasons.append(f"Score fell {prev_score}→{curr_score} (-{prev_score - curr_score})")

        # 3. ARK sell signals for this ticker
        for sig in sell_signals.get(ticker, []):
            if sig["source"] == "ARK":
                reasons.append(sig["detail"])

        # 4. Congressional sell for conviction tickers
        for sig in sell_signals.get(ticker, []):
            if sig["source"] == "Congressional":
                reasons.append(sig["detail"])

        if reasons:
            sell_alerts.append({
                "ticker": ticker,
                "company": prev_t.get("company", ticker),
                "prev_tier": prev_tier,
                "curr_tier": curr_tier,
                "reasons": reasons,
            })
            processed.add(ticker)

    # Also flag ARK/congressional sell signals on current conviction tickers not already processed
    for ticker in conviction_tickers - processed:
        if ticker not in sell_signals:
            continue
        reasons = []
        for sig in sell_signals[ticker]:
            reasons.append(sig["detail"])
        if reasons:
            curr_t = curr_map.get(ticker)
            prev_t = prev_tickers.get(ticker, {})
            sell_alerts.append({
                "ticker": ticker,
                "company": (curr_t or prev_t).get("company", ticker),
                "prev_tier": prev_t.get("tier", 4) if prev_t else None,
                "curr_tier": curr_t["tier"] if curr_t else None,
                "reasons": reasons,
            })

    log.info("Sell alerts generated: %d", len(sell_alerts))

    # Persist tickers list for next run's tier comparison
    try:
        prev_snapshot = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_snapshot["tickers"] = tickers_out
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_snapshot))
    except Exception as e:
        log.warning("Could not persist tickers snapshot: %s", e)

    tier1 = [t["ticker"] for t in tickers_out if t["tier"] == 1]
    tier2 = [t["ticker"] for t in tickers_out if t["tier"] == 2]
    tier3 = [t["ticker"] for t in tickers_out if t["tier"] == 3]
    tier4 = [t["ticker"] for t in tickers_out if t["tier"] == 4]

    stats = {
        "total_scanned": total_scanned,
        "tier1_count": len(tier1),
        "tier2_count": len(tier2),
        "tier3_count": len(tier3),
        "tier4_count": len(tier4),
        "sources_fetched": fetched,
    }

    output = {
        "generated_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "tickers": tickers_out,
        "tier1_alert": tier1,
        "sell_alerts": sell_alerts,
        "stats": stats,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    log.info("Wrote %s (%d tickers, %d sell alerts)", OUTPUT_PATH, len(tickers_out), len(sell_alerts))

    send_daily_brief(tickers_out, sell_alerts, stats)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("=== WATCHTOWER run complete in %.1fs — Tier 1: %s ===", elapsed, tier1 or "none")


if __name__ == "__main__":
    main()
