"""Rigid, deterministic Risk Engine — the gate AFTER the LLM decision.

- SL/TP are computed HERE, in Python, from ATR — never by the LLM.
- `confidence` is an ORDINAL threshold, never a probability or a size.
- Volume is fixed at execution (TRADING_VOLUME in trading_hands); the engine never sizes.
- Fail-closed: any violation REJECTS. Out-of-bounds values are REJECTED, never clamped.
- SPREAD IS MANDATORY for a BUY/SELL: no spread -> reject (`missing_spread`). A replay bar
  carries no live quote, so it is rejected until historical/modeled spread is wired — we
  NEVER borrow the current XTB quote for a historical bar (look-ahead).
- `sl_pct`/`tp_pct` are positive magnitudes; the direction sign is applied only in the
  execution payload (trading_hands take_profit sign encodes Buy/Sell).

Execution-readiness: a passing verdict is `approved` = SHADOW-ELIGIBLE, NOT execution-ready.
Cooldown/frequency and existing-position checks are STATEFUL and not wired yet, so
`execution_ready` is always False and lists the pending gates. Do not route a verdict to a
live order on `approved` alone.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from core.models import Direction
from data_collector.session import DEFAULT_CALENDAR, XauUsdCalendar
from decision.schema import DecisionOutput

RISK_CONFIG_VERSION = "risk-mvp-2026.4"   # 2026.4: model-proposed SL/TP bounded to an ATR multiple

# Gates required for a LIVE order that are STATEFUL and not yet implemented. Until these are
# wired, no verdict is execution-ready (only shadow-eligible).
PENDING_EXECUTION_GATES = ["cooldown_frequency", "existing_positions"]


class RiskConfig(BaseModel):
    # Lowered 0.60 -> 0.55 on measurement (2026-07-21, run claude-sonnet5-probe): across 109 paid
    # Sonnet-5 decisions the risk engine rejected 50 of 56 directional calls, and EVERY rejection
    # was low_confidence. Claude's conviction clusters just under the old threshold (0.42, 0.45,
    # 0.52, 0.55, 0.58), so 0.60 was not filtering weak signals so much as filtering nearly all of
    # them: 6 trades from 56 signals. At 0.55 the same sample yields ~42 — the difference between a
    # strategy that can be measured and one that almost never acts.
    # This is a RISK threshold, not a tuning knob: raise it again if the extra trades prove to be
    # noise. `confidence` is ORDINAL, so these numbers are not probabilities and only compare
    # within one model — re-measure before reusing this value on a different model.
    min_confidence: float = Field(0.55, ge=0.0, le=1.0)   # ORDINAL threshold
    sl_atr_mult: float = Field(1.5, gt=0)                 # SL = mult * ATR%
    reward_risk: float = Field(2.0, gt=0)                 # TP = reward_risk * SL
    # "atr" = size the stop deterministically (the model never sees the knob). "model" = let the
    # model propose from market structure and validate it here. The default stays "atr" so this
    # is an opt-in experiment rather than a silent change of what every past run measured.
    sl_tp_source: Literal["atr", "model"] = "atr"
    # A proposal may deviate from the deterministic size, but only so far. Without this the only
    # ceiling was max_sl_pct (3.0%) against a typical ATR stop of ~0.28%, so a model could
    # propose a stop 10x wider and pass every check — and WIDENING IS REWARDED, because the
    # spread-vs-stop gate is relative to the stop, so a wider stop unlocks bars a normal one
    # cannot trade. Worse, R hides it: at fixed volume a 10x stop risks 10x the money for the
    # same R, so the track record would look unchanged while real drawdown scaled with it.
    max_sl_atr_multiple: float = Field(2.5, gt=0)
    min_sl_pct: float = Field(0.05, gt=0)                 # reject stops tighter than this
    max_sl_pct: float = Field(3.0, gt=0)                  # reject stops wider than this
    max_spread_fraction_of_sl: float = Field(0.33, gt=0)  # spread must be < this * SL
    require_session_open: bool = True                     # reject if the market is closed
    version: str = RISK_CONFIG_VERSION


class RiskVerdict(BaseModel):
    approved: bool                       # risk checks passed -> SHADOW-ELIGIBLE
    reason: str | None
    direction: Direction
    confidence: float
    sl_pct: float | None = None
    tp_pct: float | None = None
    spread_pct: float | None = None
    spread_provenance: str = "unavailable"
    session_open: bool | None = None
    # Where SL/TP came from, so a track record can separate model-sized from ATR-sized trades
    # instead of averaging two different policies together.
    sl_tp_source: str = "atr"
    execution_ready: bool = False        # always False until PENDING_EXECUTION_GATES are wired
    pending_execution_gates: list[str] = Field(default_factory=lambda: list(PENDING_EXECUTION_GATES))
    risk_config_version: str


def compute_sl_tp(atr_pct_m15: float, config: RiskConfig) -> tuple[float, float]:
    """Deterministic SL/TP magnitudes from ATR. Pure function; no LLM input."""
    sl = round(atr_pct_m15 * config.sl_atr_mult, 3)
    tp = round(sl * config.reward_risk, 3)
    return sl, tp


def resolve_sl_tp(decision, atr_pct_m15: float, config: RiskConfig) -> tuple[float, float, str]:
    """Pick the SL/TP this trade will use, and say WHERE it came from.

    With `sl_tp_source="model"` the model's proposal is used when it is present and passes the
    reward:risk floor; otherwise we fall back to ATR sizing rather than trading a shape the risk
    rules do not accept. The bounds themselves (min/max SL, spread-vs-stop) are enforced by the
    caller for BOTH sources — a proposal gets no easier a path than a computed value.

    Returns (sl_pct, tp_pct, source) where source is one of:
      "atr"                      — deterministic sizing
      "model"                    — the model's proposal, accepted
      "atr_fallback:no_proposal" — policy is model, but none was offered
      "atr_fallback:rr_too_low"  — proposal offered, reward:risk below the floor
      "atr_fallback:sl_too_wide" — proposal exceeds max_sl_atr_multiple x the ATR stop
    """
    atr_sl, atr_tp = compute_sl_tp(atr_pct_m15, config)
    if config.sl_tp_source != "model":
        return atr_sl, atr_tp, "atr"

    sl = getattr(decision, "proposed_sl_pct", None)
    tp = getattr(decision, "proposed_tp_pct", None)
    if sl is None or tp is None:
        return atr_sl, atr_tp, "atr_fallback:no_proposal"

    # The one shape check that cannot be left to the bounds below: a wide stop with a near target
    # is exactly how a self-sized stop flatters a win rate, so the reward:risk floor is applied to
    # the PROPOSAL, not merely to the ATR-derived pair.
    if tp < sl * config.reward_risk:
        return atr_sl, atr_tp, "atr_fallback:rr_too_low"
    # Bound the deviation from the deterministic size. The reward:risk floor constrains the
    # SHAPE but not the SCALE — a proportionally-scaled stop and target satisfy it at any width.
    if sl > atr_sl * config.max_sl_atr_multiple:
        return atr_sl, atr_tp, "atr_fallback:sl_too_wide"
    return round(float(sl), 3), round(float(tp), 3), "model"


def evaluate_risk(
    decision: DecisionOutput,
    packet,
    config: RiskConfig,
    *,
    spread_provenance: str = "unavailable",
    calendar: XauUsdCalendar = DEFAULT_CALENDAR,
) -> RiskVerdict:
    d, conf = decision.direction, decision.confidence
    session_open = calendar.is_open(packet.bar_close)

    def reject(reason: str, **extra) -> RiskVerdict:
        return RiskVerdict(approved=False, reason=reason, direction=d, confidence=conf,
                           spread_pct=packet.spread_pct, spread_provenance=spread_provenance,
                           session_open=session_open, risk_config_version=config.version, **extra)

    # NO_TRADE is a valid decision with no order.
    if d == Direction.NO_TRADE:
        return reject("no_trade")

    # Ordinal confidence gate.
    if conf < config.min_confidence:
        return reject(f"low_confidence:{conf}<{config.min_confidence}")

    # Spread is MANDATORY for an entry. Missing spread -> fail closed (this rejects a replay
    # bar until historical/modeled spread exists; we never substitute the current quote).
    if packet.spread_pct is None:
        return reject(f"missing_spread:provenance={spread_provenance}")

    # Session must be open (BUY/SELL).
    if config.require_session_open and not session_open:
        return reject("market_closed")

    # Need a positive ATR to size SL/TP deterministically.
    atr = packet.atr_pct_m15
    if atr is None or atr <= 0:
        return reject("no_atr")

    sl_pct, tp_pct, sl_tp_source = resolve_sl_tp(decision, atr, config)

    # Bounds: reject (never clamp) an SL outside the allowed band.
    if not (config.min_sl_pct <= sl_pct <= config.max_sl_pct):
        return reject(f"sl_out_of_bounds:{sl_pct}∉[{config.min_sl_pct},{config.max_sl_pct}]")

    # Spread must not eat the stop.
    if packet.spread_pct > sl_pct * config.max_spread_fraction_of_sl:
        return reject(f"spread_vs_sl:{packet.spread_pct}>{sl_pct}*{config.max_spread_fraction_of_sl}")

    # Passed risk -> shadow-eligible. NOT execution-ready (stateful gates still pending).
    return RiskVerdict(
        approved=True, reason=None, direction=d, confidence=conf, sl_pct=sl_pct, tp_pct=tp_pct,
        spread_pct=packet.spread_pct, spread_provenance=spread_provenance, session_open=session_open,
        execution_ready=False, risk_config_version=config.version, sl_tp_source=sl_tp_source,
    )
