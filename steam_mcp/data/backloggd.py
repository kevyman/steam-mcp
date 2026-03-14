"""Scrape Backloggd reviews for the configured user and upsert into ratings table."""

import logging
import os
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup, Tag
from rapidfuzz import process, fuzz

from .db import get_db

_BACKLOGGD_USER = os.getenv("BACKLOGGD_USER", "")
BASE_URL = f"https://backloggd.com/u/{_BACKLOGGD_USER}/reviews"
logger = logging.getLogger(__name__)


async def sync_backloggd() -> dict:
    """
    Scrape all pages of Backloggd reviews, fuzzy-match to DB games,
    upsert into ratings. Returns stats dict.
    """
    async with get_db() as db:
        game_rows = await db.execute_fetchall("SELECT appid, name FROM games")

    name_to_appid = {r["name"].lower(): r["appid"] for r in game_rows}
    all_names = list(name_to_appid.keys())

    reviews = await _scrape_all_pages()
    synced = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        for review in reviews:
            appid = _match_appid(review["title"], all_names, name_to_appid)
            if appid is None:
                logger.debug("No match for Backloggd game: %s", review["title"])
                skipped += 1
                continue

            raw = review["score"]  # 0.5–5
            normalized = raw * 2   # → 1–10

            await db.execute(
                """INSERT INTO ratings (appid, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, 'backloggd', ?, ?, ?, ?)
                   ON CONFLICT(appid, source) DO UPDATE SET
                       raw_score = excluded.raw_score,
                       normalized_score = excluded.normalized_score,
                       review_text = excluded.review_text,
                       synced_at = excluded.synced_at""",
                (appid, raw, normalized, review.get("text", ""), now),
            )
            synced += 1

        await db.commit()

    return {"synced": synced, "skipped": skipped, "total_scraped": len(reviews)}


async def _scrape_all_pages() -> list[dict]:
    """Paginate through Backloggd reviews and return list of {title, score, text}."""
    reviews = []
    page = 1
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "steam-mcp/1.0"}) as client:
        while True:
            url = BASE_URL if page == 1 else f"{BASE_URL}?page={page}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Backloggd page %d fetch failed: %s", page, e)
                break

            page_reviews = _parse_page(resp.text)
            if not page_reviews:
                break

            reviews.extend(page_reviews)
            page += 1

            # Safety limit
            if page > 100:
                break

    return reviews


def _parse_page(html: str) -> list[dict]:
    """Parse one Backloggd reviews page."""
    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # Backloggd review cards — selectors may need adjustment if site changes
    for card in soup.select(".review-card, [class*='review']"):
        title_el = card.select_one(".game-title, [class*='game-title'], h3, h4")
        if title_el is None:
            continue

        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Star rating — look for filled stars or rating text
        score = _extract_score(card)
        if score is None:
            continue

        text_el = card.select_one(".review-text, [class*='review-text'], p")
        text = text_el.get_text(strip=True) if text_el else ""

        reviews.append({"title": title, "score": score, "text": text})

    return reviews


def _extract_score(card: Tag) -> float | None:
    """Try to extract a 0.5–5 star score from a review card."""
    # Look for a rating element
    rating_el = card.select_one("[class*='rating'], [class*='stars'], .rating")
    if rating_el:
        text = rating_el.get_text(strip=True)
        try:
            val = float(text.replace("★", "").replace("⭐", "").strip())
            if 0.5 <= val <= 5:
                return val
        except ValueError:
            pass

    # Count filled star icons
    filled = len(card.select(".star-filled, .filled, [class*='filled']"))
    half = len(card.select(".star-half, .half, [class*='half']"))
    if filled > 0 or half > 0:
        score = filled + half * 0.5
        if 0.5 <= score <= 5:
            return score

    return None


def _match_appid(title: str, all_names: list[str], name_to_appid: dict) -> int | None:
    """Fuzzy-match a Backloggd title to a game in the DB."""
    title_lower = title.lower()

    # Exact match first
    if title_lower in name_to_appid:
        return name_to_appid[title_lower]

    # Fuzzy match
    result = process.extractOne(
        title_lower,
        all_names,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=85,
    )
    if result:
        return name_to_appid[result[0]]

    return None
