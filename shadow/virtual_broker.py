"""Shadow virtual broker: open a VIRTUAL position from a risk-approved decision.

Measurement only — NO real order is placed, and account balance/equity is NEVER used as PnL.

MODELLING HONESTY (this is an approximation, not a bid/ask microstructure simulation):
- Bars are treated as MID prices. SL/TP are absolute MID levels from the entry mid and the
  deterministic sl_pct / tp_pct the Risk Engine produced.
- The bid/ask spread is charged as ONE flat round-trip cost deducted from the R-multiple
  (see reconciler `_r_net`). Touch thresholds are NOT bid/ask-adjusted; a gap through the
  stop fills IDEALLY at the stop level; there is no latency or slippage yet. Those are
  refinements tracked for later in the Faza 3 plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from core.models import Direction

SpreadProvenance = Literal["observed_xtb", "modeled", "historical"]


class ShadowConfig(BaseModel):
    # Close the trade if neither SL nor TP is hit within this many bars (of whatever
    # timeframe is fed to the reconciler). ~1 trading day of M15 bars by default.
    timeout_bars: int = 96
    # Round-trip commission as % of notional. XTB gold CFD is typically commission-free;
    # default 0 (set from the real account terms — do not invent a rate).
    commission_pct: float = 0.0
    # Overnight financing (swap) as % of notional per rollover held. Default 0 until the real
    # swapLong/swapShort is read from the account; applied per rollover crossed.
    swap_pct_per_night: float = 0.0
    rollover_hour_utc: int = 22   # XTB daily rollover (22:00 UTC in summer)
    # Online entries land MID-BAR (opened_at inside an M15 bar). We only have that bar's full
    # OHLC, which mixes pre- and post-entry movement. Conservative policy: on the partial entry
    # bar a STOP touch counts (pessimistic — the adverse move may be post-entry) but a TP touch
    # does NOT (we can't confirm it happened after entry). Proper fix later = M1/tick reconcile.
    conservative_partial_entry: bool = True


class VirtualTrade(BaseModel):
    direction: Direction        # BUY or SELL only (NO_TRADE never reaches here)
    entry_mid: float            # ACTUAL entry fill (reference price + adverse slippage)
    sl_price: float             # absolute stop level
    tp_price: float             # absolute target level
    spread_pct: float           # round-trip spread cost basis
    spread_provenance: SpreadProvenance
    slippage_pct: float = 0.0   # adverse slippage baked into entry; also applied on exit
    opened_at: datetime

    @property
    def risk_per_unit(self) -> float:
        """Price distance to the stop — the '1R' unit."""
        return abs(self.entry_mid - self.sl_price)


def cost_manifest(trade: "VirtualTrade", config: ShadowConfig) -> dict:
    """Honest cost manifest for persistence: a cost component is 'modeled' ONLY when its rate
    is actually non-zero. Commission/swap default to 0 (real XTB terms not wired), so they
    land in `not_modeled` — the R-multiple is NOT net of real financing, and the manifest must
    say so instead of claiming 'modeled' with a zero rate."""
    # A component is 'modeled' when its rate is non-zero (the reconciler APPLIES it) — including
    # a NEGATIVE swap (a credit). Only an exactly-zero rate is not_modeled.
    modeled = ["spread", "gap_through_stop", "latency"]
    not_modeled: list[str] = []
    (modeled if trade.slippage_pct != 0 else not_modeled).append("slippage")
    (modeled if config.commission_pct != 0 else not_modeled).append("commission")
    (modeled if config.swap_pct_per_night != 0 else not_modeled).append("swap")
    manifest = {
        "spread_pct": trade.spread_pct, "spread_provenance": trade.spread_provenance,
        "slippage_pct": trade.slippage_pct,
        "commission_pct": config.commission_pct, "swap_pct_per_night": config.swap_pct_per_night,
        "modeled": modeled, "not_modeled": not_modeled,
    }
    if "swap" in not_modeled or "commission" in not_modeled:
        manifest["note"] = ("commission/swap rate 0 -> NOT net of real financing; also single "
                            "swap rate (no long/short split), fixed 22:00 UTC rollover, no DST/"
                            "triple-swap. Wire real XTB terms before trusting expectancy.")
    return manifest


def open_virtual_trade(
    direction: Direction,
    entry_ref: float,
    sl_pct: float,
    tp_pct: float,
    *,
    spread_pct: float,
    spread_provenance: SpreadProvenance,
    slippage_pct: float = 0.0,
    opened_at: datetime,
) -> VirtualTrade:
    """`entry_ref` is the reference fill price (online: the observed quote; replay: the next
    bar's open). Adverse slippage is applied to it, and SL/TP are derived from that fill."""
    if direction not in (Direction.BUY, Direction.SELL):
        raise ValueError("a virtual trade requires BUY or SELL, not NO_TRADE")
    if entry_ref <= 0 or sl_pct <= 0 or tp_pct <= 0:
        raise ValueError("entry_ref, sl_pct and tp_pct must be positive")
    s = slippage_pct / 100.0
    if direction == Direction.BUY:
        entry_fill = entry_ref * (1 + s)          # buying slips UP (adverse)
        sl = entry_fill * (1 - sl_pct / 100)
        tp = entry_fill * (1 + tp_pct / 100)
    else:  # SELL
        entry_fill = entry_ref * (1 - s)          # selling slips DOWN (adverse)
        sl = entry_fill * (1 + sl_pct / 100)
        tp = entry_fill * (1 - tp_pct / 100)
    return VirtualTrade(
        direction=direction, entry_mid=round(entry_fill, 4), sl_price=round(sl, 4),
        tp_price=round(tp, 4), spread_pct=spread_pct, spread_provenance=spread_provenance,
        slippage_pct=slippage_pct, opened_at=opened_at,
    )
