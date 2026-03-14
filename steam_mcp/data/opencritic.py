"""
Metacritic score sourced via Steam Store appdetails (free, no API key).
OpenCritic's API now requires a paid RapidAPI key and is no longer used.
metacritic_score and metacritic_cached_at are populated by steam_store.enrich_game.
"""

from .db import get_db


async def get_metacritic(appid: int) -> int | None:
    """Return cached Metacritic score (populated by steam_store.enrich_game)."""
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT metacritic_score FROM games WHERE appid = ?", (appid,)
        )
    return row["metacritic_score"] if row else None
