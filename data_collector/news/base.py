"""News interface + point-in-time (bitemporal) as_of discipline.

Two time axes (see docs/NEWS_PROVIDER_REQUIREMENTS.md):
- publication_time = VALID time      (when a revision became public)
- ingestion_time   = TRANSACTION time / first-seen (when WE received that revision)

At decision time `as_of`, a revision is usable only if BOTH times are <= as_of (a story
we hadn't ingested yet was not knowable). Events have a stable identity (source,
external_id); corrections are new REVISIONS of that identity, and the decision view keeps
only the latest revision that was visible by `as_of`. This makes look-ahead leakage —
attaching current/late news to a historical snapshot — impossible by construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel


class NewsItem(BaseModel):
    source: str                          # origin feed; part of the stable identity
    external_id: str                     # source's durable event id; part of the identity
    revision: int = 0                    # monotonic; a higher revision supersedes the identity
    publication_time: datetime           # VALID time: when THIS revision was published
    ingestion_time: datetime             # TRANSACTION time / first-seen: when WE received it
    headline: str
    impact: Literal["low", "medium", "high"] = "low"
    sentiment_for_gold: float = 0.0      # [-1, 1]; negative = bearish for gold

    @property
    def identity(self) -> tuple[str, str]:
        return (self.source, self.external_id)

    def to_digest(self) -> dict:
        return {
            "id": f"{self.source}:{self.external_id}",
            "revision": self.revision,
            "time": self.publication_time.isoformat(),
            "headline": self.headline,
            "impact": self.impact,
            "sentiment_for_gold": self.sentiment_for_gold,
        }


class NewsProvider(Protocol):
    async def get_news(self, symbol: str, count: int, *, as_of: datetime | None = None) -> list[NewsItem]:
        """Return news for `symbol`. When `as_of` is given, the result MUST be the
        point-in-time-correct decision view (no look-ahead, deduped, latest visible
        revision). When it is None (live), return the raw items and let the caller gate."""
        ...


def as_of_filter(items: list[NewsItem], as_of: datetime) -> list[NewsItem]:
    """Raw no-look-ahead temporal filter: keep revisions publishable AND ingestible by
    `as_of`. Does NOT dedup or resolve revisions. Sorted newest-publication first."""
    usable = [
        it for it in items
        if it.publication_time <= as_of and it.ingestion_time <= as_of
    ]
    return sorted(usable, key=lambda it: it.publication_time, reverse=True)


def as_of_view(items: list[NewsItem], as_of: datetime) -> list[NewsItem]:
    """The DECISION view: no-look-ahead + dedup by identity + latest revision visible by
    `as_of`. For each identity keep the revision with the highest (revision, ingestion_time)
    among those usable at `as_of`. Newest publication first."""
    best: dict[tuple[str, str], NewsItem] = {}
    for it in as_of_filter(items, as_of):
        cur = best.get(it.identity)
        if cur is None or (it.revision, it.ingestion_time) > (cur.revision, cur.ingestion_time):
            best[it.identity] = it
    return sorted(best.values(), key=lambda it: it.publication_time, reverse=True)
