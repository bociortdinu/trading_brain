"""XTB provider (trading_hands /candles) via httpx.MockTransport. Same MarketDataProvider
contract as Polygon: closed bars only, on the canonical UTC grid, fail-closed otherwise."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from data_collector.providers.polygon import ProviderError
from data_collector.providers.xtb import XtbCandlesProvider
from tests.helpers import run

UTC = timezone.utc
T0 = 1767225600000  # 2026-01-01T00:00:00Z, on-grid
_NOW = lambda: datetime(2026, 2, 1, tzinfo=UTC)


def _rows(prices, start_ms=T0, step_ms=900_000):
    return [{"t": start_ms + i * step_ms, "o": p, "h": p + 1, "l": p - 1, "c": p + 0.5, "v": 10}
            for i, p in enumerate(prices)]


def _provider(handler):
    return XtbCandlesProvider("http://127.0.0.1:4000",
                              transport=httpx.MockTransport(handler), now_fn=_NOW)


def test_parses_candles_envelope():
    def h(req):
        assert req.url.path == "/candles/GOLD/M15" and req.url.params.get("count") == "10"
        return httpx.Response(200, json={"symbol": "GOLD", "period": "M15", "candles": _rows([2400.0, 2401.0])})

    async def go():
        p = _provider(h)
        try:
            candles = await p.get_ohlcv("GOLD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 2 and candles[0].close == 2400.5
        assert (candles[0].close_time - candles[0].open_time).total_seconds() == 900

    run(go())


def test_bare_array_also_accepted():
    def h(_req):
        return httpx.Response(200, json=_rows([2400.0]))

    async def go():
        p = _provider(h)
        try:
            assert len(await p.get_ohlcv("GOLD", "1h", 10)) == 1
        finally:
            await p.aclose()

    run(go())


def test_forming_bar_dropped():
    # last bar closes AFTER now -> dropped (anti look-ahead)
    now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)  # only bars closing by 00:30 are kept

    def h(_req):
        return httpx.Response(200, json={"candles": _rows([2400.0, 2401.0, 2402.0])})  # closes 00:15,00:30,00:45

    async def go():
        p = XtbCandlesProvider("http://x", transport=httpx.MockTransport(h), now_fn=lambda: now)
        try:
            candles = await p.get_ohlcv("GOLD", "15min", 10)
        finally:
            await p.aclose()
        assert len(candles) == 2 and candles[-1].close_time == now  # 00:45 bar dropped

    run(go())


def test_off_grid_series_rejected():
    def h(_req):
        base = int(datetime(2026, 1, 1, 0, 8, tzinfo=UTC).timestamp() * 1000)  # :08 phase -> off grid
        return httpx.Response(200, json={"candles": _rows([2400.0, 2401.0], start_ms=base)})

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError, match="off the 15min grid|contract violation"):
                await p.get_ohlcv("GOLD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_http_error_is_provider_error():
    def h(_req):
        return httpx.Response(503, text="unavailable")

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError, match="HTTP 503"):
                await p.get_ohlcv("GOLD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_malformed_row_is_provider_error():
    def h(_req):
        return httpx.Response(200, json={"candles": [{"t": T0, "o": 2400.0}]})  # missing h/l/c

    async def go():
        p = _provider(h)
        try:
            with pytest.raises(ProviderError, match="malformed candle"):
                await p.get_ohlcv("GOLD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


def test_unsupported_timeframe_rejected():
    async def go():
        p = _provider(lambda _r: httpx.Response(200, json={"candles": []}))
        try:
            with pytest.raises(ProviderError, match="unsupported timeframe"):
                await p.get_ohlcv("GOLD", "5min", 10)
        finally:
            await p.aclose()

    run(go())
