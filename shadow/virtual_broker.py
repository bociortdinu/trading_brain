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
    # Overnight financing (swap) as % of notional per rollover held. This is the LEGACY single
    # rate used for BOTH directions when the long/short split below is unset. Default 0 until the
    # real terms are read; applied per rollover crossed.
    swap_pct_per_night: float = 0.0
    # Real GOLD financing terms (from xStation5 -> GOLD -> Specification). All default to unset,
    # so behaviour is IDENTICAL to the single-rate model until they are wired — no invented rates.
    swap_long_pct_per_night: float | None = None   # BUY held overnight; None -> swap_pct_per_night
    swap_short_pct_per_night: float | None = None  # SELL held overnight; None -> swap_pct_per_night
    triple_swap_weekday: int | None = None         # 0=Mon..6=Sun charged 3x (weekend value roll); None=off
    swap_currency: str | None = None               # informational: currency the swap is quoted in
    terms_version: str = "unset"                   # provenance of the financing terms above
    # The daily rollover is `rollover_hour_utc` o'clock in `rollover_tz`. With the default tz="UTC"
    # this is literally 22:00 UTC (legacy behaviour). Set a real IANA tz (e.g. the broker server
    # tz) to make the rollover DST-aware — the wall-clock hour stays fixed, the UTC instant shifts.
    rollover_hour_utc: int = 22
    rollover_tz: str = "UTC"
    # Timeframe used for INTRABAR reconciliation (SL/TP touch ordering). Default = the decision
    # timeframe ("15min"): SL and TP can then both land in one bar -> the both-hit ambiguity band.
    # Set "1min" to resolve that ordering at M1 granularity (far fewer ambiguous cases). Recorded
    # in the manifest and the fingerprint; the actual granularity used is stored at close time.
    reconcile_timeframe: str = "15min"
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


def swap_rate_for(config: ShadowConfig, direction: Direction) -> float:
    """The overnight swap rate (%/night of notional) for THIS direction: the direction-specific
    long/short rate when set, else the legacy single `swap_pct_per_night`. A long pays swapLong,
    a short pays swapShort; either can be a credit (negative)."""
    r = (config.swap_long_pct_per_night if direction == Direction.BUY
         else config.swap_short_pct_per_night)
    return r if r is not None else config.swap_pct_per_night


def cost_manifest(trade: "VirtualTrade", config: ShadowConfig) -> dict:
    """Honest cost manifest for persistence: a cost component is 'modeled' ONLY when its rate
    is actually non-zero. Commission/swap default to 0 (real XTB terms not wired), so they
    land in `not_modeled` — the R-multiple is NOT net of real financing, and the manifest must
    say so instead of claiming 'modeled' with a zero rate."""
    # A component is 'modeled' when its rate is non-zero (the reconciler APPLIES it) — including
    # a NEGATIVE swap (a credit). Only an exactly-zero rate is not_modeled. Swap is judged by the
    # rate that actually applies to THIS trade's direction (long vs short).
    eff_swap = swap_rate_for(config, trade.direction)
    modeled = ["spread", "gap_through_stop", "latency"]
    not_modeled: list[str] = []
    (modeled if trade.slippage_pct != 0 else not_modeled).append("slippage")
    (modeled if config.commission_pct != 0 else not_modeled).append("commission")
    (modeled if eff_swap != 0 else not_modeled).append("swap")
    manifest = {
        "spread_pct": trade.spread_pct, "spread_provenance": trade.spread_provenance,
        "slippage_pct": trade.slippage_pct,
        "commission_pct": config.commission_pct, "swap_pct_per_night": config.swap_pct_per_night,
        # Persist the rate-bearing config too, so a trade opened NOW is reconciled on a LATER tick
        # with the SAME terms even if the live config changed in between (rates were not
        # reconstructed before -> a config change silently re-priced an open position's R).
        "swap_long_pct_per_night": config.swap_long_pct_per_night,
        "swap_short_pct_per_night": config.swap_short_pct_per_night,
        "swap_effective_pct_per_night": eff_swap,   # the rate this direction actually pays
        "triple_swap_weekday": config.triple_swap_weekday,
        "swap_currency": config.swap_currency,
        "terms_version": config.terms_version,
        "rollover_hour_utc": config.rollover_hour_utc,
        "rollover_tz": config.rollover_tz,
        "conservative_partial_entry": config.conservative_partial_entry,
        "reconcile_timeframe": config.reconcile_timeframe,
        "modeled": modeled, "not_modeled": not_modeled,
    }
    # Honest caveats: flag whichever real-terms features are still missing.
    caveats: list[str] = []
    if eff_swap == 0 or config.commission_pct == 0:
        caveats.append("commission/swap rate 0 -> NOT net of real financing")
    if config.terms_version == "unset":
        caveats.append("financing terms_version unset (rates not sourced from the account spec)")
    if config.triple_swap_weekday is None:
        caveats.append("no triple-swap day modelled")
    if config.rollover_tz == "UTC":
        caveats.append("rollover fixed at 22:00 UTC (no DST)")
    if caveats:
        manifest["note"] = "; ".join(caveats) + ". Wire real XTB terms before trusting expectancy."
    return manifest


def execution_manifest(*, modeled_spread_pct: float, slippage_pct: float, config: ShadowConfig,
                       single_position: bool, cooldown_bars: int,
                       risk_config_version: str, prefilter_version: str) -> dict:
    """EVERY parameter that can change a shadow trade's outcome — so it can rebuild the exact same
    trade from a persisted decision (crash recovery) and so a config change is a different decision.

    Includes the position policy (single_position, cooldown_bars) and the risk/prefilter config
    versions: `timeout_bars=96` vs `5` MUST hash differently (an earlier version omitted it and
    they collided). Anything the reconciler or the gate reads goes here."""
    return {
        "modeled_spread_pct": modeled_spread_pct,
        "slippage_pct": slippage_pct,
        "commission_pct": config.commission_pct,
        "swap_pct_per_night": config.swap_pct_per_night,
        "swap_long_pct_per_night": config.swap_long_pct_per_night,
        "swap_short_pct_per_night": config.swap_short_pct_per_night,
        "triple_swap_weekday": config.triple_swap_weekday,
        "terms_version": config.terms_version,
        "rollover_hour_utc": config.rollover_hour_utc,
        "rollover_tz": config.rollover_tz,
        "conservative_partial_entry": config.conservative_partial_entry,
        "reconcile_timeframe": config.reconcile_timeframe,
        "timeout_bars": config.timeout_bars,
        "single_position": single_position,
        "cooldown_bars": cooldown_bars,
        "risk_config_version": risk_config_version,
        "prefilter_version": prefilter_version,
    }


def execution_hash(manifest: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]


def shadow_config_from_costs(costs: dict | None, *, timeout_bars: int,
                             fallback: "ShadowConfig | None" = None) -> ShadowConfig:
    """Rebuild the ShadowConfig a trade was OPENED with, from its persisted cost manifest, so a
    later reconciliation prices it with the same terms. `timeout_bars` comes from the trade row;
    everything else from `costs`. `fallback` fills anything a legacy manifest didn't record."""
    costs = costs or {}
    fb = fallback or ShadowConfig()
    return ShadowConfig(
        timeout_bars=timeout_bars if timeout_bars is not None else fb.timeout_bars,
        commission_pct=costs.get("commission_pct", fb.commission_pct),
        swap_pct_per_night=costs.get("swap_pct_per_night", fb.swap_pct_per_night),
        # Real-terms fields: a legacy manifest lacks these -> .get returns the fallback (None/UTC),
        # so an old single-rate trade reconciles EXACTLY as before; a new trade rebuilds its terms.
        swap_long_pct_per_night=costs.get("swap_long_pct_per_night", fb.swap_long_pct_per_night),
        swap_short_pct_per_night=costs.get("swap_short_pct_per_night", fb.swap_short_pct_per_night),
        triple_swap_weekday=costs.get("triple_swap_weekday", fb.triple_swap_weekday),
        swap_currency=costs.get("swap_currency", fb.swap_currency),
        terms_version=costs.get("terms_version", fb.terms_version),
        rollover_hour_utc=costs.get("rollover_hour_utc", fb.rollover_hour_utc),
        rollover_tz=costs.get("rollover_tz", fb.rollover_tz),
        reconcile_timeframe=costs.get("reconcile_timeframe", fb.reconcile_timeframe),
        conservative_partial_entry=costs.get("conservative_partial_entry",
                                             fb.conservative_partial_entry),
    )


def shadow_config_from_settings(settings, **overrides) -> ShadowConfig:
    """Build a ShadowConfig from typed settings, so the runner / online / evaluation all wire the
    SAME real financing terms from one place (no per-call-site field list to drift). `overrides`
    (e.g. timeout_bars) win over settings."""
    base = dict(
        commission_pct=settings.commission_pct,
        swap_pct_per_night=settings.swap_pct_per_night,
        swap_long_pct_per_night=getattr(settings, "swap_long_pct_per_night", None),
        swap_short_pct_per_night=getattr(settings, "swap_short_pct_per_night", None),
        triple_swap_weekday=getattr(settings, "triple_swap_weekday", None),
        swap_currency=getattr(settings, "swap_currency", None),
        terms_version=getattr(settings, "financing_terms_version", "unset"),
        rollover_hour_utc=getattr(settings, "rollover_hour_utc", 22),
        rollover_tz=getattr(settings, "rollover_tz", "UTC"),
        reconcile_timeframe=getattr(settings, "reconcile_timeframe", "15min"),
    )
    base.update(overrides)
    return ShadowConfig(**base)


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
