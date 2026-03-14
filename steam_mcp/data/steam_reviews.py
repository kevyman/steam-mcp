"""Scrape Steam community reviews for the configured user."""

import logging
import os
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .db import get_db

_STEAM_PROFILE_ID = os.getenv("STEAM_PROFILE_ID", "")
BASE_URL = f"https://steamcommunity.com/id/{_STEAM_PROFILE_ID}/recommended/"
logger = logging.getLogger(__name__)


async def sync_steam_reviews() -> dict:
    """
    Scrape paginated Steam reviews, upsert into ratings.
    Returns stats dict.
    """
    reviews = await _scrape_all_pages()
    synced = 0
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        for review in reviews:
            appid = review["appid"]
            raw = review["vote"]        # 1 (up) or -1 (down)
            normalized = 7.0 if raw == 1 else 3.0

            await db.execute(
                """INSERT INTO ratings (appid, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, 'steam_review', ?, ?, ?, ?)
                   ON CONFLICT(appid, source) DO UPDATE SET
                       raw_score = excluded.raw_score,
                       normalized_score = excluded.normalized_score,
                       review_text = excluded.review_text,
                       synced_at = excluded.synced_at""",
                (appid, float(raw), normalized, review.get("text", ""), now),
            )
            synced += 1

        await db.commit()

    return {"synced": synced, "total_scraped": len(reviews)}


async def _scrape_all_pages() -> list[dict]:
    reviews = []
    page = 1
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; steam-mcp/1.0)"},
        follow_redirects=True,
    ) as client:
        while True:
            url = BASE_URL if page == 1 else f"{BASE_URL}?p={page}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Steam reviews page %d fetch failed: %s", page, e)
                break

            page_reviews = _parse_page(resp.text)
            if not page_reviews:
                break

            reviews.extend(page_reviews)
            page += 1

            if page > 200:
                break

    return reviews


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # Each review box on the Steam profile recommendations page
    for box in soup.select(".review_box, [class*='review_box']"):
        # Extract appid from the review link e.g. /recommended/12345/
        link = box.select_one("a[href*='/recommended/']")
        if link is None:
            continue

        m = re.search(r"/recommended/(\d+)/", link.get("href", ""))
        if not m:
            continue

        appid = int(m.group(1))

        # Determine thumb direction
        thumb_up = box.select_one(".thumb_up, .thumbsUp, [class*='thumbsUp'], [class*='thumb_up']")
        thumb_down = box.select_one(".thumb_down, .thumbsDown, [class*='thumbsDown'], [class*='thumb_down']")

        if thumb_up is not None:
            vote = 1
        elif thumb_down is not None:
            vote = -1
        else:
            # Try text "Recommended" / "Not Recommended"
            title_el = box.select_one(".title, [class*='ratingSummary']")
            if title_el:
                text = title_el.get_text(strip=True).lower()
                vote = 1 if "not" not in text and "recommend" in text else -1
            else:
                continue

        text_el = box.select_one(".content, [class*='review_content'] p, [class*='apphub_CardTextContent']")
        text = text_el.get_text(strip=True) if text_el else ""

        reviews.append({"appid": appid, "vote": vote, "text": text})

    return reviews
