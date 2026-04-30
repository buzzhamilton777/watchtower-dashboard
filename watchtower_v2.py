#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — Pure Camillo Social Arbitrage Engine
Opus (strategic architect) | Hamilton (QC) | Claude Code (builder)
April 29, 2026

Finds cultural/consumer trends accelerating in social data BEFORE Wall Street
prices them in. Maps trends to companies. Alerts K when conviction is high.

Usage:
  python3 watchtower_v2.py --mode fast      # Google Trends + Reddit only (~45s)
  python3 watchtower_v2.py --mode morning   # All signals + thesis generation
  python3 watchtower_v2.py --mode full      # Everything + full delta comparison
"""

import argparse
import json
import logging
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import yfinance as yf

load_dotenv()

# Local scanner modules
try:
    from scanner_autocomplete import scan_amazon_autocomplete
    from scanner_tiktok import scan_tiktok, is_available as tiktok_available
except ImportError as e:
    log.warning(f"Scanner module import failed: {e}")
    def scan_amazon_autocomplete(trend_keywords, previous): return {}
    def scan_tiktok(trend_keywords, previous): return {}
    def tiktok_available(): return False

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/watchtower.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
THESIS_DIR = DATA_DIR / "thesis"
LOGS_DIR = BASE_DIR / "logs"
MAPPER_PATH = BASE_DIR / "mapper.json"
DATA_PATH = DATA_DIR / "watchtower-data.json"
PREV_PATH = DATA_DIR / "watchtower-previous.json"

for d in [DATA_DIR, THESIS_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Config ────────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "watchtower:v2.0")

DASHBOARD_URL = "https://buzzhamilton777.github.io/watchtower-dashboard/"

# Subreddits to monitor for keyword migration
TARGET_SUBREDDITS = [
    "biohacking", "peptides", "longevity", "supplements", "fitness",
    "pickleball", "skincareaddiction", "solotravel", "DIY", "nootropics",
    "Testosterone", "intermittentfasting", "investing", "stocks", "wallstreetbets",
    "personalfinance", "nutrition", "weightloss", "bodybuilding", "running",
    "financialindependence", "dividends", "options", "hiking", "trailrunning",
    "solarenergy", "testosterone", "microbiome"
]

# Exit signal: if absolute Google Trends score exceeds this, trend is going mainstream
MAINSTREAM_THRESHOLD = 75

# Scoring thresholds
TIER1_THRESHOLD = 8
TIER2_THRESHOLD = 5
TIER3_THRESHOLD = 3

# ─── Load Data ─────────────────────────────────────────────────────────────────

def load_mapper() -> dict:
    with open(MAPPER_PATH) as f:
        return json.load(f)

def load_previous() -> dict:
    if PREV_PATH.exists():
        with open(PREV_PATH) as f:
            return json.load(f)
    return {}

# ─── Scanner 1: Google Trends ──────────────────────────────────────────────────

def scan_google_trends(keywords: list[str], previous: dict) -> dict:
    """
    Scans Google Trends for keyword rate-of-change.
    Returns dict of keyword -> signal data.
    Signal fires when: ratio > 2.0 AND new 90-day high
    Also pulls Rising Queries for trend discovery.
    """
    log.info("Google Trends scanner starting...")
    results = {}
    discovery = []

    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
    except ImportError:
        log.error("pytrends not installed. Run: pip install pytrends")
        return results

    # Scan known keywords in batches of 3 (smaller batches = fewer 429s)
    batch_size = 3
    consecutive_429s = 0
    MAX_429s = 2  # 2-strike rule: bail after 2 consecutive 429s — don't hang forever
    for i in range(0, len(keywords), batch_size):
        if consecutive_429s >= MAX_429s:
            log.warning(f"Google Trends: {consecutive_429s} consecutive 429s — skipping remaining batches to avoid hang")
            break
        batch = keywords[i:i + batch_size]
        try:
            pytrends.build_payload(batch, timeframe="today 3-m", geo="US")
            df = pytrends.interest_over_time()
            consecutive_429s = 0  # Reset on success
            time.sleep(180)  # 3-min delay between batches — afternoon-only policy, avoid 429s

            # Google Shopping trends (gprop='froogle') — consumer purchase intent
            shopping_df = None
            try:
                pytrends.build_payload(batch, timeframe="today 3-m", geo="US", gprop="froogle")
                shopping_df = pytrends.interest_over_time()
                time.sleep(15)
            except Exception as shop_e:
                log.warning(f"  Google Shopping Trends failed for {batch}: {shop_e}")

            for kw in batch:
                if kw not in df.columns:
                    continue
                series = df[kw].dropna()
                if len(series) < 4:
                    continue

                current = float(series.iloc[-1])
                avg_12w = float(series.iloc[-13:-1].mean()) if len(series) >= 13 else float(series[:-1].mean())
                is_90d_high = current == series.max() and current > 0
                ratio = current / avg_12w if avg_12w > 0 else 0

                # Score (web search)
                score = 0
                if ratio > 2.0 and is_90d_high:
                    score = 3
                elif ratio > 1.5 and is_90d_high:
                    score = 1

                # Google Shopping boost — purchase intent is stronger signal
                shopping_score = 0
                shopping_ratio = 0.0
                if shopping_df is not None and kw in shopping_df.columns:
                    shop_series = shopping_df[kw].dropna()
                    if len(shop_series) >= 4:
                        shop_current = float(shop_series.iloc[-1])
                        shop_avg = float(shop_series.iloc[:-1].mean()) if len(shop_series) > 1 else 1
                        shopping_ratio = shop_current / shop_avg if shop_avg > 0 else 0
                        if shopping_ratio > 2.0:
                            shopping_score = 2
                        elif shopping_ratio > 1.5:
                            shopping_score = 1
                        if shopping_score > 0 and score > 0:
                            score = min(score + 1, 3)  # Confirmation bonus

                # Exit warning check
                is_mainstream = current > MAINSTREAM_THRESHOLD

                results[kw] = {
                    "score": score,
                    "current": current,
                    "avg_12w": round(avg_12w, 1),
                    "ratio": round(ratio, 2),
                    "is_90d_high": is_90d_high,
                    "is_mainstream": is_mainstream,
                    "shopping_ratio": round(shopping_ratio, 2),
                    "shopping_score": shopping_score,
                }

                if score > 0:
                    log.info(f"  Google Trends: {kw} | ratio={ratio:.2f} | shopping_ratio={shopping_ratio:.2f} | score={score}")

        except Exception as e:
            log.warning(f"Google Trends batch {batch} failed: {e}")
            if "429" in str(e):
                consecutive_429s += 1
                # Exponential backoff on rate limit
                wait = 90
                log.info(f"  Rate limited by Google — waiting {wait}s (exponential backoff) [{consecutive_429s}/{MAX_429s}]")
                time.sleep(wait)
                # Retry once with smaller batch (single keyword)
                for single_kw in batch[:1]:
                    try:
                        pytrends.build_payload([single_kw], timeframe="today 3-m", geo="US")
                        df_retry = pytrends.interest_over_time()
                        time.sleep(30)
                        if single_kw in df_retry.columns:
                            series = df_retry[single_kw].dropna()
                            if len(series) >= 4:
                                current = float(series.iloc[-1])
                                avg_12w = float(series.iloc[-13:-1].mean()) if len(series) >= 13 else float(series[:-1].mean())
                                ratio = current / avg_12w if avg_12w > 0 else 0
                                is_90d_high = current == series.max() and current > 0
                                score = 0
                                if ratio > 2.0 and is_90d_high:
                                    score = 3
                                elif ratio > 1.5 and is_90d_high:
                                    score = 1
                                results[single_kw] = {
                                    "score": score, "current": current,
                                    "avg_12w": round(avg_12w, 1), "ratio": round(ratio, 2),
                                    "is_90d_high": is_90d_high,
                                    "is_mainstream": current > MAINSTREAM_THRESHOLD,
                                    "shopping_ratio": 0.0, "shopping_score": 0,
                                    "from_retry": True,
                                }
                    except Exception:
                        pass
            else:
                time.sleep(15)

    # Discovery: pull rising queries in key categories
    try:
        for category in ["health", "sports", "food & drink", "hobbies & leisure"]:
            pytrends.build_payload([""], timeframe="today 1-m", geo="US")
            time.sleep(5)
            # Note: rising queries discovery via related_queries on broad terms
            pytrends.build_payload(["wellness", "fitness", "health supplement"], timeframe="today 1-m", geo="US")
            related = pytrends.related_queries()
            time.sleep(6)
            for kw in ["wellness", "fitness", "health supplement"]:
                if kw in related and related[kw].get("rising") is not None:
                    rising_df = related[kw]["rising"]
                    if rising_df is not None and not rising_df.empty:
                        top_rising = rising_df.head(5)["query"].tolist()
                        for term in top_rising:
                            if term not in keywords and len(term) > 3:
                                discovery.append(term)
            break  # Just one pass for now
    except Exception as e:
        log.warning(f"Rising queries discovery failed: {e}")

    log.info(f"Google Trends complete. {sum(1 for v in results.values() if v['score'] > 0)} signals fired.")
    return {"signals": results, "discovery": list(set(discovery))}


# ─── Scanner 2: Reddit Keyword Velocity ────────────────────────────────────────

def scan_reddit(trend_keywords: dict[str, list[str]], previous: dict) -> dict:
    """
    Scans Reddit for keyword velocity across target subreddits.
    Tracks keyword migration (novelty filter) — keyword appearing in
    subreddits it doesn't normally appear in is the real signal.
    """
    log.info("Reddit scanner starting...")
    results = {}

    # Try PRAW first, fall back to public HTTP
    reddit = None
    if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        try:
            import praw
            reddit = praw.Reddit(
                client_id=REDDIT_CLIENT_ID,
                client_secret=REDDIT_CLIENT_SECRET,
                user_agent=REDDIT_USER_AGENT,
            )
            log.info("  Using PRAW authenticated Reddit access")
        except Exception as e:
            log.warning(f"  PRAW init failed: {e}, falling back to public HTTP")

    # Collect posts from subreddits
    subreddit_posts: dict[str, list[str]] = {}
    headers = {"User-Agent": "watchtower:v2.0 (research bot)"}

    consecutive_reddit_failures = 0
    MAX_REDDIT_FAILURES = 2  # 2-strike rule
    for sub in TARGET_SUBREDDITS:
        if consecutive_reddit_failures >= MAX_REDDIT_FAILURES:
            log.warning(f"Reddit: {consecutive_reddit_failures} consecutive failures — stopping scanner early")
            break
        texts = []
        try:
            if reddit:
                subreddit = reddit.subreddit(sub)
                for post in subreddit.new(limit=100):
                    texts.append((post.title + " " + (post.selftext or "")).lower())
            else:
                url = f"https://www.reddit.com/r/{sub}/new.json?limit=100"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    posts = resp.json().get("data", {}).get("children", [])
                    for p in posts:
                        d = p.get("data", {})
                        texts.append((d.get("title", "") + " " + d.get("selftext", "")).lower())
                time.sleep(1)
            consecutive_reddit_failures = 0  # reset on success
        except Exception as e:
            consecutive_reddit_failures += 1
            log.warning(f"  Reddit r/{sub} failed [{consecutive_reddit_failures}/{MAX_REDDIT_FAILURES}]: {e}")

        if texts:
            subreddit_posts[sub] = texts

    # Count keyword frequency per subreddit
    prev_reddit = previous.get("reddit_counts", {})
    kw_subreddit_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for trend_name, keywords in trend_keywords.items():
        for sub, texts in subreddit_posts.items():
            count = sum(
                sum(1 for text in texts if kw.lower() in text)
                for kw in keywords
            )
            kw_subreddit_counts[trend_name][sub] = count

    # Score each trend
    for trend_name, sub_counts in kw_subreddit_counts.items():
        total_7d = sum(sub_counts.values())
        prev_avg = prev_reddit.get(trend_name, {}).get("avg_30d", 5)

        ratio = total_7d / prev_avg if prev_avg > 0 else 0
        score = 0
        if ratio > 2.0 and total_7d >= 3:
            score = 2
        elif ratio > 1.5 and total_7d >= 2:
            score = 1

        # Novelty filter: keyword appearing in 3+ "non-native" subreddits
        non_native_subs = ["investing", "stocks", "personalfinance", "fitness",
                           "running", "solotravel", "DIY", "intermittentfasting"]
        non_native_hits = sum(1 for sub in non_native_subs if sub_counts.get(sub, 0) > 0)
        novelty_bonus = non_native_hits >= 3

        # High financial community activity = exit warning
        financial_subs = ["investing", "stocks", "wallstreetbets", "personalfinance"]
        financial_activity = sum(sub_counts.get(s, 0) for s in financial_subs)

        results[trend_name] = {
            "score": score,
            "total_7d": total_7d,
            "avg_30d": prev_avg,
            "ratio": round(ratio, 2),
            "novelty_bonus": novelty_bonus,
            "non_native_hits": non_native_hits,
            "financial_activity": financial_activity,
            "subreddit_breakdown": dict(sub_counts),
        }

        if score > 0 or novelty_bonus:
            log.info(f"  Reddit: {trend_name} | total_7d={total_7d} | ratio={ratio:.2f} | novelty={novelty_bonus} | score={score}")

    log.info(f"Reddit complete. {sum(1 for v in results.values() if v['score'] > 0)} signals fired.")
    return results


# ─── Scanner 3: Amazon BSR ─────────────────────────────────────────────────────

def scan_amazon_bsr(trend_keywords: dict[str, list[str]], previous: dict) -> dict:
    """
    Scrapes Amazon bestseller pages and Movers & Shakers.
    Looks for products climbing fast in BSR.
    FRAGILE: wraps all errors, returns empty on failure.
    """
    log.info("Amazon BSR scanner starting...")
    results = {}
    prev_bsr = previous.get("amazon_bsr", {})

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    category_urls = [
        ("health", "https://www.amazon.com/best-sellers-health-personal-care/zgbs/hpc/"),
        ("sports", "https://www.amazon.com/Best-Sellers-Sports-Fitness/zgbs/sporting-goods/"),
        ("grocery", "https://www.amazon.com/Best-Sellers-Grocery-Gourmet-Food/zgbs/grocery/"),
        ("beauty", "https://www.amazon.com/Best-Sellers-Beauty/zgbs/beauty/"),
    ]

    movers_urls = [
        ("health", "https://www.amazon.com/gp/movers-and-shakers/hpc/"),
        ("sports", "https://www.amazon.com/gp/movers-and-shakers/sporting-goods/"),
        ("grocery", "https://www.amazon.com/gp/movers-and-shakers/grocery/"),
    ]

    # Collect product names from BSR pages
    product_texts: list[str] = []

    consecutive_bsr_failures = 0
    MAX_BSR_FAILURES = 2  # 2-strike rule
    for cat_name, url in category_urls + movers_urls:
        if consecutive_bsr_failures >= MAX_BSR_FAILURES:
            log.warning(f"Amazon BSR: {consecutive_bsr_failures} consecutive failures — stopping scanner early")
            break
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                consecutive_bsr_failures += 1
                log.warning(f"  Amazon BSR {cat_name} HTTP {resp.status_code} [{consecutive_bsr_failures}/{MAX_BSR_FAILURES}]")
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            # Extract product titles
            for tag in soup.find_all(["span", "div", "a"], class_=lambda c: c and ("p13n-sc-truncate" in c or "a-size-small" in c or "zg-item" in c)):
                text = tag.get_text(strip=True).lower()
                if len(text) > 5:
                    product_texts.append(text)
            time.sleep(2)
            consecutive_bsr_failures = 0  # reset on success
        except Exception as e:
            consecutive_bsr_failures += 1
            log.warning(f"  Amazon BSR {cat_name} failed [{consecutive_bsr_failures}/{MAX_BSR_FAILURES}]: {e}")

    # Match product texts against trend keywords
    for trend_name, keywords in trend_keywords.items():
        matches = []
        for text in product_texts:
            for kw in keywords:
                if kw.lower() in text:
                    matches.append(text[:80])
                    break

        score = 0
        if len(matches) >= 5:
            score = 3
        elif len(matches) >= 3:
            score = 2
        elif len(matches) >= 1:
            score = 1

        results[trend_name] = {
            "score": score,
            "match_count": len(matches),
            "sample_products": matches[:3],
        }

        if score > 0:
            log.info(f"  Amazon BSR: {trend_name} | matches={len(matches)} | score={score}")

    log.info(f"Amazon BSR complete. {sum(1 for v in results.values() if v['score'] > 0)} signals fired.")
    return results


# ─── Exit Signal Detection ──────────────────────────────────────────────────────

def detect_exit_signals(
    trend_name: str,
    gt_signals: dict,
    reddit_signals: dict,
    bsr_signals: dict,
    previous: dict,
    mapper: dict,
) -> dict | None:
    """
    Detect trend plateau/decline signals that suggest the arbitrage window
    is closing. Three independent detectors:
    1. Google Trends decline: current < avg AND ratio < 0.8
    2. Reddit financial migration: financial subreddit activity spiking
    3. Amazon BSR plateau: BSR match count stable/declining after prior growth
    """
    exit_signals = []
    exit_score = 0

    mapper_entry = mapper.get(trend_name, {})
    keywords = mapper_entry.get("keywords", [trend_name])
    prev_signals = previous.get("active_signals", {})
    prev_trend = None
    if isinstance(prev_signals, list):
        for s in prev_signals:
            if s.get("trend_name") == trend_name:
                prev_trend = s
                break
    elif isinstance(prev_signals, dict):
        prev_trend = prev_signals.get(trend_name)

    try:
        # ── Detector 1: Google Trends Decline ──
        gt = gt_signals.get("signals", {})
        for kw in keywords:
            if kw in gt:
                gt_data = gt[kw]
                current = gt_data.get("current", 0)
                avg_12w = gt_data.get("avg_12w", 0)
                ratio = gt_data.get("ratio", 1)
                is_mainstream = gt_data.get("is_mainstream", False)

                if current > 0 and avg_12w > 0 and ratio < 0.8:
                    exit_signals.append({
                        "type": "google_trends_decline",
                        "keyword": kw,
                        "detail": f"GT ratio {ratio:.2f} (current {current} vs avg {avg_12w})",
                        "severity": "high" if ratio < 0.6 else "medium",
                    })
                    exit_score += 2 if ratio < 0.6 else 1

                if is_mainstream:
                    exit_signals.append({
                        "type": "mainstream_saturation",
                        "keyword": kw,
                        "detail": f"GT absolute score {current} exceeds mainstream threshold {MAINSTREAM_THRESHOLD}",
                        "severity": "high",
                    })
                    exit_score += 2
                break

        # ── Detector 2: Reddit Financial Migration ──
        reddit_data = reddit_signals.get(trend_name, {})
        financial_activity = reddit_data.get("financial_activity", 0)
        total_7d = reddit_data.get("total_7d", 0)

        if total_7d > 0 and financial_activity > 0:
            financial_ratio = financial_activity / total_7d
            if financial_ratio > 0.4 and financial_activity >= 5:
                exit_signals.append({
                    "type": "financial_migration",
                    "detail": f"Financial sub activity {financial_activity}/{total_7d} ({financial_ratio:.0%}) — Wall Street awareness high",
                    "severity": "high",
                })
                exit_score += 2
            elif financial_ratio > 0.25 and financial_activity >= 3:
                exit_signals.append({
                    "type": "financial_migration",
                    "detail": f"Financial sub activity {financial_activity}/{total_7d} ({financial_ratio:.0%}) — awareness growing",
                    "severity": "medium",
                })
                exit_score += 1

        # ── Detector 3: BSR Plateau ──
        bsr_data = bsr_signals.get(trend_name, {})
        current_bsr_matches = bsr_data.get("match_count", 0)
        if prev_trend:
            prev_bsr_matches = prev_trend.get("amazon_bsr", {}).get("match_count", 0)
            days_active = prev_trend.get("days_active", 0)

            if days_active >= 14 and prev_bsr_matches > 0:
                if current_bsr_matches <= prev_bsr_matches and prev_bsr_matches >= 3:
                    exit_signals.append({
                        "type": "bsr_plateau",
                        "detail": f"BSR matches {current_bsr_matches} (was {prev_bsr_matches}) after {days_active} days active",
                        "severity": "medium",
                    })
                    exit_score += 1

    except Exception as e:
        log.warning(f"  Exit signal detection failed for {trend_name}: {e}")

    if not exit_signals:
        return None

    if exit_score >= 4:
        urgency = "EXIT_RECOMMENDED"
    elif exit_score >= 2:
        urgency = "CONSIDER_EXIT"
    else:
        urgency = "WATCH"

    return {
        "trend_name": trend_name,
        "exit_score": exit_score,
        "urgency": urgency,
        "signals": exit_signals,
        "detector_count": len(exit_signals),
    }


# ─── Company Mapper ─────────────────────────────────────────────────────────────

def map_companies(trend_name: str, mapper: dict) -> dict:
    """Fetches company data for a trend from mapper.json + yfinance."""
    if trend_name not in mapper:
        return {"direct": [], "picks_and_shovels": []}

    entry = mapper[trend_name]
    result = {"direct": [], "picks_and_shovels": []}

    def enrich_ticker(ticker_entry: dict) -> dict:
        ticker = ticker_entry["ticker"]
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            hist = t.history(period="3mo")
            price = info.last_price if hasattr(info, 'last_price') else None
            if price is None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
            change_30d = None
            change_90d = None
            if not hist.empty and len(hist) >= 2:
                if len(hist) >= 22:
                    change_30d = round((hist["Close"].iloc[-1] / hist["Close"].iloc[-22] - 1) * 100, 1)
                if len(hist) >= 60:
                    change_90d = round((hist["Close"].iloc[-1] / hist["Close"].iloc[-60] - 1) * 100, 1)
            return {
                "ticker": ticker,
                "thesis": ticker_entry.get("thesis", ""),
                "price": round(price, 2) if price else None,
                "change_30d": change_30d,
                "change_90d": change_90d,
                "valid": True,
            }
        except Exception as e:
            log.warning(f"  yfinance {ticker} failed: {e}")
            return {
                "ticker": ticker,
                "thesis": ticker_entry.get("thesis", ""),
                "price": None,
                "change_30d": None,
                "change_90d": None,
                "valid": False,
            }

    for te in entry.get("direct", []):
        result["direct"].append(enrich_ticker(te))

    for te in entry.get("picks_and_shovels", []):
        result["picks_and_shovels"].append(enrich_ticker(te))

    return result


# ─── Scoring Engine ─────────────────────────────────────────────────────────────

def score_trend(
    trend_name: str,
    gt_signals: dict,
    reddit_signals: dict,
    bsr_signals: dict,
    autocomplete_signals: dict,
    mapper: dict,
    previous: dict,
) -> dict | None:
    """
    Aggregates scanner signals into a final score.
    Applies bonuses, penalties, and time decay.
    """
    gt = gt_signals.get("signals", {})
    reddit = reddit_signals
    bsr = bsr_signals

    # Find matching Google Trends keyword
    mapper_entry = mapper.get(trend_name, {})
    keywords = mapper_entry.get("keywords", [trend_name])

    gt_score = 0
    gt_data = {}
    for kw in keywords:
        if kw in gt:
            if gt[kw]["score"] > gt_score:
                gt_score = gt[kw]["score"]
                gt_data = gt[kw]

    reddit_score = reddit.get(trend_name, {}).get("score", 0)
    reddit_data = reddit.get(trend_name, {})

    bsr_score = bsr.get(trend_name, {}).get("score", 0)
    bsr_data = bsr.get(trend_name, {})

    autocomplete_score = autocomplete_signals.get(trend_name, {}).get("score", 0)
    autocomplete_data = autocomplete_signals.get(trend_name, {})

    raw_score = gt_score + reddit_score + bsr_score + autocomplete_score

    if raw_score == 0:
        return None

    # Bonuses
    platforms_firing = sum([gt_score > 0, reddit_score > 0, bsr_score > 0, autocomplete_score > 0])
    multi_platform_bonus = 3 if platforms_firing >= 2 else 0

    # Wall Street awareness (use GT absolute score as proxy)
    gt_absolute = gt_data.get("current", 0)
    gt_has_real_data = bool(gt_data)  # Empty dict = GT was rate-limited or returned nothing
    # Only grant low-awareness bonus if GT actually returned data confirming low awareness
    # If GT was rate-limited (gt_absolute=0 due to no data), don't assume low awareness
    low_awareness_bonus = 2 if (gt_has_real_data and gt_absolute < 30) else 0

    # Penalties
    is_mainstream = gt_data.get("is_mainstream", False) or gt_absolute > MAINSTREAM_THRESHOLD
    mainstream_penalty = -3 if is_mainstream else 0

    financial_reddit = reddit_data.get("financial_activity", 0)
    financial_penalty = -2 if financial_reddit > 20 else 0

    # Novelty bonus
    novelty_bonus = 1 if reddit_data.get("novelty_bonus", False) else 0

    final_score = raw_score + multi_platform_bonus + low_awareness_bonus + mainstream_penalty + financial_penalty + novelty_bonus
    final_score = max(0, min(10, final_score))

    # Time decay
    # Build a dict from the active_signals list for lookup
    _prev_active_list = previous.get("active_signals", [])
    if isinstance(_prev_active_list, list):
        prev_signals = {s.get("trend_name"): s for s in _prev_active_list if isinstance(s, dict)}
    else:
        prev_signals = _prev_active_list
    signal_first_seen = prev_signals.get(trend_name, {}).get("signal_first_seen", date.today().isoformat())
    days_active = prev_signals.get(trend_name, {}).get("days_active", 1)
    peak_score = max(final_score, prev_signals.get(trend_name, {}).get("peak_score", 0))

    # Decay for sustained but weakening signals
    if days_active > 7 and raw_score <= 2:
        final_score = max(0, final_score - 1)

    # Determine tier
    # Tier 1 requires 2+ days of multi-platform firing to prevent day-1 false urgency
    tier1_confirmed = platforms_firing >= 2 and days_active >= 2
    if final_score >= TIER1_THRESHOLD and tier1_confirmed:
        tier = 1
    elif final_score >= TIER1_THRESHOLD:
        # Score qualifies for Tier 1 but not yet confirmed — hold at Tier 2
        tier = 2
        log.info(f"  {trend_name}: Tier 1 score ({final_score}) but day {days_active} — holding at Tier 2 (needs 2+ days)")
    elif final_score >= TIER2_THRESHOLD:
        tier = 2
    elif final_score >= TIER3_THRESHOLD:
        tier = 3
    else:
        return None

    # Wall Street awareness label
    if gt_absolute > 60 or financial_reddit > 15:
        ws_awareness = "HIGH"
    elif gt_absolute > 35 or financial_reddit > 8:
        ws_awareness = "MEDIUM"
    else:
        ws_awareness = "LOW"

    signals_firing = []
    if gt_score > 0:
        signals_firing.append("google_trends")
    if reddit_score > 0:
        signals_firing.append("reddit")
    if bsr_score > 0:
        signals_firing.append("amazon_bsr")
    if autocomplete_score > 0:
        signals_firing.append("amazon_autocomplete")

    return {
        "trend_name": trend_name,
        "display_name": trend_name.replace("_", " ").title(),
        "tier": tier,
        "score": final_score,
        "score_breakdown": {
            "google_trends": gt_score,
            "reddit": reddit_score,
            "amazon_bsr": bsr_score,
            "amazon_autocomplete": autocomplete_score,
            "multi_platform_bonus": multi_platform_bonus,
            "low_awareness_bonus": low_awareness_bonus,
            "novelty_bonus": novelty_bonus,
            "mainstream_penalty": mainstream_penalty,
            "financial_penalty": financial_penalty,
        },
        "signals_firing": signals_firing,
        "signal_first_seen": signal_first_seen,
        "days_active": days_active,
        "peak_score": peak_score,
        "google_trends": gt_data,
        "reddit": reddit_data,
        "amazon_bsr": bsr_data,
        "amazon_autocomplete": autocomplete_data,
        "wall_street_awareness": ws_awareness,
        "exit_warning": is_mainstream,
        "category": mapper_entry.get("category", ""),
        "description": mapper_entry.get("description", ""),
    }


# ─── Thesis Generation (Opus) ───────────────────────────────────────────────────

def generate_thesis(signal: dict, companies: dict) -> str | None:
    """Calls Opus via Anthropic API to generate investment thesis for Tier 1 hits."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — skipping thesis generation")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        direct_companies = ", ".join(
            f"{c['ticker']} (${c['price']})" for c in companies.get("direct", []) if c.get("price")
        )
        ps_companies = ", ".join(
            f"{c['ticker']} (${c['price']})" for c in companies.get("picks_and_shovels", []) if c.get("price")
        )

        prompt = f"""You are an investment thesis writer using Chris Camillo's social arbitrage methodology.

Trend: {signal['display_name']}
Score: {signal['score']}/10 | Tier {signal['tier']}
Day {signal['days_active']} of signal

Signals firing:
- Google Trends: ratio={signal['google_trends'].get('ratio', 'N/A')}, current={signal['google_trends'].get('current', 'N/A')}, 90d_high={signal['google_trends'].get('is_90d_high', False)}
- Reddit: total_7d={signal['reddit'].get('total_7d', 0)}, ratio={signal['reddit'].get('ratio', 'N/A')}, novelty_crossover={signal['reddit'].get('novelty_bonus', False)}
- Amazon BSR: matches={signal['amazon_bsr'].get('match_count', 0)}

Wall Street awareness: {signal['wall_street_awareness']}
Description: {signal['description']}

Direct plays: {direct_companies or 'None identified'}
Picks & shovels: {ps_companies or 'None identified'}

Write a sharp, actionable investment thesis in this exact format:

TREND: {signal['display_name']}
SCORE: {signal['score']}/10 | TIER {signal['tier']} | Day {signal['days_active']} of signal

WHAT'S HAPPENING:
[2-3 sentences. Plain English. What behavior change is occurring and why it matters.]

SIGNALS FIRING:
[Bullet list of what fired and at what level]

PLAYS:
Direct: [ticker] — [1-line thesis why they benefit]
Picks & shovels: [ticker] — [1-line thesis why they win regardless of brand winner]

WALL STREET AWARENESS: {signal['wall_street_awareness']}
[1 sentence on why the window is open or closing]

ESTIMATED WINDOW: [X months estimate before mainstream]

CAMILLO FILTER:
☑/☐ Durable behavior change (not a fad)
☑/☐ Demographic with spending power
☑/☐ Under Wall Street radar
☑/☐ Public company exposure exists

Be direct. No hedging. K is making real investment decisions from this."""

        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text

    except Exception as e:
        log.error(f"Thesis generation failed: {e}")
        return None


def save_thesis(signal: dict, thesis_text: str, companies: dict) -> str:
    """Saves thesis to file and returns filename."""
    top_ticker = ""
    if companies.get("picks_and_shovels"):
        top_ticker = companies["picks_and_shovels"][0]["ticker"]
    elif companies.get("direct"):
        top_ticker = companies["direct"][0]["ticker"]

    filename = f"{date.today().isoformat()}_{signal['trend_name']}_{top_ticker}.md"
    filepath = THESIS_DIR / filename

    with open(filepath, "w") as f:
        f.write(f"# WATCHTOWER TIER {signal['tier']} — {signal['display_name'].upper()}\n")
        f.write(f"*Generated: {datetime.now().isoformat()}*\n\n")
        f.write(thesis_text)

    log.info(f"Thesis saved: {filename}")
    return str(filename)


# ─── Discord Alert ──────────────────────────────────────────────────────────────

def send_discord_alert(signal: dict, companies: dict, thesis: str | None):
    """Fires Discord webhook for Tier 1 hits."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return

    # Build company lines
    direct_lines = "\n".join(
        f"📈 Direct: **{c['ticker']}** ${c['price']} ({c['change_30d']:+.1f}% 30d)" if c.get("price") and c.get("change_30d") is not None
        else f"📈 Direct: **{c['ticker']}**"
        for c in companies.get("direct", [])[:2]
    )
    ps_lines = "\n".join(
        f"⚒️ Picks & shovels: **{c['ticker']}** ${c['price']} ({c['change_30d']:+.1f}% 30d)" if c.get("price") and c.get("change_30d") is not None
        else f"⚒️ Picks & shovels: **{c['ticker']}**"
        for c in companies.get("picks_and_shovels", [])[:2]
    )

    # Awareness indicator
    awareness_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(signal["wall_street_awareness"], "⚪")

    # Thesis preview
    thesis_preview = ""
    if thesis:
        lines = thesis.split("\n")
        for line in lines:
            if line.startswith("WHAT'S HAPPENING:"):
                idx = lines.index(line)
                if idx + 1 < len(lines):
                    thesis_preview = f"\n_{lines[idx+1][:200]}_"
                break

    content = f"""🗼 **WATCHTOWER TIER {signal['tier']} ALERT**
**{signal['display_name'].upper()}** | Score: {signal['score']}/10 | Day {signal['days_active']}

{direct_lines}
{ps_lines}

{awareness_icon} Wall St awareness: **{signal['wall_street_awareness']}**
🔗 {DASHBOARD_URL}{thesis_preview}"""

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        if resp.status_code in [200, 204]:
            log.info("Discord alert sent successfully")
        else:
            log.warning(f"Discord alert failed: {resp.status_code}")
    except Exception as e:
        log.error(f"Discord alert error: {e}")


# ─── Git Commit ─────────────────────────────────────────────────────────────────

def commit_data():
    """Commits updated data files to git."""
    try:
        subprocess.run(["git", "config", "user.name", "WATCHTOWER Bot"], cwd=BASE_DIR, capture_output=True)
        subprocess.run(["git", "config", "user.email", "watchtower@hamilton"], cwd=BASE_DIR, capture_output=True)
        subprocess.run(["git", "add", "data/", "logs/"], cwd=BASE_DIR, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--staged", "--quiet"],
            cwd=BASE_DIR, capture_output=True
        )
        if result.returncode != 0:
            tag = datetime.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(
                ["git", "commit", "-m", f"WATCHTOWER 2.0 data update {tag}"],
                cwd=BASE_DIR, capture_output=True
            )
            subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True)
            log.info("Data committed and pushed to GitHub")
        else:
            log.info("No data changes to commit")
    except Exception as e:
        log.error(f"Git commit failed: {e}")


# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WATCHTOWER 2.0 — Camillo Social Arbitrage Engine")
    parser.add_argument("--mode", choices=["fast", "morning", "full"], default="morning",
                        help="Scan mode: fast (GT+Reddit), morning (all), full (all+delta)")
    args = parser.parse_args()

    start = time.time()
    log.info(f"=== WATCHTOWER 2.0 run started — mode: {args.mode} ===")

    mapper = load_mapper()
    previous = load_previous()

    trend_names = list(mapper.keys())
    all_keywords = []
    trend_keywords = {}
    for trend, entry in mapper.items():
        kws = entry.get("keywords", [trend])
        trend_keywords[trend] = kws
        # Fast mode: only scan primary keyword (first in list) to avoid rate limits
        if args.mode == "fast":
            all_keywords.append(kws[0])
        else:
            all_keywords.extend(kws)

    # ── Run Scanners ──────────────────────────────────────────────────────────

    # GT runs only in full mode — once-daily at 2:15 PM CST (April 30, 2026 decision)
    # All 4 scanners run together in full mode for cleanest thesis signal
    # Revisit ~June 1: consider SerpAPI (~$50/mo) if faster detection ever needed
    if args.mode == "full":
        gt_output = scan_google_trends(list(set(all_keywords)), previous)
    else:
        log.info(f"{args.mode.capitalize()} mode: skipping Google Trends (full-mode only policy)")
        gt_output = {"signals": {}, "discovery": []}

    reddit_signals = scan_reddit(trend_keywords, previous)

    if args.mode in ["morning", "full"]:
        bsr_signals = scan_amazon_bsr(trend_keywords, previous)
    else:
        bsr_signals = {t: {"score": 0, "match_count": 0, "sample_products": []} for t in trend_names}

    # Amazon Autocomplete — runs in all modes (lightweight)
    try:
        autocomplete_signals = scan_amazon_autocomplete(trend_keywords, previous)
    except Exception as e:
        log.warning(f"Autocomplete scanner error: {e}")
        autocomplete_signals = {}

    # TikTok — morning/full only to conserve daily quota
    tiktok_signals = {}
    if args.mode in ["morning", "full"]:
        try:
            tiktok_signals = scan_tiktok(trend_keywords, previous)
        except Exception as e:
            log.warning(f"TikTok scanner error: {e}")

    # ── Score Each Trend ──────────────────────────────────────────────────────

    active_signals = []
    exit_warnings = []
    prev_signals_dict = {s.get("trend_name"): s for s in previous.get("active_signals", [])}

    for trend_name in trend_names:
        scored = score_trend(trend_name, gt_output, reddit_signals, bsr_signals, autocomplete_signals, mapper, previous)
        if scored is None:
            # Still check exit signals even for non-active trends
            exit_sig = detect_exit_signals(trend_name, gt_output, reddit_signals, bsr_signals, previous, mapper)
            if exit_sig and exit_sig.get("urgency") in ("EXIT_RECOMMENDED", "CONSIDER_EXIT"):
                exit_warnings.append(exit_sig)
            continue

        # Attach exit signal to scored trend
        exit_sig = detect_exit_signals(trend_name, gt_output, reddit_signals, bsr_signals, previous, mapper)
        if exit_sig:
            scored["exit_signal"] = exit_sig
            if exit_sig.get("urgency") in ("EXIT_RECOMMENDED", "CONSIDER_EXIT", "WATCH"):
                exit_warnings.append(exit_sig)

        # Carry forward persistence data
        if trend_name in prev_signals_dict:
            prev = prev_signals_dict[trend_name]
            scored["signal_first_seen"] = prev.get("signal_first_seen", scored["signal_first_seen"])
            scored["days_active"] = prev.get("days_active", 0) + 1
            scored["peak_score"] = max(scored["score"], prev.get("peak_score", 0))
            scored["thesis"] = prev.get("thesis", "")
            scored["thesis_file"] = prev.get("thesis_file", "")
        else:
            scored["days_active"] = 1
            scored["thesis"] = ""
            scored["thesis_file"] = ""

        # Map companies
        companies = map_companies(trend_name, mapper)
        scored["companies"] = companies

        # Generate thesis for NEW Tier 1 hits (morning/full mode only)
        is_new_tier1 = (
            scored["tier"] == 1
            and args.mode in ["morning", "full"]
            and trend_name not in prev_signals_dict
        )

        if is_new_tier1:
            log.info(f"NEW TIER 1: {trend_name} — generating Opus thesis...")
            thesis_text = generate_thesis(scored, companies)
            if thesis_text:
                thesis_file = save_thesis(scored, thesis_text, companies)
                scored["thesis"] = thesis_text
                scored["thesis_file"] = thesis_file
                send_discord_alert(scored, companies, thesis_text)
            else:
                send_discord_alert(scored, companies, None)
        elif scored["tier"] == 1 and trend_name in prev_signals_dict:
            # Existing Tier 1 — send update alert only if score improved
            prev_score = prev_signals_dict[trend_name].get("score", 0)
            if scored["score"] > prev_score + 1:
                send_discord_alert(scored, companies, scored.get("thesis"))

        if scored["exit_warning"]:
            exit_warnings.append(trend_name)

        active_signals.append(scored)

    # Sort by tier then score
    active_signals.sort(key=lambda x: (x["tier"], -x["score"]))

    # ── Build Output ──────────────────────────────────────────────────────────

    # Build source health
    source_health = {
        "google_trends": {
            "status": "active" if gt_output.get("signals") else ("skipped" if args.mode == "fast" else "error"),
            "signals_fired": sum(1 for v in gt_output.get("signals", {}).values() if v.get("score", 0) > 0),
            "last_run": datetime.now().isoformat() if args.mode != "fast" else None,
            "note": "Morning/full mode only" if args.mode == "fast" else "Active",
        },
        "reddit": {
            "status": "active" if reddit_signals else "error",
            "signals_fired": sum(1 for v in reddit_signals.values() if v.get("score", 0) > 0),
            "subreddits_scanned": len(TARGET_SUBREDDITS),
            "last_run": datetime.now().isoformat(),
            "note": "PRAW authenticated" if (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET) else "Public HTTP fallback",
        },
        "amazon_bsr": {
            "status": "active" if (args.mode in ["morning", "full"] and bsr_signals) else ("skipped" if args.mode == "fast" else "error"),
            "signals_fired": sum(1 for v in bsr_signals.values() if v.get("score", 0) > 0),
            "last_run": datetime.now().isoformat() if args.mode != "fast" else None,
            "note": "Morning/full mode only" if args.mode == "fast" else "Active",
        },
        "yfinance": {
            "status": "active",
            "note": "Free, no rate limits",
            "last_run": datetime.now().isoformat(),
        },
        "opus_thesis": {
            "status": "active" if ANTHROPIC_API_KEY else "missing_key",
            "note": "Fires on NEW Tier 1 hits only" if ANTHROPIC_API_KEY else "ANTHROPIC_API_KEY not set",
        },
        "discord_webhook": {
            "status": "active" if DISCORD_WEBHOOK_URL else "missing",
            "note": "Tier 1 alerts" if DISCORD_WEBHOOK_URL else "DISCORD_WEBHOOK_URL not set",
        },
        "amazon_autocomplete": {
            "status": "active" if autocomplete_signals else "error",
            "signals_fired": sum(1 for v in autocomplete_signals.values() if v.get("score", 0) > 0),
            "last_run": datetime.now().isoformat(),
            "note": "Free, no auth required",
        },
        "tiktok": {
            "status": "active" if tiktok_signals else ("skipped" if not tiktok_available() else "error"),
            "signals_fired": sum(1 for v in tiktok_signals.values() if v.get("score", 0) > 0) if tiktok_signals else 0,
            "last_run": datetime.now().isoformat() if tiktok_signals else None,
            "note": "Research API" if tiktok_available() else "No API credentials — apply at developers.tiktok.com",
        },
    }

    output = {
        "generated_at": datetime.now().isoformat(),
        "mode": args.mode,
        "active_signals": active_signals,
        "new_trends_discovered": gt_output.get("discovery", []),
        "exit_warnings": exit_warnings,
        "source_health": source_health,
        "run_stats": {
            "duration_sec": round(time.time() - start, 1),
            "keywords_scanned": len(set(all_keywords)),
            "trends_evaluated": len(trend_names),
            "tier1_count": sum(1 for s in active_signals if s["tier"] == 1),
            "tier2_count": sum(1 for s in active_signals if s["tier"] == 2),
            "tier3_count": sum(1 for s in active_signals if s["tier"] == 3),
        }
    }

    # Save current as previous for next run
    if PREV_PATH.exists():
        with open(PREV_PATH) as f:
            old_prev = json.load(f)
        # Preserve 30-day rolling avg for Reddit
        old_prev["reddit_counts"] = {
            t: {
                "avg_30d": max(
                    reddit_signals.get(t, {}).get("total_7d", 0),
                    old_prev.get("reddit_counts", {}).get(t, {}).get("avg_30d", 1)
                )
            }
            for t in trend_names
        }
        old_prev["active_signals"] = active_signals
        old_prev["autocomplete"] = autocomplete_signals
        if tiktok_signals:
            old_prev["tiktok"] = tiktok_signals
        with open(PREV_PATH, "w") as f:
            json.dump(old_prev, f, indent=2)
    else:
        with open(PREV_PATH, "w") as f:
            json.dump({"active_signals": active_signals, "reddit_counts": {}}, f, indent=2)

    with open(DATA_PATH, "w") as f:
        json.dump(output, f, indent=2)

    # Commit to GitHub
    commit_data()

    # Summary
    t1 = output["run_stats"]["tier1_count"]
    t2 = output["run_stats"]["tier2_count"]
    duration = output["run_stats"]["duration_sec"]
    tier1_names = [s["display_name"] for s in active_signals if s["tier"] == 1]
    tier1_str = ", ".join(tier1_names) if tier1_names else "none"

    log.info(f"=== WATCHTOWER 2.0 run complete in {duration}s — Tier 1: {tier1_str} | Tier 2: {t2} ===")


if __name__ == "__main__":
    main()
