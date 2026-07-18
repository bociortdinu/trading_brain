"""Decision eligibility — a CONTEXTUAL verdict, kept separate from the market snapshot.

The market snapshot (features/mtf.py) is the IMMUTABLE observation of a bar; it never
changes. Eligibility is a separate, mode- and policy-stamped judgment about whether that
bar may drive a decision *right now*. The SAME bar can be:
  - eligible in "replay"  (freshness measured against as_of), and
  - stale/ineligible in "online" (freshness measured against the wall clock),
so eligibility is persisted as its own record per (snapshot, mode, policy_version)
— never as a single boolean on the snapshot that one mode could overwrite.

Checks:
- feed freshness of the last M15 bar (online: vs now; replay: vs as_of, fresh by design),
- online XTB quote must be PRESENT and fresh (a missing quote fails closed), and
- unexpected gaps in a RECENT per-timeframe window (a stale old holiday must not block now).

Fail-closed: an ineligible bar is still PERSISTED (snapshot + evaluation, with reasons);
the gate is applied before the LLM call and before execution, not by dropping data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from data_collector.providers.base import Candle, validate_series
from data_collector.session import DEFAULT_CALENDAR, UNEXPECTED_MISSING_BAR, XauUsdCalendar, classify_gap

Mode = Literal["online", "replay"]

# Bump when the eligibility LOGIC changes, independently of the thresholds below.
ELIGIBILITY_LOGIC_VERSION = "elig-2026.1"


class EligibilityConfig(BaseModel):
    # How many recent bars per timeframe are decision-relevant.
    recent_window_bars: dict[str, int] = Field(
        default_factory=lambda: {"15min": 8, "1h": 6, "4h": 4, "1day": 3}
    )
    max_feed_lag_seconds: int = 1800   # online: last M15 bar must be this fresh vs now
    max_quote_lag_seconds: int = 120   # online: XTB quote must be this fresh vs now
    max_clock_skew_seconds: int = 5    # a quote/bar timestamped AFTER now by more than this
    #                                    is fail-closed (look-ahead / clock mismatch)

    def as_policy(self) -> dict:
        """Canonical, serializable description of the active policy (logic + thresholds)."""
        return {
            "logic": ELIGIBILITY_LOGIC_VERSION,
            "recent_window_bars": dict(sorted(self.recent_window_bars.items())),
            "max_feed_lag_seconds": self.max_feed_lag_seconds,
            "max_quote_lag_seconds": self.max_quote_lag_seconds,
            "max_clock_skew_seconds": self.max_clock_skew_seconds,   # changes the verdict -> in the policy
        }

    def policy_version(self) -> str:
        """Stable fingerprint of logic + thresholds. A config change yields a new policy
        version -> a NEW evaluation row; a prior verdict is never silently overwritten."""
        blob = json.dumps(self.as_policy(), sort_keys=True, separators=(",", ":"))
        return f"{ELIGIBILITY_LOGIC_VERSION}+{hashlib.sha1(blob.encode()).hexdigest()[:10]}"


class EligibilityResult(BaseModel):
    """One contextual verdict for one snapshot, under one mode + policy. Auditable."""
    eligible: bool
    reasons: list[str]
    mode: Mode
    as_of: datetime                            # the bar (bar_close) this verdict is about
    policy_version: str
    policy: dict
    evaluated_at: datetime
    ref_now: datetime | None = None            # the wall clock used (online only)
    feed_lag_seconds: float | None = None
    quote_lag_seconds: float | None = None
    quote_present: bool | None = None          # online only


def evaluate_eligibility(
    tf_candles: dict[str, list[Candle]],
    trigger_tf: str,
    as_of: datetime,
    *,
    mode: Mode,
    now: datetime,
    config: EligibilityConfig,
    calendar: XauUsdCalendar = DEFAULT_CALENDAR,
    quote_time: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> EligibilityResult:
    reasons: list[str] = []
    feed_lag: float | None = None
    quote_lag: float | None = None
    quote_present: bool | None = None
    stamp = evaluated_at or now
    pol_version, pol = config.policy_version(), config.as_policy()

    m15 = tf_candles.get(trigger_tf) or []
    if not m15:
        return EligibilityResult(
            eligible=False, reasons=["no_trigger_bars"], mode=mode, as_of=as_of,
            policy_version=pol_version, policy=pol, evaluated_at=stamp,
            ref_now=now if mode == "online" else None,
        )

    # 1. Feed freshness. Online -> vs wall clock; replay -> vs as_of (fresh by construction).
    ref = now if mode == "online" else as_of
    feed_lag = (ref - m15[-1].close_time).total_seconds()
    if mode == "online" and feed_lag > config.max_feed_lag_seconds:
        reasons.append(f"stale_feed:m15_lag={int(feed_lag)}s>{config.max_feed_lag_seconds}s")

    # 2. Online XTB quote: must be PRESENT and fresh. Absence fails closed (no live basis).
    if mode == "online":
        quote_present = quote_time is not None
        if not quote_present:
            reasons.append("missing_xtb_quote")
        else:
            quote_lag = (now - quote_time).total_seconds()
            if quote_lag < -config.max_clock_skew_seconds:
                # quote timestamped in the FUTURE relative to now -> look-ahead / clock issue
                reasons.append(f"future_quote:lag={int(quote_lag)}s")
            elif quote_lag > config.max_quote_lag_seconds:
                reasons.append(f"stale_quote:lag={int(quote_lag)}s>{config.max_quote_lag_seconds}s")

    # 3. Unexpected gaps in the RECENT window only (old gaps stay in audit, not here).
    for tf, candles in tf_candles.items():
        n = config.recent_window_bars.get(tf)
        recent = candles[-n:] if n else candles
        for g in validate_series(recent, tf):
            if classify_gap(g, tf, calendar) == UNEXPECTED_MISSING_BAR:
                reasons.append(f"recent_unexpected_gap:{tf}:{g.after.isoformat()}")

    return EligibilityResult(
        eligible=(not reasons), reasons=reasons, mode=mode, as_of=as_of,
        policy_version=pol_version, policy=pol, evaluated_at=stamp,
        ref_now=now if mode == "online" else None,
        feed_lag_seconds=round(feed_lag, 1) if feed_lag is not None else None,
        quote_lag_seconds=round(quote_lag, 1) if quote_lag is not None else None,
        quote_present=quote_present,
    )
