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

from pydantic import BaseModel, Field

from core.models import Direction
from data_collector.session import DEFAULT_CALENDAR, XauUsdCalendar
from decision.schema import DecisionOutput

RISK_CONFIG_VERSION = "risk-mvp-2026.2"   # 2026.2: mandatory spread, session gate, exec-readiness

# Gates required for a LIVE order that are STATEFUL and not yet implemented. Until these are
# wired, no verdict is execution-ready (only shadow-eligible).
PENDING_EXECUTION_GATES = ["cooldown_frequency", "existing_positions"]


class RiskConfig(BaseModel):
    min_confidence: float = Field(0.60, ge=0.0, le=1.0)   # ORDINAL threshold
    sl_atr_mult: float = Field(1.5, gt=0)                 # SL = mult * ATR%
    reward_risk: float = Field(2.0, gt=0)                 # TP = reward_risk * SL
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
    execution_ready: bool = False        # always False until PENDING_EXECUTION_GATES are wired
    pending_execution_gates: list[str] = Field(default_factory=lambda: list(PENDING_EXECUTION_GATES))
    risk_config_version: str


def compute_sl_tp(atr_pct_m15: float, config: RiskConfig) -> tuple[float, float]:
    """Deterministic SL/TP magnitudes from ATR. Pure function; no LLM input."""
    sl = round(atr_pct_m15 * config.sl_atr_mult, 3)
    tp = round(sl * config.reward_risk, 3)
    return sl, tp


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

    sl_pct, tp_pct = compute_sl_tp(atr, config)

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
        execution_ready=False, risk_config_version=config.version,
    )
