"""Freeze the current historical bar set as an immutable, content-hashed DATASET for reproducible
replay. NO execution, NO paid AI calls — it only reads bars and records a `datasets` row.

A replay pinned to a dataset_id is reproducible: re-freezing the SAME bytes yields the SAME id, so
you can prove a backtest ran on exactly this data.

    python -m app.freeze_dataset --count 2500 --source "polygon backfill 2026-07 / XAUUSD"
"""

from __future__ import annotations

import argparse
import asyncio

from config.settings import load_settings
from data_collector.providers.factory import build_provider
from database.repository import freeze_dataset, get_dataset


async def _run(settings, count: int, source: str | None) -> int:
    from features.version import FEATURE_PIPELINE_VERSION

    provider = build_provider(settings)
    symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(symbol)
    try:
        windows = {tf: await provider.get_ohlcv(provider_symbol, tf, count)
                   for tf in settings.timeframes}
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()

    dataset_id, newly = freeze_dataset(
        settings.db_dsn, symbol=symbol, provider=settings.market_data_provider,
        provider_symbol=provider_symbol, windows=windows,
        pipeline_version=FEATURE_PIPELINE_VERSION, source=source,
        provenance={"count": count, "provider": settings.market_data_provider})
    ds = get_dataset(settings.db_dsn, dataset_id)
    print(f"dataset_id = {dataset_id}  ({'NEWLY FROZEN' if newly else 'already frozen (same bytes)'})")
    print(f"  symbol={symbol} provider={settings.market_data_provider} provider_symbol={provider_symbol}")
    print(f"  timeframes={ds['timeframes']} bar_counts={ds['bar_counts']}")
    print(f"  first_bar={ds['first_bar']} last_bar={ds['last_bar']}")
    print(f"  sha256={ds['sha256']}")
    print("Pin a replay to this dataset_id to make the run reproducible.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a reproducible replay dataset (no execution).")
    parser.add_argument("--count", type=int, default=2500, help="bars per timeframe to fetch + freeze")
    parser.add_argument("--source", help="human description of where the data came from")
    args = parser.parse_args()
    settings = load_settings()
    return asyncio.run(_run(settings, args.count, args.source))


if __name__ == "__main__":
    raise SystemExit(main())
