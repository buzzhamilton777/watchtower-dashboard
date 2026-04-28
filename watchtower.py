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
def fetch_congressional(signals: dict, fetched: list):
    log.info("Fetching congressional trades...")
    bought = 0

    # Source 1: Senate STOCK Act disclosures via efts.sec.gov proxy
    urls = [
        # Quiver Quant public CSV (no key needed for bulk)
        "https://www.quiverquant.com/congress/api/",
        # Direct Senate eFD XML feed (periodic)
        f"https://efts.sec.gov/LATEST/search-index?forms=4&dateRange=custom&startdt={days_ago_str(14)}&enddt={today_str()}&hits.hits.total.value=true",
    ]

    # Try House clerk bulk XML (the most reliable free source)
    year = datetime.now().year
    house_url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    # This is a ZIP of PDFs — too complex to parse on the fly. Use quiverquant or senate eFD instead.

    # Senate periodic XML
    senate_url = "https://efts.sec.gov/LATEST/search-index?forms=4&category=form-type&dateRange=custom" \
                 f"&startdt={days_ago_str(7)}&enddt={today_str()}&hits.hits._source=true"
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
                    if "purchase" not in txn_type and "buy" not in txn_type:
                        continue
                    ticker = str(txn.get("Ticker") or txn.get("ticker", "")).upper().strip()
                    if not ticker or ticker in ("", "--", "N/A"):
                        continue
                    rep = txn.get("Representative") or txn.get("representative", "Unknown Rep.")
                    amount = txn.get("Amount") or txn.get("amount", "")
                    detail = f"{rep} purchased {amount} ({date})"
                    signals[ticker].append({"source": "Congressional", "detail": detail, "pts": 1})
                    bought += 1
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

            # Fetch the index page for this filing
            index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_stripped}&type=13F-HR&dateb=&owner=include&count=1&search_text="
            # Use the direct accession URL instead
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/0{acc}-index.htm"

            # Try EDGAR search to get holdings
            search_url = (
                f"https://efts.sec.gov/LATEST/search-index?q=%2213F%22"
                f"&forms=13F-HR&dateRange=custom"
                f"&startdt={days_ago_str(120)}&enddt={today_str()}"
                f"&entity={requests.utils.quote(firm)}"
            )
            sr = safe_get(search_url)
            holdings_detail = f"{firm}: 13F-HR filed {filing_date}"

            # We can't easily parse the actual holdings XML without an extensive parser,
            # so we record the filing as a general whale signal on the firm's known top holdings.
            # Real parsing would require fetching the infotable XML.
            log.info("13F: %s latest filing %s on %s", firm, acc, filing_date)

            # Mark the filing as seen — award pts to known positions
            # We'll use a lightweight approach: flag the filing date recency
            filing_dt = datetime.strptime(filing_date, "%Y-%m-%d")
            days_old = (datetime.now() - filing_dt).days

            if days_old <= 45:  # Fresh 13F (within a quarter)
                # Fetch infotable XML to get actual holdings
                infotable_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/"
                idx_r = safe_get(infotable_url)
                if idx_r and "infotable" in idx_r.text.lower():
                    # Find the infotable link
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
    # Extract nameofissuer and ticker
    entries = re.findall(
        r"<nameofissuer>(.*?)</nameofissuer>.*?<cusip>(.*?)</cusip>.*?<value>(.*?)</value>.*?<sshprnamt>(.*?)</sshprnamt>",
        xml_text,
        re.DOTALL | re.I,
    )
    for name, cusip, value, shares in entries[:50]:  # cap at 50
        name = name.strip()
        shares_n = int(shares.strip().replace(",", "")) if shares.strip().replace(",", "").isdigit() else 0
        if shares_n > 100_000:
            # We don't have the ticker directly from infotable — use name as key
            ticker_guess = _name_to_ticker_guess(name)
            if ticker_guess:
                detail = f"{firm} holds {shares_n:,} shares of {name} (13F filed {filing_date})"
                signals[ticker_guess].append({"source": "13F Whale", "detail": detail, "pts": 3})


def _name_to_ticker_guess(name: str) -> str:
    name = name.upper()
    # Quick lookup for common names
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
            ticker = (src.get("file_num") or "").upper()
            # entity_name is usually the subject company, file_num is not ticker
            # Try display_names or period_of_report
            display = src.get("display_names", [])
            subject = display[0].get("name", entity) if display else entity

            # Try to get ticker from the filing
            ticker = ""
            for dn in display:
                t = dn.get("ticker", "")
                if t:
                    ticker = t.upper()
                    break

            if not ticker:
                continue

            filer = src.get("period_of_report", "")
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
def fetch_ark(signals: dict, fetched: list):
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

    # Compute diffs
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
    # Common English words to skip
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

        # Process in batches of 5 (pytrends limit)
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
# Discord alert
# ---------------------------------------------------------------------------
def send_discord_alert(tier1_tickers: list, tickers_data: list):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log.info("No DISCORD_WEBHOOK_URL set — skipping alert")
        return
    if not tier1_tickers:
        return

    lines = ["# WATCHTOWER — TIER 1 ALERT", f"**{datetime.now().strftime('%Y-%m-%d %H:%M UTC')}**", ""]

    for t in tickers_data:
        if t["ticker"] not in tier1_tickers:
            continue
        lines.append(f"## {t['ticker']} — Score {t['score']}/10+ ({t['company']})")
        for sig in t["signals"]:
            lines.append(f"• **{sig['source']}** (+{sig['pts']}pt): {sig['detail']}")
        lines.append("")

    lines.append("_See everything before the market does._")

    payload = {"content": "\n".join(lines)[:2000]}

    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Discord alert sent for %s", tier1_tickers)
    except Exception as e:
        log.error("Discord alert failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=== WATCHTOWER run started ===")
    start = datetime.now(timezone.utc)

    signals: dict[str, list] = defaultdict(list)
    fetched: list[str] = []

    # Run all sources — each is isolated; failures don't cascade
    fetch_congressional(signals, fetched)
    fetch_13d(signals, fetched)
    fetch_ark(signals, fetched)
    fetch_reddit(signals, fetched)

    # 13F needs its own step; build watchlist from signals so far for trends
    fetch_13f(signals, fetched)

    watchlist = list(signals.keys())[:25]  # Google Trends: top 25 by activity
    fetch_google_trends(signals, fetched, watchlist)

    # Resolve company names
    log.info("Resolving company names for %d tickers...", len(signals))
    companies = {}
    for ticker in signals:
        companies[ticker] = ticker_to_company(ticker)
        time.sleep(0.1)  # yfinance rate limiting

    # Build scored ticker list
    tickers_out = []
    total_scanned = len(signals)

    for ticker, sigs in signals.items():
        score = sum(s["pts"] for s in sigs)
        if score == 0:
            continue
        tier = assign_tier(score)
        last_date = today_str()

        # Deduplicate signals by source+detail
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

    tier1 = [t["ticker"] for t in tickers_out if t["tier"] == 1]
    tier2 = [t["ticker"] for t in tickers_out if t["tier"] == 2]
    tier3 = [t["ticker"] for t in tickers_out if t["tier"] == 3]
    tier4 = [t["ticker"] for t in tickers_out if t["tier"] == 4]

    output = {
        "generated_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "tickers": tickers_out,
        "tier1_alert": tier1,
        "stats": {
            "total_scanned": total_scanned,
            "tier1_count": len(tier1),
            "tier2_count": len(tier2),
            "tier3_count": len(tier3),
            "tier4_count": len(tier4),
            "sources_fetched": fetched,
        },
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    log.info("Wrote %s (%d tickers)", OUTPUT_PATH, len(tickers_out))

    if tier1:
        send_discord_alert(tier1, tickers_out)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("=== WATCHTOWER run complete in %.1fs — Tier 1: %s ===", elapsed, tier1 or "none")


if __name__ == "__main__":
    main()
