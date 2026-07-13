"""Static in-memory news provider (dev/tests). A live HTTP feed comes with Faza 2."""

from __future__ import annotations

from datetime import datetime

from .base import NewsItem, NewsProvider, as_of_view


class StaticNewsProvider(NewsProvider):
    def __init__(self, items: list[NewsItem] | None = None) -> None:
        self._items = items or []

    async def get_news(self, symbol: str, count: int, *, as_of: datetime | None = None) -> list[NewsItem]:
        # With as_of -> point-in-time decision view (no look-ahead, deduped, latest revision).
        items = as_of_view(self._items, as_of) if as_of is not None else list(self._items)
        return items[-count:] if count else items
