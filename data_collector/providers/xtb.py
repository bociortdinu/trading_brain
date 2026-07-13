"""XTB real-time OHLCV via trading_hands /candles.

trading_hands owns the authenticated XTB xStation CoreAPI connection; it exposes CLOSED,
UTC, on-grid OHLCV bars over plain HTTP. trading_brain therefore stays provider-agnostic:
this class is just another MarketDataProvider, so selecting XTB is ONE config flip
(BRAIN_MARKET_DATA_PROVIDER=xtb) — the MTF pipeline, eligibility and decision layers are
untouched. Free, real-time, and the SAME venue we execute on (no feed-vs-broker basis).

Modifiability boundary: the XTB CoreAPI chart command lives ONLY inside trading_hands. If
XTB changes it, we touch one Go method — not trading_brain. This provider only maps the
clean HTTP rows into Candles and enforces the temporal contract (closed + on-grid).

HTTP contract expected from trading_hands:
    GET /candles/{symbol}/{period}?count=N   (period in {M15,H1,H4,D1})
    -> {"symbol","period","candles":[{"t":<open_ms_utc>,"o","h","l","c","v"}, ...]}
       oldest-first, CLOSED bars only.

Two XTB specifics (validated live 2026-07-13):
- Symbol is `GOLD` (not Polygon's `C:XAUUSD`). With this provider set
  `BRAIN_PROVIDER_SYMBOL_MAP='{"GOLD":"GOLD"}'` (or empty) so GOLD is not remapped.
- M15/H1 are UTC-epoch aligned, but H4/D1 are anchored to the BROKER trading day
  (D1 opens 22:00 UTC in summer). The temporal contract enforces the UTC-epoch grid only
  for the scheduler-critical tf (see _EPOCH_ALIGNED_TFS in providers/base.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from .base import Candle, MarketDataProvider, SeriesGap, timeframe_minutes, validate_series
from .polygon import ProviderError  # shared "provider failure" type (scheduler treats it transient)

# trading_brain timeframe -> XTB period code (trading_hands maps this to the CoreAPI).
_TF_TO_XTB_PERIOD: dict[str, str] = {"15min": "M15", "1h": "H1", "4h": "H4", "1day": "D1"}


class XtbCandlesProvider(MarketDataProvider):
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        now_fn=None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_gaps: list[SeriesGap] = []

    async def __aenter__(self) -> "XtbCandlesProvider":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _to_candle(self, row: dict, tf_min: int) -> Candle:
        try:
            open_time = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
            return Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=tf_min),
                open=row["o"], high=row["h"], low=row["l"], close=row["c"],
                volume=row.get("v", 0.0) or 0.0,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed candle row: {exc}") from exc

    async def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        if timeframe not in _TF_TO_XTB_PERIOD:
            raise ProviderError(f"unsupported timeframe {timeframe!r}")
        period = _TF_TO_XTB_PERIOD[timeframe]
        tf_min = timeframe_minutes(timeframe)
        try:
            resp = await self._client.get(f"/candles/{symbol}/{period}", params={"count": count})
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"trading_hands /candles HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"trading_hands /candles request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"invalid /candles JSON: {exc}") from exc

        rows = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ProviderError("/candles response is not a list")
        candles = [self._to_candle(r, tf_min) for r in rows]
        now = self._now()
        candles = [c for c in candles if c.close_time <= now]  # never a forming bar
        try:
            # Same temporal contract as Polygon: bars must be on the canonical UTC grid.
            self.last_gaps = validate_series(candles, timeframe)
        except ValueError as exc:
            raise ProviderError(f"series contract violation ({timeframe}): {exc}") from exc
        return candles[-count:] if count else candles
