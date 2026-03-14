"""Lazy Steam Store API enrichment — genres, tags, review score, metacritic."""

import asyncio
import json
from datetime import datetime, timezone

import httpx

from .db import get_db

STORE_CACHE_DAYS = 7
STORE_API = "https://store.steampowered.com/api/appdetails"
REVIEWS_API = "https://store.steampowered.com/appreviews/{appid}"


async def enrich_game(appid: int) -> dict | None:
    """
    Fetch Steam Store data for appid and cache in DB.
    Returns the full games row dict, or None on failure.
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT store_cached_at FROM games WHERE appid = ?", (appid,)
        )
        if row and _is_fresh(row["store_cached_at"], STORE_CACHE_DAYS):
            full = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (appid,))
            return dict(full) if full else None

    store_data, review_summary = await _fetch_all(appid)
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        if store_data is None:
            await db.execute(
                "UPDATE games SET store_cached_at = ? WHERE appid = ?", (now, appid)
            )
        else:
            steam_tags = _extract_tags(store_data)
            genres = json.dumps([g["description"] for g in store_data.get("genres", [])])
            short_desc = store_data.get("short_description", "")

            # Metacritic score from appdetails
            metacritic = store_data.get("metacritic") or {}
            metacritic_score = metacritic.get("score")

            # Steam community review score from appreviews endpoint
            review_score = review_summary.get("review_score")       # 0–9 enum
            review_desc = review_summary.get("review_score_desc")   # e.g. "Overwhelmingly Positive"

            await db.execute(
                """UPDATE games SET
                    genres = ?,
                    tags = ?,
                    short_description = ?,
                    steam_review_score = ?,
                    steam_review_desc = ?,
                    metacritic_score = ?,
                    metacritic_cached_at = ?,
                    store_cached_at = ?
                WHERE appid = ?""",
                (genres, steam_tags, short_desc,
                 review_score, review_desc,
                 metacritic_score, now,
                 now, appid),
            )
        await db.commit()

    async with get_db() as db:
        full = await db.execute_fetchone("SELECT * FROM games WHERE appid = ?", (appid,))
        return dict(full) if full else None


async def _fetch_all(appid: int) -> tuple[dict | None, dict]:
    """Fetch appdetails and appreviews concurrently. Returns (store_data, review_summary)."""
    async def fetch_store():
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    STORE_API,
                    params={"appids": appid, "filters": "basic,genres,categories,short_description,metacritic"},
                )
                resp.raise_for_status()
                payload = resp.json()
            app_data = payload.get(str(appid), {})
            if not app_data.get("success"):
                return None
            return app_data.get("data", {})
        except Exception:
            return None

    async def fetch_reviews():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    REVIEWS_API.format(appid=appid),
                    params={"json": 1, "language": "all", "purchase_type": "all"},
                )
                resp.raise_for_status()
                return resp.json().get("query_summary", {})
        except Exception:
            return {}

    store_data, review_summary = await asyncio.gather(fetch_store(), fetch_reviews())
    return store_data, review_summary


def _extract_tags(data: dict) -> str:
    """Build tag list from genres + categories, deduplicated, max 20."""
    tags = []
    for g in data.get("genres", []):
        tags.append(g["description"])
    for c in data.get("categories", []):
        tags.append(c["description"])
    seen = set()
    unique = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return json.dumps(unique[:20])


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at or cached_at == "FAILED":
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
