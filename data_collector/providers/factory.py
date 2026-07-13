"""Provider selection by typed config (MVP composition, not a plugin registry)."""

from __future__ import annotations

from config.settings import Settings

from .base import MarketDataProvider


def build_provider(settings: Settings) -> MarketDataProvider:
    kind = settings.market_data_provider
    if kind == "polygon":
        if not settings.polygon_api_key:
            raise ValueError("BRAIN_POLYGON_API_KEY is required for the polygon provider")
        from .polygon import MassivePolygonProvider

        return MassivePolygonProvider(
            api_key=settings.polygon_api_key,
            base_url=settings.polygon_base_url,
            min_request_interval_seconds=settings.polygon_min_interval_seconds,
        )
    if kind == "csv":
        if not settings.csv_dir:
            raise ValueError("BRAIN_CSV_DIR is required for the csv provider")
        from .csv_provider import CsvMarketDataProvider

        return CsvMarketDataProvider(settings.csv_dir)
    if kind == "xtb":
        # Real-time OHLCV via trading_hands (which owns the XTB CoreAPI connection).
        from .xtb import XtbCandlesProvider

        return XtbCandlesProvider(
            base_url=settings.trading_hands_url,
            timeout_seconds=settings.http_timeout_seconds,
        )
    raise ValueError(f"unknown provider {kind!r}")
