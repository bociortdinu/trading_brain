"""Massive/Polygon provider: parsing, pagination, retry (429/timeout), errors, gaps.

All requests go through httpx.MockTransport — no real Polygon calls.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import httpx
import pytest

from data_collector.providers.polygon import MassivePolygonProvider, ProviderError
from tests.helpers import run

FIX = pathlib.Path(__file__).parent / "fixtures"
T0 = 1767225600000  # 2026-01-01T00:00:00Z, in ms
_NOW = lambda: datetime(2026, 2, 1, tzinfo=timezone.utc)  # after the fixture bars -> none dropped


def _provider(handler):
    return MassivePolygonProvider(
        api_key="test-key", base_url="https://api.polygon.io",
        max_retries=2, backoff_base=0.0, backoff_cap=0.0,
        transport=httpx.MockTransport(handler), now_fn=_NOW,
    )


def _bars(prices, start_ms=T0, step_ms=900_000):
    return [
        {"o": p, "h": p + 1, "l": p - 1, "c": p + 0.5, "v": 100, "t": start_ms + i * step_ms}
        for i, p in enumerate(prices)
    ]


def test_parse_single_page():
    def h(_req):
        return httpx.Response(200, json={"status": "OK", "results": _bars([2400.0, 2401.0])})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 2
        assert candles[0].open == 2400.0 and candles[0].close == 2400.5
        # close_time = open_time + 15min
        assert (candles[0].close_time - candles[0].open_time).total_seconds() == 900

    run(go())


def test_pagination_follows_next_url():
    page1 = json.loads((FIX / "polygon_page1.json").read_text())
    page2 = json.loads((FIX / "polygon_page2.json").read_text())

    def h(req):
        if req.url.params.get("cursor") == "PAGE2":
            return httpx.Response(200, json=page2)
        return httpx.Response(200, json=page1)

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 5  # 3 + 2 across pages
        assert p.last_gaps == []  # contiguous

    run(go())


def test_retry_on_429_then_success():
    calls = {"n": 0}

    def h(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"status": "ERROR"})
        return httpx.Response(200, json={"status": "OK", "results": _bars([2400.0])})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 1 and calls["n"] == 2

    run(go())


def test_retry_on_timeout_then_success():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"status": "OK", "results": _bars([2400.0])})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 1 and calls["n"] == 2

    run(go())


def test_exhausted_retries_raises():
    def h(_req):
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"status": "ERROR"})

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_error_status_raises():
    def h(_req):
        return httpx.Response(200, json={"status": "ERROR", "error": "unknown ticker"})

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_invalid_json_raises():
    def h(_req):
        return httpx.Response(200, content=b"<<not json>>", headers={"content-type": "text/plain"})

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_max_pages_reached_raises_no_partial():
    # every page advertises another page -> after max_pages, refuse partial data
    def h(_req):
        return httpx.Response(200, json={
            "status": "OK", "results": _bars([2400.0]),
            "next_url": "https://api.polygon.io/v2/aggs/x?cursor=MORE",
        })

    async def go():
        p = MassivePolygonProvider("k", max_retries=0, backoff_base=0.0, max_pages=2,
                                   transport=httpx.MockTransport(h), now_fn=_NOW)
        try:
            with pytest.raises(ProviderError):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_next_url_foreign_origin_rejected():
    def h(_req):
        return httpx.Response(200, json={
            "status": "OK", "results": _bars([2400.0]),
            "next_url": "https://evil.example.com/v2/aggs/x?cursor=PAGE2",
        })

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_retry_after_http_date_is_parsed():
    calls = {"n": 0}

    def h(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            # HTTP-date in the past -> wait clamps to 0; must not crash on the date format
            return httpx.Response(429, headers={"Retry-After": "Wed, 01 Jan 2020 00:00:00 GMT"},
                                  json={"status": "ERROR"})
        return httpx.Response(200, json={"status": "OK", "results": _bars([2400.0])})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 1 and calls["n"] == 2

    run(go())


def test_retry_after_honored_in_full_uncapped():
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    calls = {"n": 0}

    def h(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, json={"status": "ERROR"})
        return httpx.Response(200, json={"status": "OK", "results": _bars([2400.0])})

    async def go():
        p = MassivePolygonProvider(
            "k", max_retries=2, backoff_base=0.5, backoff_cap=0.1,
            transport=httpx.MockTransport(h), now_fn=_NOW, sleep_fn=fake_sleep,
        )
        try:
            await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())
    assert 5.0 in slept  # full Retry-After, NOT clamped to backoff_cap=0.1


def test_gap_reported_not_filled():
    # skip one 15-min bar between index 1 and 2 (step of 2 intervals)
    bars = _bars([2400.0, 2401.0]) + _bars([2403.0], start_ms=T0 + 3 * 900_000)

    def h(_req):
        return httpx.Response(200, json={"status": "OK", "results": bars})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 3          # NOT forward-filled
        assert len(p.last_gaps) == 1
        assert p.last_gaps[0].missing_bars == 1

    run(go())
