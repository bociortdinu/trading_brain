"""Massive / Polygon.io aggregates provider (REST, httpx).

Uses the Polygon aggregates API shape:
    GET /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from_ms}/{to_ms}
Handles pagination (next_url), 429 + 5xx retry with bounded backoff (robust
Retry-After), timeouts, invalid responses, and reports (does NOT fill) gaps.

Safety:
- next_url is followed ONLY if its scheme/host/port match the configured base_url
  origin; the API key is never attached to a different origin.
- If max_pages is reached while a next_url is still present, raises ProviderError
  rather than returning partial data.
- Async context manager: `async with MassivePolygonProvider(...) as p:` closes the
  HTTP client on exit / cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .base import (
    Candle,
    MarketDataProvider,
    SeriesGap,
    floor_to_grid,
    timeframe_minutes,
    validate_series,
)

log = logging.getLogger(__name__)

_TF_TO_POLYGON: dict[str, tuple[int, str]] = {
    "15min": (15, "minute"),
    "1h": (1, "hour"),
    "4h": (4, "hour"),
    "1day": (1, "day"),
}


class ProviderError(RuntimeError):
    """Any unrecoverable failure fetching/parsing provider data."""


def _origin(url: str) -> tuple[str, str | None, int]:
    s = urlsplit(url)
    port = s.port or (443 if s.scheme == "https" else 80)
    return (s.scheme, s.hostname, port)


class MassivePolygonProvider(MarketDataProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.polygon.io",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        max_pages: int = 10,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        min_request_interval_seconds: float = 0.0,
        transport: httpx.BaseTransport | None = None,
        now_fn=None,
        sleep_fn=None,
    ) -> None:
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds, transport=transport)
        self._allowed_origin = _origin(base_url)
        self._max_retries = max_retries
        self._max_pages = max_pages
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        # Client-side rate limiting: minimum spacing between HTTP requests. Set > 0 for
        # free/low-tier keys (e.g. ~13s respects a 5-requests/minute limit).
        self._min_interval = min_request_interval_seconds
        self._last_request_at = 0.0
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn or asyncio.sleep
        self.last_gaps: list[SeriesGap] = []

    async def _throttle(self) -> None:
        if self._min_interval > 0:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = time.monotonic()

    async def __aenter__(self) -> "MassivePolygonProvider":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def _retry_after_seconds(self, value: str | None) -> float | None:
        """Parse a Retry-After header (delta-seconds or HTTP-date). None if absent/invalid."""
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (dt - self._now()).total_seconds())
            except (TypeError, ValueError):
                return None

    async def _get_json(self, url: str, params: dict | None) -> dict:
        backoff = self._backoff_base
        for attempt in range(self._max_retries + 1):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    await self._sleep(min(backoff, self._backoff_cap))
                    backoff *= 2
                    continue
                raise ProviderError(f"timeout after {attempt + 1} attempts: {exc}") from exc
            except httpx.RequestError as exc:
                raise ProviderError(f"request error: {exc}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self._max_retries:
                    retry_after = self._retry_after_seconds(resp.headers.get("Retry-After"))
                    if retry_after is not None:
                        await self._sleep(retry_after)  # honor a valid Retry-After IN FULL (uncapped)
                    else:
                        await self._sleep(min(backoff, self._backoff_cap))
                        backoff *= 2
                    continue
                raise ProviderError(f"HTTP {resp.status_code} after {attempt + 1} attempts")
            if resp.status_code != 200:
                raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"invalid JSON: {exc}") from exc
        raise ProviderError("exhausted retries")  # unreachable

    def _to_candle(self, row: dict, tf_min: int) -> Candle:
        try:
            open_time = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
            return Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=tf_min),
                open=row["o"],
                high=row["h"],
                low=row["l"],
                close=row["c"],
                volume=row.get("v", 0.0) or 0.0,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed aggregate row: {exc}") from exc

    def _next_url(self, raw: str | None) -> str | None:
        if not raw:
            return None
        if _origin(raw) != self._allowed_origin:
            raise ProviderError(f"next_url origin not allowlisted: {raw!r}")
        return str(httpx.URL(raw).copy_merge_params({"apiKey": self._key}))

    async def _paged(self, path: str, params: dict, tf_min: int) -> list[Candle]:
        candles: list[Candle] = []
        url: str | None = path
        req_params: dict | None = params
        pages = 0
        while url and pages < self._max_pages:
            # params=None on follow-ups: httpx REPLACES an existing URL query with any
            # non-None params, which would wipe cursor + apiKey.
            data = await self._get_json(url, req_params)
            status = data.get("status")
            if status not in ("OK", "DELAYED"):
                raise ProviderError(f"polygon status {status!r}: {data.get('error') or data.get('message')}")
            for row in data.get("results") or []:
                candles.append(self._to_candle(row, tf_min))
            url = self._next_url(data.get("next_url"))
            req_params = None
            pages += 1
        if url is not None:  # stopped on max_pages with more data -> refuse partial
            raise ProviderError(f"max_pages={self._max_pages} reached with next_url still present")
        return candles

    async def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        if timeframe not in _TF_TO_POLYGON:
            raise ProviderError(f"unsupported timeframe {timeframe!r}")
        mult, span = _TF_TO_POLYGON[timeframe]
        tf_min = timeframe_minutes(timeframe)
        now = self._now()
        # Deterministic request: floor `from` to the canonical grid so the sub-bar phase
        # of `now` never leaks into the buckets (that phase leak is what produced the
        # off-grid :08 close). The offset is a whole multiple of the step, so the same bar
        # always yields the same `from` regardless of when within the bar we ask.
        anchor = floor_to_grid(now, timeframe)
        frm = anchor - timedelta(minutes=tf_min) * (count * 3)  # buffer for session gaps
        path = f"/v2/aggs/ticker/{symbol}/range/{mult}/{span}/{self._ms(frm)}/{self._ms(now)}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self._key}

        candles = await self._paged(path, params, tf_min)
        candles = [c for c in candles if c.close_time <= now]  # never return a forming bar
        try:
            # Verifies bar duration, ordering AND on-grid alignment. A phase-shifted /
            # off-grid series is a contract violation -> fail closed, never accept silently.
            self.last_gaps = validate_series(candles, timeframe)
        except ValueError as exc:
            raise ProviderError(f"series contract violation ({timeframe}): {exc}") from exc
        if self.last_gaps:
            log.warning("%s %s: %d gap(s) in series (not filled)", symbol, timeframe, len(self.last_gaps))
        return candles[-count:] if count else candles
