"""Phase 0 smoke test.

Confirms trading_brain can reach trading_hands and read real data:
  1. GET /status            -> connected + demo
  2. GET /instruments/{q}   -> resolve exact tradable symbol (unambiguous), if possible
  3. GET /quote/{symbol}    -> real, valid bid/ask + spread  (the authoritative check)

Note on symbol resolution: trading_hands' /instruments search caps at 20 fuzzy
matches and does not prioritise exact-symbol matches, so a commodity like GOLD is
crowded out by gold-mining stocks and is NOT surfaced. The instrument still exists
(GET /quote/GOLD resolves it internally). We therefore try /instruments first (and,
when it yields an exact match, enforce tradeable + session_type), but never pick an
arbitrary candidate; when it can't pin the symbol we confirm via /quote instead.

Exit codes:
  0  PASS        -- and ONLY when a real, valid quote (bid>0, ask>0) was obtained
  1  FAIL        -- unreachable, not connected, not demo, not tradeable,
                    invalid quote, or any hard error
  2  INCOMPLETE  -- connectivity OK but market closed now (session closed / 503):
                    re-run during market hours

Run (with trading_hands already running):
    python -m app.smoke

No orders are placed.
"""

from __future__ import annotations

import asyncio
import sys

from config.settings import load_settings
from brokers_bridge.trading_hands import (
    AmbiguousSymbolError,
    Instrument,
    TradingHandsClient,
    TradingHandsError,
    TradingHandsStatusError,
    TradingHandsTimeout,
    TradingHandsUnreachable,
)

PASS, FAIL, INCOMPLETE = 0, 1, 2


async def smoke(client: TradingHandsClient, symbol_query: str) -> int:
    # 1. status ------------------------------------------------------------- #
    try:
        status = await client.status()
    except TradingHandsUnreachable as exc:
        print(f"[FAIL] trading_hands unreachable: {exc}", file=sys.stderr)
        print("       Start it (operator): docker compose up -d db && cd trading_hands/browser-auth && npm start", file=sys.stderr)
        return FAIL
    except TradingHandsTimeout as exc:
        print(f"[FAIL] trading_hands timed out: {exc}", file=sys.stderr)
        return FAIL
    except TradingHandsError as exc:
        print(f"[FAIL] status error: {exc}", file=sys.stderr)
        return FAIL

    print(f"[ok] status: connected={status.connected} account={status.account} env={status.environment}")
    if not status.connected:
        print("[FAIL] trading_hands reports not connected", file=sys.stderr)
        return FAIL
    if status.environment != "demo":  # demo guard
        print(f"[FAIL] expected demo environment, got {status.environment!r}", file=sys.stderr)
        return FAIL

    # 2. resolve symbol (best-effort; never pick an arbitrary candidate) ----- #
    inst: Instrument | None = None
    try:
        inst = await client.resolve_symbol(symbol_query)
    except AmbiguousSymbolError as exc:
        print(f"[note] /instruments could not pin {symbol_query!r} ({len(exc.candidates)} candidates); confirming via /quote")
    except TradingHandsStatusError as exc:
        if exc.status_code == 404:
            print(f"[note] /instruments returned no matches for {symbol_query!r}; confirming via /quote")
        else:
            print(f"[FAIL] instrument lookup error: {exc}", file=sys.stderr)
            return FAIL
    except TradingHandsError as exc:
        print(f"[FAIL] instrument lookup error: {exc}", file=sys.stderr)
        return FAIL

    symbol = symbol_query
    if inst is not None:
        symbol = inst.symbol
        print(
            f"[ok] resolved {symbol_query!r} -> {inst.symbol} "
            f"(tradeable={inst.tradeable}, session_type={inst.session_type}, "
            f"min_vol={inst.min_volume}, step={inst.volume_step})"
        )
        if not inst.tradeable:
            print(f"[FAIL] {inst.symbol} is not tradeable", file=sys.stderr)
            return FAIL
        if inst.session_type != 1:
            print(f"[INCOMPLETE] {inst.symbol} session is closed (session_type={inst.session_type})")
            return INCOMPLETE

    # 3. quote (authoritative: real symbol + market-open) ------------------- #
    try:
        quote = await client.quote(symbol)
    except TradingHandsStatusError as exc:
        if exc.status_code == 503:
            print(f"[INCOMPLETE] quote 503 for {symbol!r}: market closed / symbol unknown ({exc.message})")
            return INCOMPLETE
        print(f"[FAIL] quote error: {exc}", file=sys.stderr)
        return FAIL
    except TradingHandsError as exc:
        print(f"[FAIL] quote error: {exc}", file=sys.stderr)
        return FAIL

    if not (quote.bid > 0 and quote.ask > 0):
        print(f"[FAIL] invalid quote: bid={quote.bid} ask={quote.ask}", file=sys.stderr)
        return FAIL

    print(f"[ok] quote {quote.symbol}: bid={quote.bid} ask={quote.ask} spread={quote.spread_pct:.4f}%")
    print("[PASS] Phase 0 smoke complete (real symbol + valid quote).")
    return PASS


def main() -> int:
    settings = load_settings()

    async def _run() -> int:
        async with TradingHandsClient(settings.trading_hands_url, settings.http_timeout_seconds) as client:
            return await smoke(client, settings.symbol_query)

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
