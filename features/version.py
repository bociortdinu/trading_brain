"""Pipeline version — bump on any change to indicators/engineering/mtf semantics.

Stored on every snapshot for reproducibility; a change means features computed
before/after are not directly comparable.
"""

FEATURE_PIPELINE_VERSION = "1.2.0"  # 1.2.0: added ema50_slope_pct per timeframe
