"""CSV OHLCV provider — for offline development, deterministic tests, and replay.

Reads files named `{SYMBOL}_{timeframe}.csv` from a directory, with header:
    open_time,close_time,open,high,low,close,volume
Timestamps are ISO-8601. Rows must be sorted oldest-first (validated).
All rows are treated as closed bars (a CSV is historical by construction).
"""

from __future__ import annotations

import csv
import pathlib
from datetime import datetime

from .base import Candle, MarketDataProvider


class CsvMarketDataProvider(MarketDataProvider):
    def __init__(self, directory: str | pathlib.Path) -> None:
        self._dir = pathlib.Path(directory)

    def _path(self, symbol: str, timeframe: str) -> pathlib.Path:
        return self._dir / f"{symbol}_{timeframe}.csv"

    async def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        path = self._path(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"no CSV for {symbol} {timeframe}: {path}")
        candles: list[Candle] = []
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                candles.append(
                    Candle(
                        open_time=datetime.fromisoformat(row["open_time"]),
                        close_time=datetime.fromisoformat(row["close_time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0) or 0),
                    )
                )
        for a, b in zip(candles, candles[1:]):
            if b.open_time < a.open_time:
                raise ValueError(f"{path} is not sorted oldest-first")
        return candles[-count:] if count else candles
