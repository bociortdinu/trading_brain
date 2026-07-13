"""Client behaviour: parsing, error mapping, 204, symbol resolution. No XTB calls."""

from __future__ import annotations

import httpx
import pytest

from brokers_bridge.trading_hands import (
    AmbiguousSymbolError,
    ModelSetup,
    PredictionRequest,
    TradingHandsBadResponse,
    TradingHandsStatusError,
    TradingHandsTimeout,
    TradingHandsUnreachable,
)
from tests.helpers import make_client, run


def _valid_prediction() -> PredictionRequest:
    return PredictionRequest(
        symbol="GOLD",
        prediction_date="2026-07-12",
        allocation=1,
        preds_proba=0.6,
        stop_loss=1.0,
        take_profit=1.0,
        model_setup=ModelSetup(forecast_horizon=1, target_change=1, model_type="Buy"),
        runtimestamp="2026-07-12T10:00:00Z",
        sl_tp_logic="percent",
        interval="15m",
        id_model_properties="test",
    )


def test_status_ok():
    def h(_req):
        return httpx.Response(200, json={"connected": True, "account": "1", "environment": "demo"})

    async def go():
        async with make_client(h) as c:
            s = await c.status()
            assert s.connected and s.environment == "demo"

    run(go())


def test_quote_ok_spread():
    def h(_req):
        return httpx.Response(200, json={"symbol": "GOLD", "bid": 2400.0, "ask": 2400.6})

    async def go():
        async with make_client(h) as c:
            q = await c.quote("GOLD")
            assert q.bid == 2400.0 and q.ask == 2400.6
            assert q.spread_pct == pytest.approx((0.6 / 2400.6) * 100)

    run(go())


def test_quote_503_market_closed():
    def h(_req):
        return httpx.Response(503, json={"error": "Quote unavailable (market may be closed)"})

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsStatusError) as ei:
                await c.quote("GOLD")
            assert ei.value.status_code == 503

    run(go())


@pytest.mark.parametrize("code", [400, 502, 503])
def test_status_errors_map(code):
    def h(_req):
        return httpx.Response(code, json={"error": "boom"})

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsStatusError) as ei:
                await c.balance()
            assert ei.value.status_code == code

    run(go())


def test_timeout_maps():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsTimeout):
                await c.status()

    run(go())


def test_connect_error_maps_unreachable():
    def h(req):
        raise httpx.ConnectError("refused", request=req)

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsUnreachable):
                await c.status()

    run(go())


def test_invalid_json_maps_bad_response():
    def h(_req):
        return httpx.Response(200, content=b"<<not json>>", headers={"content-type": "text/plain"})

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsBadResponse):
                await c.status()

    run(go())


def test_invalid_schema_maps_bad_response():
    def h(_req):
        return httpx.Response(200, json={"connected": True})  # missing account/environment

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsBadResponse):
                await c.status()

    run(go())


def test_purchase_204_returns_none():
    def h(_req):
        return httpx.Response(204)

    async def go():
        async with make_client(h) as c:
            assert await c.purchase(_valid_prediction()) is None

    run(go())


def test_purchase_201_returns_result():
    def h(_req):
        return httpx.Response(
            201,
            json={"accepted": True, "external_id": "999", "symbol": "GOLD", "side": "buy", "volume": 0.01},
        )

    async def go():
        async with make_client(h) as c:
            res = await c.purchase(_valid_prediction())
            assert res is not None and res.external_id == "999"

    run(go())


def test_purchase_502_maps_status_error():
    def h(_req):
        return httpx.Response(502, json={"error": "execution error"})

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsStatusError) as ei:
                await c.purchase(_valid_prediction())
            assert ei.value.status_code == 502

    run(go())


# ---- resolve_symbol ------------------------------------------------------- #
def _instruments_handler(items):
    def h(_req):
        return httpx.Response(200, json=items)

    return h


def test_resolve_exact_match():
    h = _instruments_handler(
        [
            {"symbol": "GOLD", "tradeable": True, "session_type": 1},
            {"symbol": "GOLDMINI", "tradeable": True, "session_type": 1},
        ]
    )

    async def go():
        async with make_client(h) as c:
            inst = await c.resolve_symbol("gold")
            assert inst.symbol == "GOLD"

    run(go())


def test_resolve_single_candidate():
    h = _instruments_handler([{"symbol": "XAUUSD", "tradeable": True, "session_type": 1}])

    async def go():
        async with make_client(h) as c:
            inst = await c.resolve_symbol("gold")
            assert inst.symbol == "XAUUSD"

    run(go())


def test_resolve_ambiguous_raises():
    h = _instruments_handler(
        [
            {"symbol": "GOLD.spot", "tradeable": True, "session_type": 1},
            {"symbol": "GOLDMINI", "tradeable": True, "session_type": 1},
        ]
    )

    async def go():
        async with make_client(h) as c:
            with pytest.raises(AmbiguousSymbolError):
                await c.resolve_symbol("gold")

    run(go())


def test_resolve_none_raises_404():
    h = _instruments_handler([])

    async def go():
        async with make_client(h) as c:
            with pytest.raises(TradingHandsStatusError) as ei:
                await c.resolve_symbol("gold")
            assert ei.value.status_code == 404

    run(go())
