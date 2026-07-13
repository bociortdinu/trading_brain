"""Smoke exit codes: PASS only on a real valid quote; demo guard; tradeable/session."""

from __future__ import annotations

import httpx

from app.smoke import FAIL, INCOMPLETE, PASS, smoke
from tests.helpers import make_client, run


def _router(*, environment="demo", instruments=None, quote=None, quote_status=200):
    instruments = instruments if instruments is not None else [
        {"symbol": "GOLD", "tradeable": True, "session_type": 1}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json={"connected": True, "account": "1", "environment": environment})
        if path.startswith("/instruments/"):
            return httpx.Response(200, json=instruments)
        if path.startswith("/quote/"):
            if quote_status != 200:
                return httpx.Response(quote_status, json={"error": "market closed"})
            return httpx.Response(200, json=quote)
        return httpx.Response(404, json={"error": "not found"})

    return handler


def test_smoke_pass():
    client = make_client(_router(quote={"symbol": "GOLD", "bid": 2400.0, "ask": 2400.5}))
    assert run(smoke(client, "gold")) == PASS


def test_smoke_demo_guard():
    client = make_client(_router(environment="real"))
    assert run(smoke(client, "gold")) == FAIL


def test_smoke_not_tradeable_fails():
    client = make_client(_router(instruments=[{"symbol": "GOLD", "tradeable": False, "session_type": 1}]))
    assert run(smoke(client, "gold")) == FAIL


def test_smoke_session_closed_incomplete():
    client = make_client(_router(instruments=[{"symbol": "GOLD", "tradeable": True, "session_type": 2}]))
    assert run(smoke(client, "gold")) == INCOMPLETE


def test_smoke_quote_503_incomplete():
    client = make_client(_router(quote_status=503))
    assert run(smoke(client, "gold")) == INCOMPLETE


def test_smoke_unresolved_symbol_confirms_via_quote():
    # /instruments cannot pin an exact match (commodity crowded out by stocks) but a
    # valid quote confirms the symbol -> PASS. We must NOT pick an arbitrary candidate.
    client = make_client(
        _router(
            instruments=[
                {"symbol": "GOLD.US", "tradeable": True, "session_type": 1},
                {"symbol": "IAG.US", "tradeable": True, "session_type": 1},
            ],
            quote={"symbol": "GOLD", "bid": 4118.35, "ask": 4120.7},
        )
    )
    assert run(smoke(client, "GOLD")) == PASS


def test_smoke_unresolved_symbol_quote_503_incomplete():
    client = make_client(
        _router(
            instruments=[
                {"symbol": "GOLD.US", "tradeable": True, "session_type": 1},
                {"symbol": "IAG.US", "tradeable": True, "session_type": 1},
            ],
            quote_status=503,
        )
    )
    assert run(smoke(client, "GOLD")) == INCOMPLETE


def test_smoke_invalid_quote_fails():
    client = make_client(_router(quote={"symbol": "GOLD", "bid": 0.0, "ask": 0.0}))
    assert run(smoke(client, "gold")) == FAIL


def test_smoke_unreachable_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert run(smoke(make_client(handler), "gold")) == FAIL
