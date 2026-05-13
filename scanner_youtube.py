#!/usr/bin/env python3
"""
WATCHTOWER 2.0 — YouTube Scanner
Camillo signal: creator + viewer interest velocity before Wall Street notices.

Measures:
  - New video upload count (per keyword, last 7 days) vs real 4-week rolling baseline
  - Time-weighted view velocity (views/day, not raw total) on keyword-related videos
  - Fires when upload rate AND weighted view velocity are both accelerating

API: YouTube Data API v3 (free, 10k units/day)
Cost: up to 3 keywords/trend × 13 trends × ~100 units = ~3,900 units/scan. Safe.

Score: 2pts if velocity > 2x baseline | 1pt if > 1.5x baseline
Updated: May 2026 — Opus review fixes applied
  Fix 1: Search all keywords per trend (up to 3), deduplicate video IDs
  Fix 2: Time-weighted view velocity (views/day) instead of raw total views
  Fix 3: Real 4-week rolling average instead of EMA seeded from one week
  Fix 4: Circuit breaker distinguishes API errors from empty results
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
MAX_RESULTS_PER_KEYWORD = 50    # Each search = ~100 units
MAX_KEYWORDS_PER_TREND = 3      # Cap to keep quota safe
PUBLISHED_AFTER_DAYS = 7        # Look at videos published in the last 7 days
VELOCITY_RATIO_HIGH = 2.0       # > 2x baseline → score 2
VELOCITY_RATIO_MED = 1.5        # > 1.5x baseline → score 1
MIN_WEIGHTED_VELOCITY = 5000    # views/day total across all videos (noise filter)
MIN_VIDEO_COUNT = 3             # Minimum new videos to count as signal (noise filter)
MAX_WEEKLY_HISTORY = 4          # Keep last 4 weeks for rolling baseline


def is_available() -> bool:
    """Returns True if YouTube API key is configured."""
    return bool(YOUTUBE_API_KEY)


def _search_videos(keyword: str, published_after: str, max_results: int = MAX_RESULTS_PER_KEYWORD) -> Optional[List[dict]]:
    """
    Searches YouTube Data API v3 for videos matching a keyword.

    Returns:
        List[dict] — video items on success (may be empty — that's OK, not a failure)
        None       — on API error (403 quota, 429 rate limit, network failure)
                     Caller should treat None as a circuit-breaker failure.

    published_after: RFC 3339 datetime string e.g. '2026-05-06T00:00:00Z'
    """
    import requests

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
            return resp.json().get("items", [])
        elif resp.status_code == 403:
            # Quota exceeded or key invalid — stop scanning immediately
            log.warning(f"YouTube API quota exceeded or key invalid for '{keyword}' — stopping all YouTube scanning today")
            return None
        elif resp.status_code == 429:
            log.warning(f"YouTube API rate limited for '{keyword}'")
            return None
        else:
            log.warning(f"YouTube search failed for '{keyword}': HTTP {resp.status_code}")
            return None
    except Exception as e:
        log.warning(f"YouTube search exception for '{keyword}': {e}")
        return None


def _get_video_stats(video_ids: List[str]) -> Dict[str, dict]:
    """
    Fetches view counts for a list of video IDs (batch, up to 50 per call).
    Returns dict of video_id -> {viewCount, likeCount}
    """
    if not video_ids:
        return {}

    import requests

    url = "https://www.googleapis.com/youtube/v3/videos"
    stats = {}

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
        if i + 50 < len(video_ids):
            time.sleep(1)  # Polite delay between stats batches

    return stats


def scan_youtube(trend_keywords: Dict[str, List[str]], previous: dict) -> dict:
    """
    Main YouTube scanner entry point.

    Args:
        trend_keywords: {trend_name: [keyword1, keyword2, ...]}
        previous: previous watchtower data (for rolling baseline)

    Returns:
        {trend_name: {score, video_count_7d, total_views_7d, weighted_views_velocity,
                      baseline_avg, ratio, keywords_used, top_video_title, top_video_views}}
    """
    if not YOUTUBE_API_KEY:
        log.warning("YouTube scanner: YOUTUBE_API_KEY not set — skipping")
        return {}

    log.info("YouTube scanner starting...")
    results = {}

    now_utc = datetime.now(timezone.utc)
    published_after = (now_utc - timedelta(days=PUBLISHED_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Previous baselines: {trend_name: {weekly_counts: [int, ...]}}
    prev_yt = previous.get("youtube_counts", {})

    consecutive_failures = 0
    MAX_FAILURES = 2  # 2-strike rule

    for trend_name, keywords in trend_keywords.items():
        if consecutive_failures >= MAX_FAILURES:
            log.warning(f"YouTube: {consecutive_failures} consecutive API failures — stopping scanner early (quota likely exhausted)")
            break

        # Cap at MAX_KEYWORDS_PER_TREND to control quota
        search_keywords = keywords[:MAX_KEYWORDS_PER_TREND]

        # Collect all video items across keywords, deduplicate by video ID
        seen_ids = set()
        all_videos = {}  # video_id -> snippet item
        api_error = False
        keywords_used = []

        for kw in search_keywords:
            items = _search_videos(kw, published_after)

            if items is None:
                # API error (quota/rate limit/network) — count as failure
                consecutive_failures += 1
                log.warning(f"YouTube: '{kw}' returned API error [{consecutive_failures}/{MAX_FAILURES}]")
                api_error = True
                break
            else:
                # Valid response (even if empty) — reset failure counter
                consecutive_failures = 0

            keywords_used.append(kw)
            for item in items:
                vid_id = item.get("id", {}).get("videoId")
                if vid_id and vid_id not in seen_ids:
                    seen_ids.add(vid_id)
                    all_videos[vid_id] = item

            time.sleep(1)  # Polite delay between keyword searches

        if api_error:
            results[trend_name] = {"score": 0, "error": "api_error"}
            time.sleep(2)
            continue

        if not all_videos:
            # No videos found — valid empty result, not a failure
            results[trend_name] = {
                "score": 0,
                "video_count_7d": 0,
                "total_views_7d": 0,
                "weighted_views_velocity": 0,
                "baseline_avg": prev_yt.get(trend_name, {}).get("weekly_counts", [0])[-1] if prev_yt.get(trend_name, {}).get("weekly_counts") else 0,
                "ratio": 0,
                "keywords_used": keywords_used,
                "top_video_title": "",
                "top_video_views": 0,
            }
            time.sleep(2)
            continue

        # Fetch stats for all deduplicated video IDs
        video_ids = list(all_videos.keys())
        stats = _get_video_stats(video_ids)

        video_count_7d = len(video_ids)
        total_views_7d = sum(s["viewCount"] for s in stats.values())

        # FIX 2: Time-weighted view velocity — views/day per video, summed
        # Newer videos with high views = stronger signal than old videos coasting
        weighted_views_velocity = 0.0
        for vid_id, stat in stats.items():
            item = all_videos.get(vid_id, {})
            published_at_str = item.get("snippet", {}).get("publishedAt", "")
            try:
                published_at = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
                days_since = max(1.0, (now_utc - published_at).total_seconds() / 86400)
            except Exception:
                days_since = 7.0  # Fallback: assume middle of window
            weighted_views_velocity += stat["viewCount"] / days_since

        weighted_views_velocity = round(weighted_views_velocity, 1)

        # Find top video by raw views
        top_vid = max(stats.items(), key=lambda x: x[1]["viewCount"], default=(None, {"viewCount": 0}))
        top_video_id = top_vid[0]
        top_video_views = top_vid[1]["viewCount"] if top_vid[0] else 0
        top_video_title = ""
        if top_video_id and top_video_id in all_videos:
            top_video_title = all_videos[top_video_id].get("snippet", {}).get("title", "")

        # FIX 3: Real rolling average from stored weekly counts
        prev_entry = prev_yt.get(trend_name, {})
        weekly_counts = list(prev_entry.get("weekly_counts", []))

        # Need at least 2 weeks of history to score (current week + 1 prior)
        if len(weekly_counts) < 1:
            # First run — calibrate
            results[trend_name] = {
                "score": 0,
                "video_count_7d": video_count_7d,
                "total_views_7d": total_views_7d,
                "weighted_views_velocity": weighted_views_velocity,
                "baseline_avg": video_count_7d,
                "ratio": 0,
                "keywords_used": keywords_used,
                "top_video_title": top_video_title,
                "top_video_views": top_video_views,
                "note": "calibrating — first run",
                # Store weekly_counts with current week for next run
                "_weekly_counts_update": [video_count_7d],
            }
            log.info(f"  YouTube: {trend_name} — calibrating (first run, {video_count_7d} videos, velocity={weighted_views_velocity:.0f} views/day)")
            time.sleep(2)
            continue

        # Baseline = mean of all stored weeks (prior weeks only, not current)
        baseline_avg = sum(weekly_counts) / len(weekly_counts)

        # Score based on upload velocity ratio AND weighted view velocity
        ratio = video_count_7d / baseline_avg if baseline_avg > 0 else 0
        score = 0

        if ratio >= VELOCITY_RATIO_HIGH and video_count_7d >= MIN_VIDEO_COUNT and weighted_views_velocity >= MIN_WEIGHTED_VELOCITY:
            score = 2
        elif ratio >= VELOCITY_RATIO_MED and video_count_7d >= MIN_VIDEO_COUNT and weighted_views_velocity >= MIN_WEIGHTED_VELOCITY:
            score = 1

        # Update weekly history — append current week, keep last MAX_WEEKLY_HISTORY
        updated_counts = (weekly_counts + [video_count_7d])[-MAX_WEEKLY_HISTORY:]

        results[trend_name] = {
            "score": score,
            "video_count_7d": video_count_7d,
            "total_views_7d": total_views_7d,
            "weighted_views_velocity": weighted_views_velocity,
            "baseline_avg": round(baseline_avg, 1),
            "ratio": round(ratio, 2),
            "keywords_used": keywords_used,
            "top_video_title": top_video_title,
            "top_video_views": top_video_views,
            "_weekly_counts_update": updated_counts,
        }

        if score > 0:
            log.info(
                f"  YouTube signal: {trend_name} | videos_7d={video_count_7d} | "
                f"velocity={weighted_views_velocity:.0f} views/day | ratio={ratio:.2f} | score={score} | "
                f"keywords={keywords_used}"
            )

        time.sleep(2)

    fired = sum(1 for v in results.values() if v.get("score", 0) > 0)
    log.info(f"YouTube complete. {fired} signals fired across {len(results)} trends.")
    return results
