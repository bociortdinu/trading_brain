"""Deterministic pre-LLM gate.

The prefilter decides whether it is worth calling the (paid, latency-bearing) LLM at all.
It is the single fail-closed gate BEFORE the decision: it composes
- eligibility (is the observation trustworthy right now? — features.eligibility), and
- strategy-level skip rules (data quality, spread, regime),
and returns pass/no-pass with reasons. An ineligible or low-value bar never reaches the LLM.

This is NOT the Risk Engine: it never looks at the LLM output. It only gates the input.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

PREFILTER_CONFIG_VERSION = "prefilter-mvp-2026.2"   # 2026.2: scheduled-release blackout


class PrefilterConfig(BaseModel):
    require_eligible: bool = True
    max_spread_pct: float = 0.10          # skip if the XTB spread is wider than this
    blocked_regimes: list[str] = Field(default_factory=lambda: ["choppy"])
    block_insufficient_data: bool = True  # any timeframe with too few bars -> skip
    version: str = PREFILTER_CONFIG_VERSION


class PrefilterResult(BaseModel):
    passed: bool
    reasons: list[str]
    config_version: str


def prefilter(packet, eligibility, config: PrefilterConfig, blackout=None) -> PrefilterResult:
    """`blackout` is the scheduled macro release whose window contains this bar (or None).
    It is passed in already resolved — the prefilter stays pure and does no IO."""
    reasons: list[str] = []

    # 1. Eligibility is a hard precondition (fail-closed): don't spend an LLM call on an
    #    untrustworthy bar. The specific eligibility reasons are carried through.
    if config.require_eligible and not eligibility.eligible:
        reasons.append(f"ineligible:{eligibility.mode}:{';'.join(eligibility.reasons) or 'unknown'}")

    # 2. Data quality: an insufficient timeframe means the features are not comparable.
    if config.block_insufficient_data:
        for tf, dq in (packet.data_quality or {}).items():
            if dq.get("verdict") == "insufficient":
                reasons.append(f"insufficient_data:{tf}")

    # 3. Spread: a blown-out spread makes the setup uneconomic before we even ask.
    if packet.spread_pct is not None and packet.spread_pct > config.max_spread_pct:
        reasons.append(f"spread_too_wide:{packet.spread_pct}>{config.max_spread_pct}")

    # 4. Regime: skip regimes the strategy does not trade (e.g. choppy).
    if packet.regime in config.blocked_regimes:
        reasons.append(f"blocked_regime:{packet.regime}")

    # 5. Scheduled macro release: gold reprices violently across CPI/NFP/FOMC, and an M15 stop
    #    (~0.28% of price) carries no meaning through such a print — the gap can jump straight
    #    over it. Skipping the window is BOTH the loss control and a cost control: the bar never
    #    reaches the paid model. The event is named so the skip is auditable.
    if blackout is not None:
        reasons.append(f"news_blackout:{blackout.label}@{blackout.scheduled_at.isoformat()}")

    return PrefilterResult(passed=not reasons, reasons=reasons, config_version=config.version)
