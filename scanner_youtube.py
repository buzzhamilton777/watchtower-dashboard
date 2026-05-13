#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — YouTube Scanner
Camillo signal: creator + viewer interest velocity before Wall Street notices.

Measures:
  - New video upload count (per keyword, last 7 days) vs 30-day rolling avg
  - Total view velocity on keyword-related videos
  - Fires when upload rate AND views are both accelerating

API: YouTube Data API v3 (free, 10k units/day)
Cost per keyword: ~100 units (search). 13 trends = ~1,300 units/scan. Well within quota.

Score: 2pts if velocity > 2x avg | 1pt if > 1.5x avg
Author: Hamilton | May 2026
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
MAX_RESULTS_PER_KEYWORD = 50  # Each search = ~100 units. 50 results is sufficient.
PUBLISHED_AFTER_DAYS = 7       # Look at videos published in the last 7 days
VELOCITY_RATIO_HIGH = 2.0      # > 2x 30-day avg → score 2
VELOCITY_RATIO_MED = 1.5       # > 1.5x 30-day avg → score 1
MIN_TOTAL_VIEWS = 10000        # Ignore trends with negligible total views (noise filter)
MIN_VIDEO_COUNT = 3            # Minimum new videos to count as signal (noise filter)


def is_available() -> bool:
    """Returns True if YouTube API key is configured."""
    return bool(YOUTUBE_API_KEY)


def _search_videos(keyword: str, published_after: str, max_results: int = MAX_RESULTS_PER_KEYWORD) -> list[dict]:
    """
    Searches YouTube Data API v3 for videos matching a keyword.
    Returns list of video metadata dicts.
    published_after: RFC 3339 datetime string e.g. '2026-05-06T00:00:00Z'
    """
    try:
        import requests
    except ImportError:
        log.error("requests not installed")
        return []

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": keyword,
        "type": "video",
        "order": "date",
        "publishedAfter": published_after,
        "maxResults": max_results,
        "regionCode": "US",
        "relevanceLanguage": "en",
        "key": YOUTUBE_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            return items
        elif resp.status_code == 403:
            log.warning(f"YouTube API quota exceeded or key invalid: {resp.json()}")
            return []
        else:
            log.warning(f"YouTube search failed for '{keyword}': HTTP {resp.status_code}")
            return []
    except Exception as e:
        log.warning(f"YouTube search exception for '{keyword}': {e}")
        return []


def _get_video_stats(video_ids: list[str]) -> dict[str, dict]:
    """
    Fetches view counts for a list of video IDs (batch, up to 50 per call).
    Returns dict of video_id -> {viewCount, likeCount}
    """
    if not video_ids:
        return {}

    try:
        import requests
    except ImportError:
        return {}

    url = "https://www.googleapis.com/youtube/v3/videos"
    stats = {}

    # API allows up to 50 IDs per call
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {
            "part": "statistics",
            "id": ",".join(batch),
            "key": YOUTUBE_API_KEY,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("items", []):
                    vid_id = item["id"]
                    s = item.get("statistics", {})
                    stats[vid_id] = {
                        "viewCount": int(s.get("viewCount", 0)),
                        "likeCount": int(s.get("likeCount", 0)),
                    }
            else:
                log.warning(f"YouTube video stats failed: HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"YouTube video stats exception: {e}")

    return stats


def scan_youtube(trend_keywords: dict[str, list[str]], previous: dict) -> dict:
    """
    Main YouTube scanner entry point.

    Args:
        trend_keywords: {trend_name: [keyword1, keyword2, ...]}
        previous: previous watchtower data (for 30-day rolling baseline)

    Returns:
        {trend_name: {score, video_count_7d, total_views_7d, avg_video_count_30d, ratio, ...}}
    """
    if not YOUTUBE_API_KEY:
        log.warning("YouTube scanner: YOUTUBE_API_KEY not set — skipping")
        return {}

    log.info("YouTube scanner starting...")
    results = {}

    # Cutoff: videos published in last 7 days
    now_utc = datetime.now(timezone.utc)
    published_after = (now_utc - timedelta(days=PUBLISHED_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Previous baselines
    prev_yt = previous.get("youtube_counts", {})

    consecutive_failures = 0
    MAX_FAILURES = 2  # 2-strike rule

    for trend_name, keywords in trend_keywords.items():
        if consecutive_failures >= MAX_FAILURES:
            log.warning(f"YouTube: {consecutive_failures} consecutive failures — stopping scanner early")
            break

        # Use primary keyword only (first in list) to conserve API quota
        # Each search = ~100 units. 13 trends * 100 = 1,300 units/day. Safe under 10k limit.
        primary_kw = keywords[0]

        try:
            videos = _search_videos(primary_kw, published_after)
            if not videos:
                # Empty result is OK (not a failure), just no signal
                results[trend_name] = {
                    "score": 0,
                    "video_count_7d": 0,
                    "total_views_7d": 0,
                    "avg_video_count_30d": prev_yt.get(trend_name, {}).get("avg_video_count_30d", 0),
                    "ratio": 0,
                    "top_video_title": "",
                    "top_video_views": 0,
                    "keyword_used": primary_kw,
                }
                time.sleep(1)
                consecutive_failures = 0
                continue

            # Get video IDs for stats fetch
            video_ids = [item["id"]["videoId"] for item in videos if item.get("id", {}).get("videoId")]
            stats = _get_video_stats(video_ids)

            video_count_7d = len(video_ids)
            total_views_7d = sum(s["viewCount"] for s in stats.values())

            # Find top video by views
            top_video = max(stats.items(), key=lambda x: x[1]["viewCount"], default=(None, {"viewCount": 0}))
            top_video_id = top_video[0]
            top_video_views = top_video[1]["viewCount"] if top_video[0] else 0

            # Get title of top video
            top_video_title = ""
            for item in videos:
                if item.get("id", {}).get("videoId") == top_video_id:
                    top_video_title = item.get("snippet", {}).get("title", "")
                    break

            # Rolling baseline (30-day avg weekly count stored from previous runs)
            prev_entry = prev_yt.get(trend_name, {})
            avg_video_count_30d = prev_entry.get("avg_video_count_30d", 0)

            # First run — no baseline yet. Store count and return score=0 (calibrating)
            if avg_video_count_30d == 0:
                results[trend_name] = {
                    "score": 0,
                    "video_count_7d": video_count_7d,
                    "total_views_7d": total_views_7d,
                    "avg_video_count_30d": video_count_7d,  # Seed with current as baseline
                    "ratio": 0,
                    "top_video_title": top_video_title,
                    "top_video_views": top_video_views,
                    "keyword_used": primary_kw,
                    "note": "calibrating — first run",
                }
                log.info(f"  YouTube: {trend_name} — calibrating (first run, {video_count_7d} videos, {total_views_7d:,} views)")
                consecutive_failures = 0
                time.sleep(2)
                continue

            # Score
            ratio = video_count_7d / avg_video_count_30d if avg_video_count_30d > 0 else 0
            score = 0

            if ratio >= VELOCITY_RATIO_HIGH and video_count_7d >= MIN_VIDEO_COUNT and total_views_7d >= MIN_TOTAL_VIEWS:
                score = 2
            elif ratio >= VELOCITY_RATIO_MED and video_count_7d >= MIN_VIDEO_COUNT and total_views_7d >= MIN_TOTAL_VIEWS:
                score = 1

            # Update rolling baseline (exponential moving average, α=0.3)
            # This keeps the baseline fresh without overweighting any single week
            alpha = 0.3
            new_avg = alpha * video_count_7d + (1 - alpha) * avg_video_count_30d

            results[trend_name] = {
                "score": score,
                "video_count_7d": video_count_7d,
                "total_views_7d": total_views_7d,
                "avg_video_count_30d": round(new_avg, 1),
                "ratio": round(ratio, 2),
                "top_video_title": top_video_title,
                "top_video_views": top_video_views,
                "keyword_used": primary_kw,
            }

            if score > 0:
                log.info(
                    f"  YouTube signal: {trend_name} | videos_7d={video_count_7d} | "
                    f"views={total_views_7d:,} | ratio={ratio:.2f} | score={score}"
                )

            consecutive_failures = 0
            time.sleep(2)  # Polite delay between keywords

        except Exception as e:
            consecutive_failures += 1
            log.warning(f"YouTube: {trend_name} failed [{consecutive_failures}/{MAX_FAILURES}]: {e}")
            results[trend_name] = {"score": 0, "error": str(e)}

    fired = sum(1 for v in results.values() if v.get("score", 0) > 0)
    log.info(f"YouTube complete. {fired} signals fired across {len(results)} trends.")
    return results
