#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — TikTok Research API Scanner (Stub)
Ready to activate when TikTok Research API access is granted.

TikTok Research API provides:
- Video search by hashtag/keyword with engagement metrics
- Hashtag video counts over time
- Comment and engagement data

Apply for access at: https://developers.tiktok.com/products/research-api/
Requires: Academic or commercial research application approval.

When approved, set these env vars:
  TIKTOK_CLIENT_KEY=<your_client_key>
  TIKTOK_CLIENT_SECRET=<your_client_secret>

This module will then activate automatically on next Watchtower run.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

# TikTok Research API endpoints
TIKTOK_AUTH_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_SEARCH_URL = "https://open.tiktokapis.com/v2/research/video/query/"

# Env vars
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")

# Rate limits: Research API allows 1000 requests/day
REQUEST_DELAY = 2.0  # seconds between requests


def is_available() -> bool:
    """Check if TikTok Research API credentials are configured."""
    return bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET)


def _get_access_token() -> str | None:
    """
    Obtain OAuth2 access token using client credentials flow.
    Token is valid for ~2 hours.
    """
    try:
        resp = requests.post(
            TIKTOK_AUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"  TikTok auth failed: HTTP {resp.status_code}")
            return None

        data = resp.json()
        token = data.get("data", {}).get("access_token")
        if not token:
            log.warning(f"  TikTok auth: no access_token in response")
            return None

        return token

    except Exception as e:
        log.warning(f"  TikTok auth error: {e}")
        return None


def _search_videos(
    access_token: str,
    keyword: str,
    days_back: int = 7,
    max_results: int = 20,
) -> dict[str, Any]:
    """
    Search TikTok videos containing a keyword in the last N days.
    Returns video count and aggregate engagement metrics.
    """
    start_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y%m%d")
    end_date = datetime.utcnow().strftime("%Y%m%d")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "query": {
            "and": [
                {
                    "operation": "IN",
                    "field_name": "keyword",
                    "field_values": [keyword],
                }
            ]
        },
        "start_date": start_date,
        "end_date": end_date,
        "max_count": max_results,
        "fields": [
            "id",
            "create_time",
            "like_count",
            "comment_count",
            "share_count",
            "view_count",
            "hashtag_names",
        ],
    }

    try:
        resp = requests.post(
            TIKTOK_SEARCH_URL,
            headers=headers,
            json=payload,
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning(f"  TikTok search HTTP {resp.status_code} for '{keyword}'")
            return {"video_count": 0, "total_views": 0, "total_likes": 0, "total_shares": 0}

        data = resp.json().get("data", {})
        videos = data.get("videos", [])

        total_views = sum(v.get("view_count", 0) for v in videos)
        total_likes = sum(v.get("like_count", 0) for v in videos)
        total_shares = sum(v.get("share_count", 0) for v in videos)
        total_comments = sum(v.get("comment_count", 0) for v in videos)

        # Collect associated hashtags for discovery
        associated_hashtags: dict[str, int] = {}
        for v in videos:
            for tag in v.get("hashtag_names", []):
                tag_lower = tag.lower()
                associated_hashtags[tag_lower] = associated_hashtags.get(tag_lower, 0) + 1

        return {
            "video_count": len(videos),
            "has_more": data.get("has_more", False),
            "total_views": total_views,
            "total_likes": total_likes,
            "total_shares": total_shares,
            "total_comments": total_comments,
            "engagement_rate": round(
                (total_likes + total_shares + total_comments) / max(total_views, 1) * 100, 2
            ),
            "associated_hashtags": dict(
                sorted(associated_hashtags.items(), key=lambda x: -x[1])[:10]
            ),
        }

    except Exception as e:
        log.warning(f"  TikTok search error for '{keyword}': {e}")
        return {"video_count": 0, "total_views": 0, "total_likes": 0, "total_shares": 0}


def scan_tiktok(
    trend_keywords: dict[str, list[str]],
    previous: dict,
) -> dict[str, dict]:
    """
    Scan TikTok Research API for trend keyword engagement.

    Args:
        trend_keywords: Dict of trend_name -> list of keywords from mapper.json
        previous: Previous run data for delta comparison

    Returns:
        Dict of trend_name -> {
            score: int (0-3),
            video_count: int,
            total_views: int,
            engagement_rate: float,
            associated_hashtags: dict,
            discovery: list of new hashtags,
        }

    Returns empty dict if API is not available (no credentials).
    """
    if not is_available():
        log.info("TikTok scanner: skipped (no API credentials configured)")
        log.info("  Apply at: https://developers.tiktok.com/products/research-api/")
        return {}

    log.info("TikTok Research API scanner starting...")

    access_token = _get_access_token()
    if not access_token:
        log.warning("TikTok scanner: could not obtain access token")
        return {}

    results = {}
    prev_tiktok = previous.get("tiktok", {})
    discovery_hashtags: list[str] = []

    for trend_name, keywords in trend_keywords.items():
        # Use primary keyword only to conserve daily request quota
        primary_kw = keywords[0] if keywords else trend_name

        try:
            search_result = _search_videos(access_token, primary_kw)
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning(f"  TikTok scan failed for '{trend_name}': {e}")
            results[trend_name] = {"score": 0, "error": str(e)}
            continue

        video_count = search_result.get("video_count", 0)
        total_views = search_result.get("total_views", 0)
        engagement_rate = search_result.get("engagement_rate", 0)

        # Score based on video count and engagement
        # These thresholds will need calibration after first real runs
        score = 0
        if video_count >= 15 and engagement_rate > 3.0:
            score = 3  # High volume + high engagement
        elif video_count >= 10 or (video_count >= 5 and engagement_rate > 5.0):
            score = 2  # Moderate volume or viral engagement
        elif video_count >= 3:
            score = 1  # Some activity

        # View velocity bonus: if views are very high relative to video count
        if total_views > 0 and video_count > 0:
            avg_views = total_views / video_count
            if avg_views > 100_000:
                score = min(score + 1, 3)

        # Delta vs previous
        prev_count = prev_tiktok.get(trend_name, {}).get("video_count", 0)
        if prev_count == 0 and video_count > 0:
            delta = "new"
        elif video_count > prev_count * 1.5:
            delta = "surging"
        elif video_count >= prev_count:
            delta = "stable"
        else:
            delta = "declining"

        # Collect novel hashtags for discovery
        for tag in search_result.get("associated_hashtags", {}):
            if tag not in [kw.lower() for kw in keywords]:
                discovery_hashtags.append(tag)

        results[trend_name] = {
            "score": score,
            "video_count": video_count,
            "total_views": total_views,
            "total_likes": search_result.get("total_likes", 0),
            "total_shares": search_result.get("total_shares", 0),
            "engagement_rate": engagement_rate,
            "associated_hashtags": search_result.get("associated_hashtags", {}),
            "delta_vs_previous": delta,
        }

        if score > 0:
            log.info(
                f"  TikTok: {trend_name} | videos={video_count} | "
                f"views={total_views:,} | engagement={engagement_rate}% | score={score}"
            )

    fired = sum(1 for v in results.values() if v.get("score", 0) > 0)
    log.info(f"TikTok scanner complete. {fired} signals fired.")

    # Attach discovery hashtags to results metadata
    if discovery_hashtags:
        log.info(f"  TikTok discovery: {len(set(discovery_hashtags))} novel hashtags found")

    return results
