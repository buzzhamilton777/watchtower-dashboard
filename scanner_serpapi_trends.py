#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — SerpAPI Google Trends Scanner
Replaces pytrends which gets 429-blocked by Google.

SerpAPI handles all proxy rotation and anti-bot bypassing server-side.
Free tier: 250 searches/month. We use ~132/month. Plenty of headroom.

Batches up to 5 keywords per API call.
~30 keywords / 5 per batch = 6 API calls per run. Finishes in <60 seconds.

Score: 3pts if ratio > 2.0 AND new 90-day high
       1pt  if ratio > 1.5 AND new 90-day high

Author: Hamilton | May 2026 — replacing pytrends after persistent 429s
"""

import logging
import os
import time
import requests
from datetime import datetime

log = logging.getLogger(__name__)

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
SERPAPI_BASE = "https://serpapi.com/search.json"
MAX_KW_PER_BATCH = 5  # SerpAPI Google Trends allows up to 5 keywords per request
MAINSTREAM_THRESHOLD = 75


def is_available() -> bool:
    return bool(SERPAPI_KEY)


def _fetch_trends_batch(keywords: list, timeframe: str = "today 3-m", geo: str = "US") -> dict:
    """
    Fetches Google Trends interest-over-time data for up to 5 keywords.
    Returns dict of {keyword: [weekly_values]} or {} on error.
    """
    if not SERPAPI_KEY:
        return {}

    params = {
        "engine": "google_trends",
        "q": ",".join(keywords),
        "date": timeframe,
        "geo": geo,
        "api_key": SERPAPI_KEY,
    }

    try:
        resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            timeline = data.get("interest_over_time", {}).get("timeline_data", [])
            if not timeline:
                log.warning(f"  SerpAPI: no timeline data for {keywords}")
                return {}

            # Build per-keyword series
            result = {kw: [] for kw in keywords}
            for point in timeline:
                for val in point.get("values", []):
                    kw = val.get("query")
                    extracted = val.get("extracted_value", 0)
                    if kw in result:
                        result[kw].append(extracted)

            return result

        elif resp.status_code == 401:
            log.error("SerpAPI: invalid API key")
            return {}
        elif resp.status_code == 429:
            log.warning("SerpAPI: rate limited (monthly quota may be exhausted)")
            return {}
        else:
            log.warning(f"SerpAPI: HTTP {resp.status_code} for {keywords}")
            return {}

    except Exception as e:
        log.warning(f"SerpAPI fetch exception for {keywords}: {e}")
        return {}


def _fetch_rising_queries(keyword: str) -> list:
    """
    Fetches rising/breakout queries related to a keyword.
    Used for trend discovery — surfaces new keywords to add to mapper.
    """
    if not SERPAPI_KEY:
        return []

    params = {
        "engine": "google_trends",
        "q": keyword,
        "data_type": "RELATED_QUERIES",
        "date": "today 3-m",
        "geo": "US",
        "api_key": SERPAPI_KEY,
    }

    try:
        resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            rising = data.get("related_queries", {}).get("rising", [])
            return [r.get("query", "") for r in rising[:5] if r.get("query")]
        return []
    except Exception:
        return []


def deep_dive_trend(trend_name: str, all_keywords: list) -> dict:
    """
    Reactive deep-dive: scan ALL keyword variants for a trend when another scanner spikes.
    Uses the monthly buffer (140 searches/month left after daily primaries).
    Call this when Reddit/Amazon/YouTube fires a strong signal on a trend.

    Args:
        trend_name: e.g. 'peptides'
        all_keywords: all keyword variants for this trend from mapper.json

    Returns same signal dict as scan_google_trends_serpapi but for all variants.
    """
    if not SERPAPI_KEY:
        log.warning("SerpAPI deep dive: no API key")
        return {"signals": {}, "discovery": []}

    log.info(f"SerpAPI deep dive: scanning all {len(all_keywords)} variants for '{trend_name}'")
    return scan_google_trends_serpapi(all_keywords, {})


def scan_google_trends_serpapi(keywords: list, previous: dict) -> dict:
    """
    Main SerpAPI Google Trends scanner.
    Replaces scan_google_trends() (pytrends version).

    Args:
        keywords: flat list of keyword strings to scan
        previous: previous watchtower data (unused for now, future delta tracking)

    Returns:
        {
            "signals": {keyword: {score, current, avg_12w, ratio, is_90d_high, is_mainstream}},
            "discovery": [new_keyword_strings]
        }
    """
    if not SERPAPI_KEY:
        log.warning("SerpAPI scanner: SERPAPI_KEY not set — skipping Google Trends")
        return {"signals": {}, "discovery": []}

    log.info("Google Trends scanner starting (SerpAPI)...")
    signals = {}
    discovery = []

    # Deduplicate keywords
    unique_keywords = list(dict.fromkeys(keywords))

    # Batch into groups of MAX_KW_PER_BATCH
    batches = [unique_keywords[i:i + MAX_KW_PER_BATCH] for i in range(0, len(unique_keywords), MAX_KW_PER_BATCH)]
    log.info(f"  {len(unique_keywords)} keywords → {len(batches)} batches of up to {MAX_KW_PER_BATCH}")

    consecutive_failures = 0
    MAX_FAILURES = 2  # 2-strike rule

    for batch in batches:
        if consecutive_failures >= MAX_FAILURES:
            log.warning(f"SerpAPI: {consecutive_failures} consecutive failures — stopping GT scanner early")
            break

        batch_data = _fetch_trends_batch(batch)

        if not batch_data:
            consecutive_failures += 1
            log.warning(f"SerpAPI batch failed [{consecutive_failures}/{MAX_FAILURES}]: {batch}")
            time.sleep(5)
            continue

        consecutive_failures = 0

        for kw in batch:
            series = batch_data.get(kw, [])
            if len(series) < 4:
                continue

            current = float(series[-1])
            # 12-week average excluding current week
            avg_12w = float(sum(series[-13:-1]) / len(series[-13:-1])) if len(series) >= 13 else float(sum(series[:-1]) / max(len(series) - 1, 1))
            is_90d_high = current == max(series) and current > 0
            ratio = current / avg_12w if avg_12w > 0 else 0
            is_mainstream = current > MAINSTREAM_THRESHOLD

            score = 0
            if ratio > 2.0 and is_90d_high:
                score = 3
            elif ratio > 1.5 and is_90d_high:
                score = 1

            signals[kw] = {
                "score": score,
                "current": current,
                "avg_12w": round(avg_12w, 1),
                "ratio": round(ratio, 2),
                "is_90d_high": is_90d_high,
                "is_mainstream": is_mainstream,
                "shopping_ratio": 0.0,
                "shopping_score": 0,
            }

            if score > 0:
                log.info(f"  Google Trends: {kw} | ratio={ratio:.2f} | 90d_high={is_90d_high} | score={score}")

        time.sleep(1)  # Light delay between batches — SerpAPI doesn't need much

    # Rising queries discovery — use first keyword of first batch as probe
    if unique_keywords and consecutive_failures < MAX_FAILURES:
        try:
            rising = _fetch_rising_queries(unique_keywords[0])
            if rising:
                # Filter out keywords we already track
                new_ones = [r for r in rising if r.lower() not in [k.lower() for k in unique_keywords]]
                discovery.extend(new_ones[:5])
                if new_ones:
                    log.info(f"  Rising queries discovery: {new_ones}")
        except Exception as e:
            log.warning(f"  Rising queries failed: {e}")

    fired = sum(1 for v in signals.values() if v.get("score", 0) > 0)
    log.info(f"Google Trends (SerpAPI) complete. {fired} signals fired across {len(signals)} keywords. Used {len(batches)} API calls.")
    return {"signals": signals, "discovery": discovery}
