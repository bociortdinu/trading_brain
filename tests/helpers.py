"""Shared test helpers. All tests use httpx.MockTransport — no real network / XTB."""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from brokers_bridge.trading_hands import TradingHandsClient

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> TradingHandsClient:
    return TradingHandsClient("http://test", 5.0, transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)
