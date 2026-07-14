"""Multi-timeframe aggregation into a compact FeaturePacket.

Roles (avoids the "tunnel effect"): D1 = macro bias, H4 = major trend,
H1 = structure/levels, M15 = execution trigger. Output is ~a few hundred tokens,
never raw candles.

D1 PARTICIPATES in confluence: an "aligned" call requires the D1 macro bias to
agree with the H4/H1 trend (see `confluence`). The whole packet is anchored to a
single `as_of` = the close of the last eligible M15 bar; every timeframe's last
bar and every news item must be <= as_of.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from data_collector.providers.base import Candle, validate_series
from data_collector.session import DEFAULT_CALENDAR, SessionCalendar, timeframe_quality
from features.engineering import MIN_BARS, timeframe_features
from features.version import FEATURE_PIPELINE_VERSION

# Role -> timeframe key (must exist in config.timeframes).
MACRO_TF = "1day"
TREND_TF = "4h"
STRUCTURE_TF = "1h"
TRIGGER_TF = "15min"


def confluence(macro: dict, trend: dict, structure: dict, trigger: dict) -> str:
    """Deterministic MTF-agreement label. D1 macro bias gates the 'aligned' calls."""
    major = trend["regime"]
    macro_bias = macro["ema_align"]
    if macro_bias == "up" and major == "bull_trend" and structure["regime"] == "bull_trend":
        return "aligned_bull"
    if macro_bias == "down" and major == "bear_trend" and structure["regime"] == "bear_trend":
        return "aligned_bear"
    if major == "bull_trend" and trigger["regime"] in ("range", "choppy"):
        return "bull_pullback"
    if major == "bear_trend" and trigger["regime"] in ("range", "choppy"):
        return "bear_pullback"
    return "mixed"


class FeaturePacket(BaseModel):
    symbol: str
    bar_close: datetime          # == as_of; deterministic idempotency key (last M15 close)
    price: float                 # data-feed price (last M15 close). NOT the XTB quote.
    spread_pct: float | None = None      # XTB spread at snapshot time (basis; see `basis`)
    # promoted (hot) columns
    regime: str                  # H1 (structure) regime -> snapshot.regime
    adx_h1: float | None = None
    atr_pct_m15: float | None = None
    # headline context
    macro_bias: str
    major_trend: str
    confluence: str
    # full nested features -> JSONB
    timeframes: dict[str, dict]
    news_digest: list[dict] | None = None
    # OBSERVED basis: feed price vs XTB bid/ask, WITH the observation latency it carries.
    basis_observed: dict | None = None
    # FULL-window data-quality (classified gaps + verdict) -> snapshot.data_quality (audit)
    data_quality: dict | None = None
    # NOTE: decision eligibility is NOT on the packet. It is a contextual, mode- and
    # policy-stamped verdict persisted separately (snapshot_evaluations); the SAME bar can
    # be eligible in replay and stale online. See features.eligibility.
    # provenance
    provider: str
    provider_symbol: str
    ingested_at: datetime
    interval_list: list[str]
    pipeline_version: str = FEATURE_PIPELINE_VERSION

    def features_json(self) -> dict:
        # basis_observed and data_quality are their own columns, NOT part of the
        # computed features JSON (so enrichment never rewrites features).
        return {
            "macro_bias": self.macro_bias,
            "major_trend": self.major_trend,
            "confluence": self.confluence,
            "price": self.price,
            "timeframes": self.timeframes,
        }


def build_feature_packet(
    symbol: str,
    tf_candles: dict[str, list[Candle]],
    *,
    as_of: datetime,
    provider: str,
    provider_symbol: str,
    ingested_at: datetime,
    spread_pct: float | None = None,
    news_digest: list[dict] | None = None,
    basis_observed: dict | None = None,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> FeaturePacket:
    for role in (MACRO_TF, TREND_TF, STRUCTURE_TF, TRIGGER_TF):
        if role not in tf_candles:
            raise ValueError(f"missing candles for timeframe {role!r}")

    # Enforce the single-as_of anchor: no bar may close after as_of.
    for name, candles in tf_candles.items():
        if not candles:
            raise ValueError(f"no candles for timeframe {name!r}")
        if candles[-1].close_time > as_of:
            raise ValueError(f"{name} last bar {candles[-1].close_time} is after as_of {as_of}")

    trigger_last = tf_candles[TRIGGER_TF][-1]
    if trigger_last.close_time != as_of:
        raise ValueError(f"as_of {as_of} must equal last M15 close {trigger_last.close_time}")

    # FULL-window data-quality (audit): classify every gap on every timeframe.
    data_quality: dict[str, dict] = {}
    for name, candles in tf_candles.items():
        gaps = validate_series(candles, name)
        data_quality[name] = timeframe_quality(len(candles), MIN_BARS, gaps, name, calendar)

    tf = {name: timeframe_features(candles) for name, candles in tf_candles.items()}
    macro, trend, structure, trigger = tf[MACRO_TF], tf[TREND_TF], tf[STRUCTURE_TF], tf[TRIGGER_TF]

    return FeaturePacket(
        symbol=symbol,
        bar_close=as_of,
        price=float(trigger_last.close),
        spread_pct=spread_pct,
        regime=structure["regime"],
        adx_h1=structure["adx"],
        atr_pct_m15=trigger["atr_pct"],
        macro_bias=macro["ema_align"],
        major_trend=trend["regime"],
        confluence=confluence(macro, trend, structure, trigger),
        timeframes=tf,
        news_digest=news_digest,
        basis_observed=basis_observed,
        data_quality=data_quality,
        provider=provider,
        provider_symbol=provider_symbol,
        ingested_at=ingested_at,
        interval_list=list(tf_candles.keys()),
    )
