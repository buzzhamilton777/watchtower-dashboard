#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — Amazon Autocomplete Scanner
Tracks Amazon search suggestion rankings for trend keywords.
Provides 1-2 week lead time over BSR changes.

The Amazon completion API is free, unauthenticated, and returns what
customers are actively typing right now. If a keyword appears in the
top suggestions (especially positions 1-3), consumer demand is real
and accelerating before it shows in bestseller rankings.
"""

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# Amazon completion endpoint — no auth required
AMAZON_COMPLETION_URL = "https://completion.amazon.com/api/2017/suggestions"

# Headers to mimic a browser request
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.amazon.com",
    "Referer": "https://www.amazon.com/",
}

# Delay between requests to avoid throttling
REQUEST_DELAY = 1.5  # seconds


def _fetch_suggestions(query: str, marketplace: str = "www.amazon.com") -> list[str]:
    """
    Fetch autocomplete suggestions for a query from Amazon.
    Returns list of suggestion strings, ordered by Amazon's ranking.
    """
    params = {
        "mid": "ATVPDKIKX0DER",  # US marketplace ID
        "alias": "aps",  # All departments
        "prefix": query,
        "event": "onKeyPress",
        "limit": "10",
        "fb": "1",
        "suggestion-type": "KEYWORD",
    }

    try:
        resp = requests.get(
            AMAZON_COMPLETION_URL,
            params=params,
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"  Autocomplete HTTP {resp.status_code} for '{query}'")
            return []

        data = resp.json()
        suggestions = []
        for item in data.get("suggestions", []):
            value = item.get("value", "")
            if value:
                suggestions.append(value.lower())
        return suggestions

    except requests.exceptions.Timeout:
        log.warning(f"  Autocomplete timeout for '{query}'")
        return []
    except Exception as e:
        log.warning(f"  Autocomplete error for '{query}': {e}")
        return []


def _score_keyword_presence(
    keyword: str,
    suggestions: list[str],
) -> dict[str, Any]:
    """
    Score a keyword's presence in Amazon autocomplete suggestions.

    Scoring:
    - Position 1-3: strong signal (score 3) — Amazon ranks by search volume
    - Position 4-7: moderate signal (score 2)
    - Position 8-10: weak signal (score 1)
    - Not present: no signal (score 0)

    Also checks for keyword variants/expansions (e.g., "peptide" matching
    "peptide supplements", "peptide cream") which indicate category breadth.
    """
    kw_lower = keyword.lower()
    best_position = None
    matching_suggestions = []

    for i, suggestion in enumerate(suggestions):
        if kw_lower in suggestion:
            if best_position is None:
                best_position = i + 1  # 1-indexed
            matching_suggestions.append(suggestion)

    if best_position is None:
        return {
            "score": 0,
            "position": None,
            "match_count": 0,
            "matching_suggestions": [],
            "category_breadth": 0,
        }

    # Position-based scoring
    if best_position <= 3:
        score = 3
    elif best_position <= 7:
        score = 2
    else:
        score = 1

    # Category breadth: how many distinct suggestions contain the keyword
    # More variants = broader consumer interest
    category_breadth = len(matching_suggestions)
    if category_breadth >= 5:
        score = min(score + 1, 3)  # Breadth bonus, capped at 3

    return {
        "score": score,
        "position": best_position,
        "match_count": category_breadth,
        "matching_suggestions": matching_suggestions[:5],
        "category_breadth": category_breadth,
    }


def scan_amazon_autocomplete(
    trend_keywords: dict[str, list[str]],
    previous: dict,
) -> dict[str, dict]:
    """
    Scan Amazon autocomplete for all trend keywords.

    Args:
        trend_keywords: Dict of trend_name -> list of keywords from mapper.json
        previous: Previous run data for delta comparison

    Returns:
        Dict of trend_name -> {
            score: int (0-3),
            keywords_found: int,
            top_position: int or None,
            avg_position: float or None,
            category_breadth: int,
            details: list of per-keyword results,
            sample_suggestions: list of strings,
            delta_vs_previous: str ("new", "improved", "stable", "declining", None),
        }
    """
    log.info("Amazon Autocomplete scanner starting...")
    results = {}
    prev_autocomplete = previous.get("autocomplete", {})

    for trend_name, keywords in trend_keywords.items():
        trend_score = 0
        keywords_found = 0
        positions = []
        all_suggestions = []
        details = []
        total_breadth = 0

        # Use primary keyword + up to 2 secondary keywords to limit requests
        scan_keywords = keywords[:3]

        for kw in scan_keywords:
            try:
                suggestions = _fetch_suggestions(kw)
                time.sleep(REQUEST_DELAY)

                kw_result = _score_keyword_presence(kw, suggestions)
                details.append({"keyword": kw, **kw_result})

                if kw_result["score"] > 0:
                    keywords_found += 1
                    trend_score = max(trend_score, kw_result["score"])
                    if kw_result["position"] is not None:
                        positions.append(kw_result["position"])
                    all_suggestions.extend(kw_result["matching_suggestions"])
                    total_breadth += kw_result["category_breadth"]

            except Exception as e:
                log.warning(f"  Autocomplete scan failed for '{kw}': {e}")
                details.append({"keyword": kw, "score": 0, "error": str(e)})

        # Compute aggregates
        top_position = min(positions) if positions else None
        avg_position = round(sum(positions) / len(positions), 1) if positions else None

        # Multi-keyword bonus: if 2+ keywords from same trend appear, boost
        if keywords_found >= 2:
            trend_score = min(trend_score + 1, 3)

        # Delta vs previous
        prev_score = prev_autocomplete.get(trend_name, {}).get("score", 0)
        prev_position = prev_autocomplete.get(trend_name, {}).get("top_position")
        if prev_score == 0 and trend_score > 0:
            delta = "new"
        elif trend_score > prev_score:
            delta = "improved"
        elif trend_score == prev_score and trend_score > 0:
            delta = "stable"
        elif trend_score < prev_score:
            delta = "declining"
        else:
            delta = None

        results[trend_name] = {
            "score": trend_score,
            "keywords_found": keywords_found,
            "top_position": top_position,
            "avg_position": avg_position,
            "category_breadth": total_breadth,
            "details": details,
            "sample_suggestions": list(set(all_suggestions))[:5],
            "delta_vs_previous": delta,
        }

        if trend_score > 0:
            log.info(
                f"  Autocomplete: {trend_name} | score={trend_score} | "
                f"pos={top_position} | breadth={total_breadth} | delta={delta}"
            )

    fired = sum(1 for v in results.values() if v["score"] > 0)
    log.info(f"Amazon Autocomplete complete. {fired} signals fired.")
    return results
