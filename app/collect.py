"""Phase 1 collector: fetch closed OHLCV, compute MTF features, persist a snapshot.

The whole packet is anchored to a single as_of = the close of the last eligible M15
bar ACTUALLY RETURNED BY THE PROVIDER. Each timeframe's window is fetched once and
sliced locally per as_of.

Eligibility (recent-window gaps + freshness) is computed separately from full-window
audit quality. A structurally-valid snapshot is ALWAYS persisted, even when
ineligible — with its reasons. The fail-closed gate is applied before the LLM call
(Phase 2), not by dropping the observation.

    python -m app.collect                 # live provider, online mode, persist
    python -m app.collect --replay        # freshness vs as_of (historical/dev)
    python -m app.collect --csv <dir>     # offline CSV source
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from brokers_bridge.trading_hands import TradingHandsClient, TradingHandsError
from config.settings import Settings, load_settings
from data_collector.providers.base import Candle, MarketDataProvider, only_closed
from data_collector.providers.csv_provider import CsvMarketDataProvider
from data_collector.providers.factory import build_provider
from data_collector.session import DEFAULT_CALENDAR, calendar_for
from database.repository import insert_evaluation, upsert_snapshot
from features.eligibility import EligibilityConfig, EligibilityResult, evaluate_eligibility
from features.mtf import TRIGGER_TF, FeaturePacket, build_feature_packet

BARS_PER_TF = 250  # >= MIN_BARS (200) with headroom


async def fetch_windows(provider, provider_symbol, timeframes, now, bars=BARS_PER_TF) -> dict[str, list[Candle]]:
    return {tf: only_closed(await provider.get_ohlcv(provider_symbol, tf, bars), now) for tf in timeframes}


def m15_closes(windows: dict[str, list[Candle]]) -> list[datetime]:
    return [c.close_time for c in windows.get(TRIGGER_TF, [])]


def slice_to_as_of(windows: dict[str, list[Candle]], as_of: datetime) -> dict[str, list[Candle]]:
    return {tf: [c for c in cs if c.close_time <= as_of] for tf, cs in windows.items()}


def build_packet_from_windows(
    windows, as_of, *, brain_symbol, provider_name, provider_symbol, ingested_at,
    spread_pct=None, basis_observed=None, news_digest=None, calendar=None,
) -> FeaturePacket:
    cal = calendar or calendar_for(provider_name)
    return build_feature_packet(
        brain_symbol, slice_to_as_of(windows, as_of), as_of=as_of, provider=provider_name,
        provider_symbol=provider_symbol, ingested_at=ingested_at, spread_pct=spread_pct,
        basis_observed=basis_observed, news_digest=news_digest, calendar=cal,
    )


def _eligibility_config(settings: Settings) -> EligibilityConfig:
    return EligibilityConfig(
        recent_window_bars=settings.eligibility_recent_window_bars,
        max_feed_lag_seconds=settings.eligibility_max_feed_lag_seconds,
        max_quote_lag_seconds=settings.eligibility_max_quote_lag_seconds,
    )


def compute_eligibility(
    windows, as_of, settings: Settings, *, mode: str, now: datetime,
    quote_time: datetime | None = None, provider_name: str | None = None,
) -> EligibilityResult:
    """Contextual verdict for this bar — separate from the (immutable) snapshot. Uses the
    provider's own market calendar (XTB and Polygon have different session boundaries)."""
    cal = calendar_for(provider_name) if provider_name else DEFAULT_CALENDAR
    return evaluate_eligibility(
        slice_to_as_of(windows, as_of), TRIGGER_TF, as_of, mode=mode, now=now,
        config=_eligibility_config(settings), quote_time=quote_time, evaluated_at=now, calendar=cal,
    )


def compute_basis(feed_price, bid, ask, bar_close, observed_at, quote_time, *,
                  max_lag_seconds: float) -> dict:
    """Feed-vs-broker basis at a bar. RELIABLE only when the quote is observed close to the
    bar close: beyond `max_lag_seconds` the (feed_price - broker_mid) difference is dominated
    by price MOVEMENT between the two instants, not a genuine feed-vs-broker basis — so the
    basis magnitudes are withheld and `basis_reliable=False`. The instantaneous spread is
    always kept (it does not depend on the lag)."""
    mid = (bid + ask) / 2 if bid and ask else None
    lag = (observed_at - bar_close).total_seconds()
    reliable = lag <= max_lag_seconds
    return {
        "feed_price": feed_price, "xtb_bid": bid, "xtb_ask": ask,
        "xtb_spread_pct": round((ask - bid) / ask * 100, 4) if ask else 0.0,
        "quote_time": quote_time.isoformat() if quote_time else None,
        "observed_at": observed_at.isoformat(), "bar_close": bar_close.isoformat(),
        "observation_lag_seconds": round(lag, 1),
        "basis_reliable": reliable,
        "basis_abs": round(feed_price - mid, 4) if (mid and reliable) else None,
        "basis_pct": round((feed_price - mid) / mid * 100, 4) if (mid and reliable) else None,
    }


async def observe_xtb_spread(settings, feed_price, bar_close) -> tuple[float | None, dict | None]:
    async with TradingHandsClient(settings.trading_hands_url, settings.http_timeout_seconds) as th:
        try:
            q = await th.quote(settings.symbol_query)
        except TradingHandsError:
            return None, None
    observed_at = datetime.now(timezone.utc)
    quote_time = datetime.fromtimestamp(q.time / 1000, tz=timezone.utc) if q.time else None
    basis = compute_basis(feed_price, q.bid, q.ask, bar_close, observed_at, quote_time,
                          max_lag_seconds=settings.max_basis_lag_seconds)
    return round(q.spread_pct, 4), basis


def _build(settings, csv_dir) -> tuple[MarketDataProvider, str]:
    if csv_dir:
        return CsvMarketDataProvider(csv_dir), "csv"
    return build_provider(settings), settings.market_data_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one MTF snapshot.")
    parser.add_argument("--csv", help="offline CSV source directory (overrides provider)")
    parser.add_argument("--symbol", help="override BRAIN_SYMBOL_QUERY")
    parser.add_argument("--replay", action="store_true", help="freshness vs as_of (not now)")
    parser.add_argument("--no-write", action="store_true", help="compute only; do not persist")
    parser.add_argument("--no-spread", action="store_true", help="skip the XTB spread read")
    args = parser.parse_args()

    settings = load_settings()
    mode = "replay" if args.replay else settings.market_mode
    brain_symbol = args.symbol or settings.symbol_query
    provider, provider_name = _build(settings, args.csv)
    provider_symbol = brain_symbol if args.csv else settings.provider_symbol(brain_symbol)

    async def _run() -> tuple[FeaturePacket, EligibilityResult]:
        now = datetime.now(timezone.utc)
        ingested_at = now
        try:
            windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
        finally:
            aclose = getattr(provider, "aclose", None)
            if aclose:
                await aclose()
        closes = m15_closes(windows)
        if not closes:
            raise ValueError(f"no closed {TRIGGER_TF} bars for {provider_symbol}")
        as_of = closes[-1]
        packet = build_packet_from_windows(
            windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
            provider_symbol=provider_symbol, ingested_at=ingested_at,
        )
        quote_time = None
        if not args.no_spread:
            spread_pct, basis = await observe_xtb_spread(settings, packet.price, packet.bar_close)
            if basis is not None:
                packet = packet.model_copy(update={"spread_pct": spread_pct, "basis_observed": basis})
                if basis.get("quote_time"):
                    quote_time = datetime.fromisoformat(basis["quote_time"])
        # Recapture the clock AFTER the quote so freshness is measured against a `now` that
        # is not earlier than the quote (a quote timestamped after `now` would look like the
        # future and is fail-closed by evaluate_eligibility).
        eval_now = datetime.now(timezone.utc)
        result = compute_eligibility(windows, as_of, settings, mode=mode, now=eval_now,
                                     quote_time=quote_time, provider_name=provider_name)
        return packet, result

    packet, result = asyncio.run(_run())
    print(
        f"[features] {packet.symbol} ({packet.provider}:{packet.provider_symbol}) @ {packet.bar_close.isoformat()} "
        f"price={packet.price} regime(H1)={packet.regime} confluence={packet.confluence} "
        f"spread%={packet.spread_pct} mode={mode} v={packet.pipeline_version}"
    )
    print(f"[eligibility] mode={result.mode} policy={result.policy_version} "
          f"eligible={result.eligible} reasons={result.reasons}")
    if args.no_write:
        return 0
    # The snapshot is an immutable observation -> ALWAYS persisted (ineligible or not).
    # Eligibility is a separate, mode+policy-stamped verdict; fail-closed is a DECISION
    # gate (Phase 2), never applied by dropping the observation.
    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    print(f"[db] {status} snapshot id={snap_id}")
    if snap_id is not None and status != "conflict":
        eval_id = insert_evaluation(settings.db_dsn, snap_id, result)
        print(f"[db] evaluation id={eval_id} persisted for snapshot {snap_id} "
              f"(mode={result.mode}, eligible={result.eligible})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
