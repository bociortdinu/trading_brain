"""News: bitemporal as_of discipline (no look-ahead), identity dedup, revisions, and an
HTTP provider integration through httpx.MockTransport. See docs/NEWS_PROVIDER_REQUIREMENTS.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from data_collector.news.base import NewsItem, as_of_filter, as_of_view
from data_collector.news.http_provider import HttpNewsProvider, NewsProviderError
from tests.helpers import run

UTC = timezone.utc


def _item(src, ext, rev, pub_h, seen_h, headline="x"):
    return NewsItem(
        source=src, external_id=ext, revision=rev,
        publication_time=datetime(2026, 7, 12, pub_h, tzinfo=UTC),
        ingestion_time=datetime(2026, 7, 12, seen_h, tzinfo=UTC),
        headline=headline,
    )


# --------------------------------------------------------------------------- #
# no look-ahead
# --------------------------------------------------------------------------- #
def test_future_publication_excluded():
    as_of = datetime(2026, 7, 12, 12, tzinfo=UTC)
    items = [_item("s", "a", 0, 11, 11, "past"), _item("s", "b", 0, 13, 13, "future")]
    assert [it.headline for it in as_of_view(items, as_of)] == ["past"]


def test_late_ingestion_excluded():
    # published before as_of, but first-seen AFTER as_of -> not knowable -> dropped
    as_of = datetime(2026, 7, 12, 12, tzinfo=UTC)
    items = [_item("s", "a", 0, 11, 13, "seen_late")]
    assert as_of_view(items, as_of) == []


# --------------------------------------------------------------------------- #
# identity dedup + revisions
# --------------------------------------------------------------------------- #
def test_dedup_by_identity():
    as_of = datetime(2026, 7, 12, 12, tzinfo=UTC)
    dup = [_item("s", "a", 0, 10, 10, "one"), _item("s", "a", 0, 10, 10, "one")]
    assert len(as_of_view(dup, as_of)) == 1


def test_latest_revision_visible_by_as_of_wins():
    # rev0 published+seen at 09:00; rev1 (correction) published 10:00 but only seen 13:00.
    rev0 = _item("s", "a", 0, 9, 9, "initial")
    rev1 = _item("s", "a", 1, 10, 13, "corrected")
    early = datetime(2026, 7, 12, 12, tzinfo=UTC)  # before rev1 was seen
    late = datetime(2026, 7, 12, 14, tzinfo=UTC)   # after rev1 was seen
    assert [it.headline for it in as_of_view([rev0, rev1], early)] == ["initial"]
    assert [it.headline for it in as_of_view([rev0, rev1], late)] == ["corrected"]


def test_as_of_filter_keeps_all_revisions_but_view_collapses():
    rev0 = _item("s", "a", 0, 9, 9, "initial")
    rev1 = _item("s", "a", 1, 10, 10, "corrected")
    as_of = datetime(2026, 7, 12, 12, tzinfo=UTC)
    assert len(as_of_filter([rev0, rev1], as_of)) == 2   # raw filter keeps both revisions
    assert len(as_of_view([rev0, rev1], as_of)) == 1     # decision view collapses to latest


# --------------------------------------------------------------------------- #
# HTTP provider via MockTransport
# --------------------------------------------------------------------------- #
_NEWS_JSON = {
    "results": [
        {"source": "acme", "external_id": "E1", "revision": 0,
         "published_at": "2026-07-12T09:00:00+00:00", "first_seen_at": "2026-07-12T09:01:00+00:00",
         "headline": "CPI as expected", "impact": "medium", "sentiment_for_gold": 0.0},
        {"source": "acme", "external_id": "E1", "revision": 1,   # correction, seen late
         "published_at": "2026-07-12T10:00:00+00:00", "first_seen_at": "2026-07-12T13:00:00+00:00",
         "headline": "CPI revised UP", "impact": "high", "sentiment_for_gold": -0.4},
        {"source": "acme", "external_id": "E2", "revision": 0,   # future story
         "published_at": "2026-07-12T20:00:00+00:00", "first_seen_at": "2026-07-12T20:00:00+00:00",
         "headline": "Fed presser tonight", "impact": "high", "sentiment_for_gold": 0.0},
    ]
}


def _provider():
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_NEWS_JSON)
    return HttpNewsProvider("https://news.example", api_key="k",
                            transport=httpx.MockTransport(handler))


def test_http_provider_as_of_no_lookahead():
    as_of = datetime(2026, 7, 12, 12, tzinfo=UTC)  # before the correction was seen, before the future story

    async def go():
        p = _provider()
        try:
            return await p.get_news("GOLD", 10, as_of=as_of)
        finally:
            await p.aclose()

    items = run(go())
    heads = [it.headline for it in items]
    assert heads == ["CPI as expected"]          # E1 rev0 only; rev1 not-yet-seen, E2 in the future
    assert all(it.publication_time <= as_of and it.ingestion_time <= as_of for it in items)


def test_http_provider_later_as_of_sees_correction():
    as_of = datetime(2026, 7, 12, 14, tzinfo=UTC)  # after correction seen, still before the 20:00 story

    async def go():
        p = _provider()
        try:
            return await p.get_news("GOLD", 10, as_of=as_of)
        finally:
            await p.aclose()

    items = run(go())
    assert [it.headline for it in items] == ["CPI revised UP"]  # latest visible revision of E1


def test_http_provider_historical_requires_first_seen():
    payload = {"results": [{"source": "acme", "external_id": "E9", "revision": 0,
                            "published_at": "2026-07-12T09:00:00+00:00",
                            "headline": "no first_seen", "impact": "low"}]}

    def handler(_req):
        return httpx.Response(200, json=payload)

    async def go():
        p = HttpNewsProvider("https://news.example", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(NewsProviderError, match="first_seen"):
                await p.get_news("GOLD", 10, as_of=datetime(2026, 7, 12, 12, tzinfo=UTC))
        finally:
            await p.aclose()

    run(go())
