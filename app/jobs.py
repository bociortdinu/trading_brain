"""M15-close scheduler for the collector.

- `latest_available` is the last M15 bar the PROVIDER returned (not floor_m15(now));
  weekends / session breaks / publication delay are never "missing bars".
- WindowCache: each timeframe is refetched at most once per (configurable) TTL, so a
  delayed feed or a closed market does not re-download H1/H4/D1 on every M15 tick.
- The latest bar is evaluated ONLINE (freshness vs now); backfilled bars are evaluated
  as REPLAY (freshness vs their own as_of). A structurally-valid snapshot is always
  persisted, ineligible or not.
- Transient tick errors are swallowed; unexpected errors escalate with a traceback.

    python -m app.jobs --once      # process due bar(s) then exit
    python -m app.jobs             # run forever, waking at each M15 close
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.collect import build_packet_from_windows, compute_eligibility, m15_closes, observe_xtb_spread
from config.settings import Settings, load_settings
from data_collector.providers.base import MarketDataProvider, only_closed, timeframe_minutes
from data_collector.providers.factory import build_provider
from data_collector.providers.polygon import ProviderError
from database.repository import (
    insert_evaluation,
    latest_snapshot_bar_close,
    snapshot_enrichment_status,
    upsert_snapshot,
)

log = logging.getLogger(__name__)
M15 = timedelta(minutes=15)
BARS_PER_TF = 250


def _transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [ProviderError]
    try:
        import psycopg
        types.append(psycopg.OperationalError)
    except ImportError:
        pass
    try:
        import httpx
        types.append(httpx.TransportError)
    except ImportError:
        pass
    return tuple(types)


TRANSIENT = _transient_types()


def floor_m15(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def next_m15(dt: datetime) -> datetime:
    return floor_m15(dt) + M15


def select_targets(closes: list[datetime], last_done: datetime | None, max_backfill: int) -> list[datetime]:
    if not closes:
        return []
    if last_done is None:
        return [closes[-1]]
    targets = [c for c in closes if c > last_done]
    if len(targets) > max_backfill:
        log.warning("scheduler: %d new bars > max_backfill=%d; skipping %d oldest",
                    len(targets), max_backfill, len(targets) - max_backfill)
        targets = targets[-max_backfill:]
    return targets


class WindowCache:
    """Refetch each timeframe at most once per TTL (default = the bar duration), so a
    delayed feed / closed market doesn't re-download H1/H4/D1 on every M15 tick.
    TTL is measured against the scheduler's `now`, tracked as last_attempt_at."""

    def __init__(self, ttl_seconds: dict[str, float] | None = None) -> None:
        self._cache: dict[str, tuple[list, datetime]] = {}  # tf -> (window, last_attempt_at)
        self._ttl = ttl_seconds or {}

    def _ttl_for(self, tf: str) -> float:
        return self._ttl.get(tf, timeframe_minutes(tf) * 60.0)

    async def window(self, provider: MarketDataProvider, symbol: str, tf: str, bars: int, now: datetime) -> list:
        cached = self._cache.get(tf)
        if cached is not None and (now - cached[1]).total_seconds() < self._ttl_for(tf):
            return cached[0]
        window = only_closed(await provider.get_ohlcv(symbol, tf, bars), now)
        self._cache[tf] = (window, now)
        return window


async def _finalize_and_store(settings, windows, as_of, *, brain_symbol, provider_name,
                              provider_symbol, ingested_at, mode, now, observe_spread: bool) -> str:
    packet = build_packet_from_windows(
        windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
        provider_symbol=provider_symbol, ingested_at=ingested_at,
    )
    quote_time = None
    if observe_spread:
        spread_pct, basis = await observe_xtb_spread(settings, packet.price, packet.bar_close)
        if basis is not None:
            packet = packet.model_copy(update={"spread_pct": spread_pct, "basis_observed": basis})
            if basis.get("quote_time"):
                quote_time = datetime.fromisoformat(basis["quote_time"])
    # Persist the immutable observation first, then its separate mode+policy eligibility.
    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    if snap_id is not None and status != "conflict":
        result = compute_eligibility(windows, as_of, settings, mode=mode, now=now, quote_time=quote_time)
        insert_evaluation(settings.db_dsn, snap_id, result)
    return status


async def catch_up(settings: Settings, provider: MarketDataProvider, provider_name: str,
                   max_backfill: int = 8, cache: WindowCache | None = None) -> dict:
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)
    now = datetime.now(timezone.utc)

    if cache is not None:
        windows = {tf: await cache.window(provider, provider_symbol, tf, BARS_PER_TF, now)
                   for tf in settings.timeframes}
    else:
        windows = {tf: only_closed(await provider.get_ohlcv(provider_symbol, tf, BARS_PER_TF), now)
                   for tf in settings.timeframes}

    closes = m15_closes(windows)
    last_done = latest_snapshot_bar_close(settings.db_dsn, brain_symbol)
    targets = select_targets(closes, last_done, max_backfill)
    latest = closes[-1] if closes else None

    results: dict[str, int] = {}
    for as_of in targets:
        # latest bar -> online (spread + freshness vs now); backfill -> replay (vs as_of)
        is_latest = as_of == latest
        status = await _finalize_and_store(
            settings, windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
            provider_symbol=provider_symbol, ingested_at=now,
            mode=(settings.market_mode if is_latest else "replay"), now=now, observe_spread=is_latest,
        )
        results[status] = results.get(status, 0) + 1
        log.info("bar %s -> %s", as_of.isoformat(), status)

    # Enrichment retry: latest already stored but still missing spread/basis.
    if latest is not None and latest not in targets:
        exists, needs = snapshot_enrichment_status(settings.db_dsn, brain_symbol, latest)
        if exists and needs:
            status = await _finalize_and_store(
                settings, windows, latest, brain_symbol=brain_symbol, provider_name=provider_name,
                provider_symbol=provider_symbol, ingested_at=now,
                mode=settings.market_mode, now=now, observe_spread=True,
            )
            results[f"retry_{status}"] = results.get(f"retry_{status}", 0) + 1
            log.info("enrichment retry %s -> %s", latest.isoformat(), status)
    return results


async def safe_catch_up(settings: Settings, provider: MarketDataProvider, provider_name: str,
                        max_backfill: int = 8, cache: WindowCache | None = None) -> dict:
    """Transient errors keep the loop alive; unexpected errors escalate with a traceback."""
    try:
        return await catch_up(settings, provider, provider_name, max_backfill, cache)
    except TRANSIENT as exc:
        log.warning("scheduler tick: transient error (continuing): %s", exc)
        return {"error": str(exc)}
    except Exception:
        log.exception("scheduler tick: UNEXPECTED error (escalating)")
        raise


async def run_scheduler(settings: Settings, offset_seconds: float = 5.0) -> None:
    provider = build_provider(settings)
    provider_name = settings.market_data_provider
    cache = WindowCache()
    try:
        while True:
            await safe_catch_up(settings, provider, provider_name, cache=cache)
            wake = next_m15(datetime.now(timezone.utc)) + timedelta(seconds=offset_seconds)
            await asyncio.sleep(max(1.0, (wake - datetime.now(timezone.utc)).total_seconds()))
    finally:  # runs on CancelledError too
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="M15-close collector scheduler.")
    parser.add_argument("--once", action="store_true", help="process due bar(s) then exit")
    args = parser.parse_args()

    settings = load_settings()
    if args.once:
        provider = build_provider(settings)

        async def _once() -> dict:
            try:
                return await catch_up(settings, provider, settings.market_data_provider)
            finally:
                aclose = getattr(provider, "aclose", None)
                if aclose:
                    await aclose()

        print(f"[scheduler] {asyncio.run(_once())}")
        return 0
    asyncio.run(run_scheduler(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
