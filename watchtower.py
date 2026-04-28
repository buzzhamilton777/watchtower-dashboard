#!/usr/bin/env python3
"""
WATCHTOWER — Investment Signal Intelligence
Fetches signals from SEC filings, ARK ETFs, Reddit, and Google Trends.
Runs daily via GitHub Actions. Results written to data/watchtower-data.json.
"""

import codecs
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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
# Scoring: ARK new=2, ARK increase>10%=1, 13D=5, 13F freshness-decayed(2/1/0),
#          Congressional=3, Reddit+corroboration=1, Reddit-solo=0, Trends=1,
#          Form4 cluster=4 / single>100K=3 / single<100K=2
SCORE_THRESHOLDS = {1: 4, 2: 3, 3: 2, 4: 1}
HEADERS = {"User-Agent": "watchtower/1.0 kurtafarmer@gmail.com"}

WHALE_CIKS = {
    "Berkshire Hathaway": "0001067983",
    "Pershing Square": "0001336528",
    "Druckenmiller Family Office": "0001536411",
}

ARK_FUNDS = ["ARKK", "ARKQ", "ARKW", "ARKG", "ARKF"]

REDDIT_SUBS = ["investing", "stocks", "options", "pennystocks", "wallstreetbets"]

PREVIOUS_DATA_PATH = Path("data/watchtower-previous.json")
OUTPUT_PATH = Path("data/watchtower-data.json")

# Camillo behavioral signal layer
CAMILLO_SCORES = {
    "app_store_climb": 1,      # app rank improvement >10 positions WoW
    "job_acceleration": 2,     # job postings up >50% WoW
    "subreddit_growth": 1,     # relevant subreddit gained >5% subscribers in 7 days
    "news_sentiment": 1,       # net positive news score >= 3 in 24h
    "short_interest_cover": 2, # short interest fell >20% vs prior period
}

# Subreddit → ticker mapping for Camillo layer
SUBREDDIT_TICKER_MAP = {
    "peptides": "BCHMY",
    "glp1": "HIMS",
    "hims": "HIMS",
    "himsandhers": "HIMS",
    "robinhoodapp": "HOOD",
    "robinhood": "HOOD",
    "palantir": "PLTR",
    "nvidia": "NVDA",
    "amazon": "AMZN",
    "arkinvest": "ARKK",
}

# Consumer-facing tickers that benefit from review velocity tracking
CONSUMER_TICKERS = ["HIMS", "AMZN", "HOOD", "PLTR"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago_str(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def safe_get(url, extra_headers=None, timeout=20, **kwargs):
    h = {**HEADERS, **(extra_headers or {})}
    try:
        r = requests.get(url, headers=h, timeout=timeout, **kwargs)
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
    for tier in sorted(SCORE_THRESHOLDS.keys()):
        if score >= SCORE_THRESHOLDS[tier]:
            return tier
    return 4


# ---------------------------------------------------------------------------
# Source: Congressional trades (House Stock Watcher + Senate Stock Watcher)
# ---------------------------------------------------------------------------
CACHE_CONGRESSIONAL_PATH = Path("data/congressional-cache.json")


def _load_congressional_cache() -> dict:
    if not CACHE_CONGRESSIONAL_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_CONGRESSIONAL_PATH.read_text())
    except Exception:
        return {}


def _save_congressional_cache(cache: dict):
    try:
        CACHE_CONGRESSIONAL_PATH.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning("Could not save congressional cache: %s", e)


def _is_congressional_cache_valid(cache: dict) -> bool:
    ts = cache.get("timestamp")
    if not ts:
        return False
    try:
        cache_dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - cache_dt).total_seconds() < 86400
    except Exception:
        return False


def fetch_congressional(signals: dict, sell_signals: dict, fetched: list):
    log.info("Fetching congressional trades from Capitol Trades...")
    cutoff = days_ago_str(30)
    house_buys = 0
    senate_buys = 0
    new_signals = []

    url = "https://www.capitoltrades.com/trades?pageSize=100"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    r = safe_get(url, extra_headers=headers, timeout=30)
    if r:
        try:
            html = r.text
            match = re.search(r'\\"data\\":\[', html)
            trades = []
            if match:
                start_idx = match.end()
                depth = 1
                i = start_idx
                while i < len(html) and depth > 0:
                    if html[i:i+2] == '\\"':
                        i += 2
                        continue
                    if html[i] == "[":
                        depth += 1
                    elif html[i] == "]":
                        depth -= 1
                    i += 1
                if depth == 0:
                    data_str = "[" + html[start_idx:i-1] + "]"
                    data_str = codecs.decode(data_str, "unicode_escape")
                    trades = json.loads(data_str)
            if trades:
                for trade in trades:
                    tx_date = str(trade.get("txDate", ""))[:10]
                    if tx_date < cutoff:
                        continue
                    tx_type = str(trade.get("txType", "")).lower()
                    issuer = trade.get("issuer") or {}
                    ticker_raw = issuer.get("issuerTicker") or ""
                    ticker = ticker_raw.split(":")[0].upper().strip() if ticker_raw else ""
                    if not ticker or ticker in ("", "--", "N/A", "—"):
                        continue
                    politician = trade.get("politician", {})
                    first = politician.get("firstName", "")
                    last = politician.get("lastName", "")
                    name = f"{first} {last}".strip() or "Unknown"
                    chamber = trade.get("chamber", "")
                    value = trade.get("value", 0)
                    value_str = f"${value:,.0f}" if value else "undisclosed"

                    prefix = "Rep." if chamber == "house" else "Sen."
                    if tx_type == "buy":
                        detail = f"{prefix} {name} bought {value_str} ({tx_date})"
                        signals[ticker].append({"source": "Congressional", "detail": detail, "pts": 3})
                        new_signals.append({"ticker": ticker, "detail": detail, "pts": 3, "type": "buy"})
                        if chamber == "house":
                            house_buys += 1
                        else:
                            senate_buys += 1
                    elif tx_type == "sell":
                        detail = f"{prefix} {name} sold {value_str} ({tx_date})"
                        sell_signals[ticker].append({"source": "Congressional", "detail": detail, "reason": "sell"})
                        new_signals.append({"ticker": ticker, "detail": detail, "type": "sell"})
            else:
                log.warning("Capitol Trades: Could not find trade data in response")
        except Exception as e:
            log.warning("Capitol Trades parse failed: %s", e)
    else:
        log.warning("Capitol Trades fetch failed")

    total_buys = house_buys + senate_buys
    log.info("Congressional: %d buy signals (House: %d, Senate: %d)", total_buys, house_buys, senate_buys)

    if total_buys > 0:
        cache = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signals": new_signals,
        }
        _save_congressional_cache(cache)
        fetched.append("congressional")
    else:
        cache = _load_congressional_cache()
        if _is_congressional_cache_valid(cache):
            log.info("Congressional: Using cached data from %s", cache.get("timestamp", "unknown"))
            cached_count = 0
            for sig in cache.get("signals", []):
                ticker = sig.get("ticker")
                detail = sig.get("detail")
                sig_type = sig.get("type")
                pts = sig.get("pts", 3)
                if ticker and detail:
                    if sig_type == "buy":
                        signals[ticker].append({"source": "Congressional", "detail": detail, "pts": pts})
                        cached_count += 1
                    elif sig_type == "sell":
                        sell_signals[ticker].append({"source": "Congressional", "detail": detail, "reason": "sell"})
            if cached_count > 0:
                fetched.append("congressional")
                log.info("Congressional: Loaded %d cached buy signals", cached_count)
        else:
            log.warning("Congressional: No data from APIs and no valid cache available")


# ---------------------------------------------------------------------------
# Source: Form 4 insider buying
# ---------------------------------------------------------------------------
def fetch_form4_insiders(signals: dict, fetched: list):
    log.info("Fetching Form 4 insider buys...")
    url = (
        f"https://efts.sec.gov/LATEST/search-index?forms=4&dateRange=custom"
        f"&startdt={days_ago_str(7)}&enddt={today_str()}&hits.hits._source=true"
    )
    r = safe_get(url)
    if not r:
        log.error("Form 4 fetch failed")
        return

    try:
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
    except Exception as e:
        log.error("Form 4 JSON parse failed: %s", e)
        return

    ticker_buys: dict = defaultdict(list)

    for hit in hits:
        try:
            src = hit.get("_source", {})

            # Filter for purchase transactions (type "P") when the field is present
            txn_type = (
                src.get("transaction_code")
                or src.get("transactionCode")
                or src.get("transaction_type", "")
            )
            if txn_type and str(txn_type).upper() != "P":
                continue

            ticker = ""
            for dn in src.get("display_names", []):
                t = dn.get("ticker", "")
                if t:
                    ticker = t.upper()
                    break
            if not ticker:
                continue

            entity = src.get("entity_name", "Unknown")
            value = 0.0
            try:
                value = float(
                    src.get("transaction_value")
                    or src.get("transactionValue")
                    or src.get("value", 0)
                    or 0
                )
            except (ValueError, TypeError):
                value = 0.0

            ticker_buys[ticker].append({"name": entity, "value": value})
        except Exception:
            continue

    found_any = False
    for ticker, buys in ticker_buys.items():
        seen_names: set = set()
        unique_buys = []
        for b in buys:
            if b["name"] not in seen_names:
                seen_names.add(b["name"])
                unique_buys.append(b)

        if len(unique_buys) >= 2:
            names = ", ".join(b["name"] for b in unique_buys[:3])
            signals[ticker].append({
                "source": "Form4 Insider",
                "detail": f"Cluster insider buy: {names}",
                "pts": 4,
            })
            found_any = True
        elif len(unique_buys) == 1:
            b = unique_buys[0]
            if b["value"] >= 100_000:
                signals[ticker].append({
                    "source": "Form4 Insider",
                    "detail": f"{b['name']} bought ${b['value']:,.0f}",
                    "pts": 3,
                })
            else:
                signals[ticker].append({
                    "source": "Form4 Insider",
                    "detail": f"{b['name']} bought ${b['value']:,.0f} (small)",
                    "pts": 2,
                })
            found_any = True

    log.info("Form4: %d tickers with insider buys", len(ticker_buys))
    if found_any:
        fetched.append("form4")


# ---------------------------------------------------------------------------
# Source: SEC 13F whale filings
# ---------------------------------------------------------------------------
CACHE_13F_PATH = Path("data/13f-cache.json")


def _load_13f_cache() -> dict:
    if not CACHE_13F_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_13F_PATH.read_text())
    except Exception:
        return {}


def _save_13f_cache(cache: dict):
    try:
        CACHE_13F_PATH.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning("Could not save 13F cache: %s", e)


def _is_cache_valid(cache: dict) -> bool:
    ts = cache.get("timestamp")
    if not ts:
        return False
    try:
        cache_dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - cache_dt).total_seconds() < 86400
    except Exception:
        return False


def _fetch_with_retry(url: str, timeout: int = 20):
    r = safe_get(url, timeout=timeout)
    if r is not None:
        return r
    log.info("13F: Rate limited or failed, waiting 10s before retry...")
    time.sleep(10)
    return safe_get(url, timeout=timeout)


def fetch_13f(signals: dict, fetched: list):
    log.info("Fetching 13F whale filings...")
    cache = _load_13f_cache()
    found_any = False
    new_holdings: dict = {}

    for firm, cik in WHALE_CIKS.items():
        try:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            r = _fetch_with_retry(sub_url)
            if not r:
                log.warning("13F: Could not fetch submissions for %s", firm)
                time.sleep(2)
                continue
            sub = r.json()

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
                time.sleep(2)
                continue

            acc = accessions[idx].replace("-", "")
            filing_date = dates[idx]
            cik_stripped = cik.lstrip("0")

            log.info("13F: %s latest filing %s on %s", firm, acc, filing_date)

            filing_dt = datetime.strptime(filing_date, "%Y-%m-%d")
            days_old = (datetime.now() - filing_dt).days

            if days_old <= 45:
                time.sleep(2)
                infotable_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc}/"
                idx_r = _fetch_with_retry(infotable_url)
                if idx_r and "infotable" in idx_r.text.lower():
                    import re
                    links = re.findall(r'href="([^"]*infotable[^"]*\.xml)"', idx_r.text, re.I)
                    if not links:
                        links = re.findall(r'href="([^"]*infotable[^"]*)"', idx_r.text, re.I)
                    if links:
                        link = links[0]
                        if link.startswith("/"):
                            it_url = f"https://www.sec.gov{link}"
                        elif not link.startswith("http"):
                            it_url = f"{infotable_url}{link}"
                        else:
                            it_url = link
                        time.sleep(2)
                        it_r = _fetch_with_retry(it_url)
                        if it_r:
                            holdings = _parse_infotable(it_r.text, firm, filing_date, days_old, signals)
                            if holdings:
                                new_holdings[firm] = {"filing_date": filing_date, "holdings": holdings}
                                found_any = True

            time.sleep(2)

        except Exception as e:
            log.error("13F fetch failed for %s: %s", firm, e)

    if found_any:
        cache = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "holdings": new_holdings,
        }
        _save_13f_cache(cache)
        fetched.append("13f")
    elif _is_cache_valid(cache):
        log.info("13F: Using cached data from %s", cache.get("timestamp", "unknown"))
        cached_holdings = cache.get("holdings", {})
        for firm, data in cached_holdings.items():
            filing_date = data.get("filing_date", "")
            holdings = data.get("holdings", [])
            if not filing_date:
                continue
            try:
                filing_dt = datetime.strptime(filing_date, "%Y-%m-%d")
                days_old = (datetime.now() - filing_dt).days
            except Exception:
                days_old = 30
            for h in holdings:
                ticker = h.get("ticker")
                detail = h.get("detail")
                pts = h.get("pts", 0)
                if ticker and detail and pts > 0:
                    signals[ticker].append({"source": "13F Whale", "detail": detail, "pts": pts})
                    found_any = True
        if found_any:
            fetched.append("13f")
        else:
            fetched.append("13f_partial")
    else:
        log.warning("13F: No holdings parsed and no valid cache available")
        fetched.append("13f_partial")


def _parse_infotable(xml_text: str, firm: str, filing_date: str, days_old: int, signals: dict) -> list:
    import re
    if days_old <= 14:
        pts = 2
    elif days_old <= 30:
        pts = 1
    else:
        pts = 0

    holdings = []
    if pts == 0:
        return holdings

    # Try multiple XML patterns for robustness
    entries = re.findall(
        r"<nameOfIssuer>(.*?)</nameOfIssuer>.*?<value>(.*?)</value>.*?<sshPrnamt>(.*?)</sshPrnamt>",
        xml_text,
        re.DOTALL | re.I,
    )
    if not entries:
        entries = re.findall(
            r"<nameofissuer>(.*?)</nameofissuer>.*?<value>(.*?)</value>.*?<sshprnamt>(.*?)</sshprnamt>",
            xml_text,
            re.DOTALL | re.I,
        )
    if not entries:
        # Fallback: try with cusip in between
        entries = re.findall(
            r"<(?:nameOfIssuer|nameofissuer)>(.*?)</(?:nameOfIssuer|nameofissuer)>.*?<(?:cusip|CUSIP)>.*?</(?:cusip|CUSIP)>.*?<value>(.*?)</value>.*?<(?:sshPrnamt|sshprnamt)>(.*?)</(?:sshPrnamt|sshprnamt)>",
            xml_text,
            re.DOTALL | re.I,
        )

    for entry in entries[:50]:
        name = entry[0].strip()
        shares_str = entry[2].strip().replace(",", "") if len(entry) > 2 else "0"
        shares_n = int(shares_str) if shares_str.isdigit() else 0
        if shares_n > 100_000:
            ticker_guess = _name_to_ticker_guess(name)
            if ticker_guess:
                detail = f"{firm} holds {shares_n:,} shares of {name} (13F filed {filing_date})"
                signals[ticker_guess].append({"source": "13F Whale", "detail": detail, "pts": pts})
                holdings.append({"ticker": ticker_guess, "detail": detail, "pts": pts})

    return holdings


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
            signals[ticker].append({"source": "13D Activist", "detail": detail, "pts": 5})
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

    current_keys = set(current.keys())
    for key, prev_info in prev.items():
        if key not in current_keys and prev_info.get("shares", 0) > 0:
            ticker = prev_info["ticker"]
            fund = prev_info["fund"]
            detail = f"{fund} fully exited {ticker} (was {int(prev_info['shares']):,} shares)"
            sell_signals[ticker].append({"source": "ARK", "detail": detail, "reason": "sell"})
            log.info("ARK full exit detected: %s from %s", ticker, fund)

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
                time.sleep(1)
            except Exception as e:
                log.warning("Public Reddit r/%s failed: %s", sub, e)

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

                time.sleep(1)
            except Exception as e:
                log.warning("Google Trends batch %s failed: %s", batch, e)

        fetched.append("google_trends")
    except Exception as e:
        log.error("Google Trends failed: %s", e)


# ---------------------------------------------------------------------------
# Camillo behavioral signal layer
# ---------------------------------------------------------------------------
def fetch_news_sentiment(signals: dict, sell_signals: dict, fetched: list, watchlist: list):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    log.info("Fetching news sentiment (Camillo layer)...")
    found_any = False

    POS_WORDS = {"upgrade", "partnership", "approval", "record", "beat", "growth",
                 "surge", "buyback", "dividend", "breakthrough"}
    NEG_WORDS = {"downgrade", "miss", "investigation", "recall", "lawsuit",
                 "decline", "loss", "cut", "warning", "fraud"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for ticker in watchlist[:30]:
        try:
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
            r = safe_get(url, timeout=10)
            if not r:
                time.sleep(0.3)
                continue

            try:
                root = ET.fromstring(r.text)
            except Exception as e:
                log.warning("News XML parse failed for %s: %s", ticker, e)
                time.sleep(0.3)
                continue

            net_score = 0
            for item in root.iter("item"):
                pub_el = item.find("pubDate")
                if pub_el is None or not pub_el.text:
                    continue
                try:
                    pub_dt = parsedate_to_datetime(pub_el.text)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    continue

                title_el = item.find("title")
                title = (title_el.text or "").lower() if title_el is not None else ""
                for word in POS_WORDS:
                    if word in title:
                        net_score += 1
                for word in NEG_WORDS:
                    if word in title:
                        net_score -= 1

            if net_score >= 3:
                signals[ticker].append({
                    "source": "News",
                    "detail": f"{ticker}: {net_score} net positive headlines in 24h",
                    "pts": 1,
                })
                found_any = True
            elif net_score <= -3:
                sell_signals[ticker].append({
                    "source": "News Negative",
                    "detail": f"{ticker}: {abs(net_score)} negative headlines — bearish sentiment",
                    "reason": "sell",
                })
                found_any = True

        except Exception as e:
            log.warning("News sentiment failed for %s: %s", ticker, e)

        time.sleep(0.3)

    if found_any:
        fetched.append("news")
    log.info("News sentiment: done")


def fetch_subreddit_growth(signals: dict, fetched: list):
    log.info("Fetching subreddit growth (Camillo layer)...")
    found_any = False

    prev_counts = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            prev_counts = prev_data.get("subreddit_counts", {})
        except Exception:
            pass

    curr_counts = {}

    for subreddit, ticker in SUBREDDIT_TICKER_MAP.items():
        try:
            url = f"https://www.reddit.com/r/{subreddit}/about.json"
            r = safe_get(url, extra_headers={"User-Agent": "watchtower:v1.0 (by watchtower)"})
            if not r:
                continue

            curr_count = r.json().get("data", {}).get("subscribers", 0)
            if not curr_count:
                continue

            curr_counts[subreddit] = curr_count
            prev_count = prev_counts.get(subreddit, 0)

            if prev_count and curr_count > prev_count * 1.05:
                signals[ticker].append({
                    "source": "Subreddit Growth",
                    "detail": f"r/{subreddit} growing fast: {prev_count:,} → {curr_count:,} subscribers",
                    "pts": 1,
                })
                found_any = True

        except Exception as e:
            log.warning("Subreddit growth failed for r/%s: %s", subreddit, e)

    try:
        prev_data = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_data["subreddit_counts"] = curr_counts
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_data))
    except Exception as e:
        log.warning("Could not save subreddit counts: %s", e)

    if found_any:
        fetched.append("subreddit_growth")
    log.info("Subreddit growth: %d subreddits checked", len(curr_counts))


def fetch_job_signals(signals: dict, fetched: list, watchlist: list):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    log.info("Fetching job signals (Camillo layer)...")
    found_any = False

    TICKER_COMPANY_SEARCH = {
        "HIMS": "Hims Hers", "BCHMY": "Bachem", "HOOD": "Robinhood",
        "PLTR": "Palantir", "NVDA": "NVIDIA", "AMZN": "Amazon",
        "TSLA": "Tesla", "MSFT": "Microsoft", "AAPL": "Apple",
    }

    prev_job_counts = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            prev_job_counts = prev_data.get("job_counts", {})
        except Exception:
            pass

    curr_job_counts = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for ticker in watchlist:
        if ticker not in TICKER_COMPANY_SEARCH:
            continue
        company = TICKER_COMPANY_SEARCH[ticker]
        try:
            url = f"https://www.indeed.com/rss?q={requests.utils.quote(company)}&sort=date&limit=25"
            r = safe_get(url, timeout=10)
            if not r:
                time.sleep(1)
                continue

            try:
                root = ET.fromstring(r.text)
            except Exception as e:
                log.warning("Job RSS parse failed for %s: %s", company, e)
                time.sleep(1)
                continue

            current_count = 0
            for item in root.iter("item"):
                pub_el = item.find("pubDate")
                if pub_el is None or not pub_el.text:
                    continue
                try:
                    pub_dt = parsedate_to_datetime(pub_el.text)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt >= cutoff:
                        current_count += 1
                except Exception:
                    continue

            curr_job_counts[ticker] = current_count
            prev_count = prev_job_counts.get(ticker, 0)

            if prev_count > 0 and current_count >= prev_count * 1.5 and current_count >= 5:
                pct = ((current_count - prev_count) / prev_count) * 100
                signals[ticker].append({
                    "source": "Job Growth",
                    "detail": f"{company}: {current_count} postings this week vs {prev_count} last week (+{pct:.0f}%)",
                    "pts": 2,
                })
                found_any = True

        except Exception as e:
            log.warning("Job signals failed for %s: %s", ticker, e)

        time.sleep(1)

    try:
        prev_data = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_data["job_counts"] = curr_job_counts
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_data))
    except Exception as e:
        log.warning("Could not save job counts: %s", e)

    if found_any:
        fetched.append("job_growth")
    log.info("Job signals: %d tickers checked", len(curr_job_counts))


def fetch_short_interest(signals: dict, fetched: list, watchlist: list):
    import re
    log.info("Fetching short interest (Camillo layer)...")
    found_any = False

    prev_short = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            prev_short = prev_data.get("short_interest", {})
        except Exception:
            pass

    curr_short = {}

    for ticker in watchlist[:20]:
        try:
            url = f"https://finviz.com/quote.ashx?t={ticker}"
            r = safe_get(url, extra_headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            if not r:
                time.sleep(1)
                continue

            match = re.search(r'Short Float</td>\s*<td[^>]*>([0-9.]+)%', r.text, re.I)
            if not match:
                match = re.search(r'Short Float[^%]{0,300}?([0-9]+\.[0-9]+)%', r.text, re.I | re.DOTALL)
            if not match:
                log.debug("Short float not found for %s", ticker)
                time.sleep(1)
                continue

            curr = float(match.group(1))
            curr_short[ticker] = curr
            prev = prev_short.get(ticker)

            if prev is not None and curr < prev * 0.80:
                signals[ticker].append({
                    "source": "Short Cover",
                    "detail": f"{ticker}: short interest fell from {prev:.1f}% to {curr:.1f}% — institutions covering",
                    "pts": 2,
                })
                found_any = True

        except Exception as e:
            log.warning("Short interest failed for %s: %s", ticker, e)

        time.sleep(1)

    try:
        prev_data = json.loads(PREVIOUS_DATA_PATH.read_text()) if PREVIOUS_DATA_PATH.exists() else {}
        prev_data["short_interest"] = curr_short
        PREVIOUS_DATA_PATH.write_text(json.dumps(prev_data))
    except Exception as e:
        log.warning("Could not save short interest: %s", e)

    if found_any:
        fetched.append("short_interest")
    log.info("Short interest: %d tickers checked", len(curr_short))


# ---------------------------------------------------------------------------
# Discord daily brief
# ---------------------------------------------------------------------------
def send_daily_brief(tickers_out: list, sell_alerts: list, stats: dict, speculative_solo: set = None):
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log.info("No DISCORD_WEBHOOK_URL set — skipping daily brief")
        return

    if speculative_solo is None:
        speculative_solo = set()

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%a %b %-d")

    sep = "─" * 32
    lines = [
        f"🗼 WATCHTOWER DAILY BRIEF — {date_str}",
        sep,
    ]

    tier1 = [t for t in tickers_out if t["tier"] == 1]
    tier2 = [t for t in tickers_out if t["tier"] == 2]

    def price_emoji(t):
        flag = t.get("price_flag", "neutral")
        if flag == "dip":
            return "📉"
        if flag == "extended":
            return "⚠️"
        return "✅"

    lines.append("🔴 TIER 1 — MAX CONVICTION")
    if tier1:
        for t in tier1:
            top_sigs = [s["detail"] for s in t["signals"][:3] if s.get("pts", 0) > 0]
            sig_str = ", ".join(top_sigs) if top_sigs else "—"
            lines.append(f"• {price_emoji(t)} {t['ticker']} (Score: {t['score']}) — {sig_str}")
    else:
        lines.append("• None today")

    lines.append("")

    lines.append("🟠 TIER 2 — HIGH CONVICTION")
    if tier2:
        for t in tier2[:5]:
            top_sigs = [s["detail"] for s in t["signals"][:2] if s.get("pts", 0) > 0]
            sig_str = ", ".join(top_sigs) if top_sigs else "—"
            lines.append(f"• {price_emoji(t)} {t['ticker']} (Score: {t['score']}) — {sig_str}")
    else:
        lines.append("• None today")

    lines.append("")

    lines.append("🚨 SELL ALERTS")
    if sell_alerts:
        for alert in sell_alerts:
            reasons_str = ", ".join(alert["reasons"])
            lines.append(f"• {alert['ticker']} — {reasons_str}")
    else:
        lines.append("• None today")

    lines.append("")

    if speculative_solo:
        lines.append("👀 Speculative (Reddit-only):")
        lines.append("• " + ", ".join(sorted(speculative_solo)[:10]))
        lines.append("")

    health = stats.get("source_health", {})

    def h(key):
        return "✅" if health.get(key) else "❌"

    lines.append(
        f"📡 Sources: ARK {h('ark')} | F4 {h('form4')} | Congressional {h('congressional')} | "
        f"13F {h('13f')} | Reddit {h('reddit')} | Trends {h('google_trends')}"
    )

    lines.append("")

    sources_active = len(stats.get("sources_fetched", []))
    lines.append(
        f"📊 Stats: {stats['total_scanned']} tickers scanned | "
        f"{stats['tier1_count']} Tier 1 | {stats['tier2_count']} Tier 2 | "
        f"{sources_active} sources active"
    )
    lines.append(sep)
    lines.append("See everything before the market does. 🗼")

    message = "\n".join(lines)

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
def main(mode: str = "full"):
    log.info("=== WATCHTOWER run started — mode: %s ===", mode)

    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    today = date.today()
    schedule = nyse.schedule(start_date=today, end_date=today)
    if schedule.empty:
        log.info("Market closed today (holiday or weekend) — exiting.")
        sys.exit(0)

    start = datetime.now(timezone.utc)

    signals: dict[str, list] = defaultdict(list)
    sell_signals: dict[str, list] = defaultdict(list)
    fetched: list[str] = []

    prev_tickers: dict[str, dict] = {}
    if PREVIOUS_DATA_PATH.exists():
        try:
            prev_data = json.loads(PREVIOUS_DATA_PATH.read_text())
            for t in prev_data.get("tickers", []):
                prev_tickers[t["ticker"]] = t
        except Exception as e:
            log.warning("Could not load previous tickers for comparison: %s", e)

    def _load_ark_universe() -> list:
        if not PREVIOUS_DATA_PATH.exists():
            return []
        try:
            prev = json.loads(PREVIOUS_DATA_PATH.read_text())
            return list({v["ticker"] for v in prev.get("ark_holdings", {}).values() if v.get("ticker")})
        except Exception:
            return []

    if mode == "fast":
        fetch_form4_insiders(signals, fetched)
        ark_universe = _load_ark_universe()
        watchlist = list(set(list(signals.keys()) + ark_universe))[:50]
        fetch_news_sentiment(signals, sell_signals, fetched, watchlist[:30])

    elif mode == "morning":
        fetch_congressional(signals, sell_signals, fetched)
        ark_universe = _load_ark_universe()
        watchlist = list(set(list(signals.keys()) + ark_universe))[:50]
        fetch_short_interest(signals, fetched, watchlist[:20])
        fetch_subreddit_growth(signals, fetched)

    else:  # full
        fetch_congressional(signals, sell_signals, fetched)
        fetch_form4_insiders(signals, fetched)
        fetch_13d(signals, fetched)
        fetch_ark(signals, sell_signals, fetched)
        fetch_reddit(signals, fetched)
        fetch_13f(signals, fetched)

        # Build tracking universe: tickers with any signal + all ARK holdings (for Reddit/Trends scanning)
        # ARK holdings don't score pts but seed the universe so Reddit/Trends can corroborate
        ark_universe = _load_ark_universe()
        watchlist = list(set(list(signals.keys()) + ark_universe))[:50]

        # Camillo behavioral layer
        fetch_news_sentiment(signals, sell_signals, fetched, watchlist[:30])
        fetch_subreddit_growth(signals, fetched)
        fetch_job_signals(signals, fetched, watchlist[:15])
        fetch_short_interest(signals, fetched, watchlist[:20])
        fetch_google_trends(signals, fetched, watchlist[:25])

    # Reddit corroboration rule: zero out Reddit pts for tickers with no other source
    speculative_solo_tickers: set = set()
    for ticker, sigs in signals.items():
        sources = {s["source"] for s in sigs}
        if "Reddit" in sources and all(s == "Reddit" for s in sources):
            speculative_solo_tickers.add(ticker)
            for s in sigs:
                s["pts"] = 0
                s["speculative_solo"] = True
    log.info("Speculative solo (Reddit-only) tickers: %d", len(speculative_solo_tickers))

    log.info("Resolving company names for %d tickers...", len(signals))
    companies = {}
    for ticker in signals:
        companies[ticker] = ticker_to_company(ticker)
        time.sleep(0.1)

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
            "price_flag": "neutral",
            "price_note": "",
        })

    tickers_out.sort(key=lambda x: -x["score"])

    # Mega-cap penalty + price context flags (single yfinance pass per ticker)
    try:
        import yfinance as yf
        for t in tickers_out:
            try:
                yf_obj = yf.Ticker(t["ticker"])
                info = yf_obj.info

                # Mega-cap penalty for tickers scoring >= 3
                if t["score"] >= 3:
                    mc = info.get("marketCap", 0) or 0
                    if mc > 200_000_000_000:
                        t["score"] = max(0, t["score"] - 2)
                        t["signals"].append({
                            "source": "Mega-cap Penalty",
                            "detail": f"Market cap >{mc / 1e9:.0f}B — harder to move needle",
                            "pts": -2,
                        })
                        t["tier"] = assign_tier(t["score"])

                # Price context: RSI + 52-week high
                hist = yf_obj.history(period="3mo")
                if hist.empty or len(hist) < 15:
                    continue

                current_price = float(hist["Close"].iloc[-1])
                week52_high = float(info.get("fiftyTwoWeekHigh") or 0)

                closes = hist["Close"]
                delta = closes.diff()
                gain = delta.clip(lower=0)
                loss = (-delta).clip(lower=0)
                avg_gain = gain.rolling(14).mean()
                avg_loss = loss.rolling(14).mean()
                rs = avg_gain / avg_loss.where(avg_loss != 0)
                rsi_series = 100 - (100 / (1 + rs))
                rsi_clean = rsi_series.dropna()
                rsi = float(rsi_clean.iloc[-1]) if not rsi_clean.empty else 50.0

                if week52_high > 0 and current_price > 0.95 * week52_high and rsi > 70:
                    t["price_flag"] = "extended"
                    t["price_note"] = "⚠️ Near 52-week high, RSI elevated — wait for pullback"
                elif week52_high > 0 and current_price < 0.70 * week52_high:
                    t["price_flag"] = "dip"
                    t["price_note"] = "📉 30%+ off highs — potential entry"

            except Exception as e:
                log.warning("yfinance enrichment failed for %s: %s", t["ticker"], e)
    except ImportError:
        log.warning("yfinance not available — skipping mega-cap penalty and price context")

    tickers_out.sort(key=lambda x: -x["score"])

    curr_map = {t["ticker"]: t for t in tickers_out}
    conviction_tickers = {t["ticker"] for t in tickers_out if t["tier"] in (1, 2)} | \
                         {tk for tk, td in prev_tickers.items() if td.get("tier") in (1, 2)}

    sell_alerts = []
    processed = set()

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

        if prev_score - curr_score >= 3:
            reasons.append(f"Score fell {prev_score}→{curr_score} (-{prev_score - curr_score})")

        for sig in sell_signals.get(ticker, []):
            if sig["source"] == "ARK":
                reasons.append(sig["detail"])

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
        "source_health": {
            "ark": "ark" in fetched,
            "congressional": "congressional" in fetched,
            "form4": "form4" in fetched,
            "13d": "13d" in fetched,
            "13f": "13f" in fetched or "13f_partial" in fetched,
            "reddit": "reddit" in fetched,
            "google_trends": "google_trends" in fetched,
        },
    }

    output = {
        "generated_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "tickers": tickers_out,
        "tier1_alert": tier1,
        "sell_alerts": sell_alerts,
        "speculative_solo": sorted(speculative_solo_tickers),
        "stats": stats,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    log.info("Wrote %s (%d tickers, %d sell alerts)", OUTPUT_PATH, len(tickers_out), len(sell_alerts))

    if mode == "full":
        send_daily_brief(tickers_out, sell_alerts, stats, speculative_solo_tickers)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("=== WATCHTOWER run complete in %.1fs — Tier 1: %s ===", elapsed, tier1 or "none")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="WATCHTOWER signal scanner")
    parser.add_argument("--mode", choices=["fast", "morning", "full"], default="full",
                        help="fast=Form4+news only | morning=congressional+short | full=everything")
    args = parser.parse_args()
    main(mode=args.mode)
