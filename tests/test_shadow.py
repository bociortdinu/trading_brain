"""Shadow Mode engine: virtual trade + intrabar reconciliation with pessimistic/optimistic
bands for the both-hit ambiguity, R-multiple net of a round-trip spread cost."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.models import Direction
from data_collector.providers.base import Candle
from shadow.reconciler import reconcile
from shadow.virtual_broker import ShadowConfig, open_virtual_trade

UTC = timezone.utc
_T0 = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)


def _bar(o, h, l, c, n=0):
    ot = _T0 + timedelta(minutes=15 * n)
    return Candle(open_time=ot, close_time=ot + timedelta(minutes=15), open=o, high=h, low=l, close=c, volume=1.0)


def _long(spread=0.03):
    # entry 4000, sl_pct 0.3 -> sl 3988, tp_pct 0.6 -> tp 4024, risk 12
    return open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6,
                              spread_pct=spread, spread_provenance="modeled", opened_at=_T0)


def _short(spread=0.03):
    # entry 4000 -> sl 4012, tp 3976, risk 12
    return open_virtual_trade(Direction.SELL, 4000.0, 0.3, 0.6,
                              spread_pct=spread, spread_provenance="modeled", opened_at=_T0)


def _long_midbar():
    # Entry lands 5 min INTO bar n=0 (open_time _T0, close _T0+15): that bar is PARTIAL.
    return open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                              spread_provenance="modeled", opened_at=_T0 + timedelta(minutes=5))


def test_partial_entry_bar_does_not_credit_a_tp_touch():
    """Conservative policy: a TP touch on the partial entry bar is NOT credited (its OHLC mixes
    pre-/post-entry movement). The trade holds and only closes on a fully-post-entry TP bar."""
    trade = _long_midbar()   # tp 4024
    partial_tp = _bar(4000, 4030, 3995, 4010, n=0)   # touches tp 4024 but is the partial bar
    assert reconcile(trade, [partial_tp]).status == "open"      # not credited -> still open
    later_tp = _bar(4010, 4030, 4005, 4025, n=1)                # fully-post-entry TP -> credited
    out = reconcile(trade, [partial_tp, later_tp])
    assert out.status == "closed" and out.exit_reason == "tp_hit"


def test_partial_entry_bar_still_honors_a_stop_touch():
    """A STOP touch on the partial entry bar DOES close (pessimistic): the adverse move might be
    post-entry, so we must not treat the position as immune until the next bar boundary."""
    trade = _long_midbar()   # sl 3988
    partial_sl = _bar(4000, 4005, 3980, 3990, n=0)   # touches sl 3988 on the partial bar
    out = reconcile(trade, [partial_sl])
    assert out.status == "closed" and out.exit_reason == "sl_hit"


def test_partial_entry_bar_never_fills_at_a_pre_entry_gap_price():
    """The partial bar OPENED before we entered, so its open is not a price we could ever have
    been filled at. Gap-through-stop must not be modelled from it: a bar that opened at 3900
    (far below the 3988 stop) would otherwise 'fill' us at 3900 — a loss taken before the trade
    existed. The justifiable worst case is the stop level itself."""
    trade = _long_midbar()   # entry 4000, sl 3988
    # Opens 88 points BELOW the stop, but that open predates the entry at _T0+5min.
    pre_entry_gap = _bar(3900, 4005, 3890, 3990, n=0)
    out = reconcile(trade, [pre_entry_gap])
    assert out.status == "closed" and out.exit_reason == "sl_hit"
    assert out.exit_price == 3988.0, "must fill at the stop, not at the pre-entry open (3900)"
    assert out.r_multiple == -1.0     # exactly -1R, not the impossible ~-8R the gap would imply


def test_open_sets_levels():
    t = _long()
    assert t.sl_price == 3988.0 and t.tp_price == 4024.0 and t.risk_per_unit == 12.0
    s = _short()
    assert s.sl_price == 4012.0 and s.tp_price == 3976.0


def test_no_trade_direction_rejected():
    with pytest.raises(ValueError):
        open_virtual_trade(Direction.NO_TRADE, 4000.0, 0.3, 0.6,
                           spread_pct=0.03, spread_provenance="modeled", opened_at=_T0)


def test_long_tp_hit_net_of_spread():
    o = reconcile(_long(), [_bar(4000, 4025, 3999, 4020)])
    assert o.status == "closed" and o.exit_reason == "tp_hit" and o.exit_price == 4024.0
    assert o.r_multiple == pytest.approx(1.9)  # gross 2.0R minus 0.1R spread
    assert not o.ambiguous


def test_long_sl_hit_worse_than_minus_one():
    o = reconcile(_long(), [_bar(4000, 4001, 3985, 3990)])
    assert o.exit_reason == "sl_hit" and o.r_multiple == pytest.approx(-1.1)  # -1R minus spread


def test_both_hit_is_ambiguous_with_bands():
    o = reconcile(_long(), [_bar(4000, 4030, 3980, 4000)])
    assert o.ambiguous and o.exit_reason == "ambiguous"
    assert o.r_pessimistic == pytest.approx(-1.1) and o.r_optimistic == pytest.approx(1.9)
    assert o.r_multiple == o.r_pessimistic  # primary is conservative


def test_short_tp_and_sl():
    tp = reconcile(_short(), [_bar(4000, 4001, 3975, 3977)])   # low 3975 <= tp 3976
    assert tp.exit_reason == "tp_hit" and tp.r_multiple == pytest.approx(1.9)
    sl = reconcile(_short(), [_bar(4000, 4013, 3999, 4010)])   # high 4013 >= sl 4012
    assert sl.exit_reason == "sl_hit" and sl.r_multiple == pytest.approx(-1.1)


def test_first_touch_wins_across_bars():
    bars = [_bar(4000, 4010, 3995, 4005, 0),   # no touch
            _bar(4005, 4026, 4000, 4020, 1)]    # TP touched on bar 2
    o = reconcile(_long(), bars)
    assert o.exit_reason == "tp_hit" and o.closed_at == bars[1].close_time


def test_timeout_when_never_touched():
    quiet = [_bar(4000, 4010, 3995, 4005, n) for n in range(5)]
    o = reconcile(_long(), quiet, ShadowConfig(timeout_bars=3))
    assert o.status == "expired" and o.exit_reason == "timeout"
    assert o.closed_at == quiet[2].close_time  # exits at the 3rd bar


def test_no_lookahead_pre_entry_bar_is_ignored():
    # The audit's case: a trade opened at the 20:15 close must NOT be resolved by the
    # 20:00-20:15 bar (which formed BEFORE entry). That bar is dropped.
    trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.03,
                               spread_provenance="modeled", opened_at=_T0 + timedelta(minutes=15))
    pre_entry = _bar(4000, 4001, 3985, 3990, n=0)   # opens 20:00, before entry; would be sl_hit
    o = reconcile(trade, [pre_entry])
    assert o.status == "open"  # pre-entry bar ignored -> nothing to resolve


def test_reconcile_rejects_out_of_order_bars():
    late = _bar(4000, 4010, 3995, 4005, n=2)
    early = _bar(4000, 4010, 3995, 4005, n=1)
    with pytest.raises(ValueError, match="strictly increasing"):
        reconcile(_long(), [late, early])  # both post-entry but disordered


def test_still_open_when_bars_run_out_before_timeout():
    quiet = [_bar(4000, 4010, 3995, 4005, n) for n in range(2)]
    o = reconcile(_long(), quiet, ShadowConfig(timeout_bars=10))
    assert o.status == "open" and o.r_multiple is None


def test_zero_spread_gives_clean_r():
    o = reconcile(_long(spread=0.0), [_bar(4000, 4025, 3999, 4020)])
    assert o.r_multiple == pytest.approx(2.0)  # exactly 2R with no spread cost


# --------------------------------------------------------------------------- #
# slippage + gap-through-stop realism
# --------------------------------------------------------------------------- #
def _long_slip(slip):
    return open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                              spread_provenance="modeled", slippage_pct=slip, opened_at=_T0)


def test_entry_slippage_makes_fill_adverse():
    t = _long_slip(0.02)                       # BUY -> entry slips UP
    assert t.entry_mid == pytest.approx(4000.8)  # 4000 * (1 + 0.0002)


def test_slippage_reduces_r_vs_zero():
    o0 = reconcile(_long_slip(0.0), [_bar(4000, 4030, 3999, 4020)])
    o1 = reconcile(_long_slip(0.02), [_bar(4000, 4030, 3999, 4020)])
    assert o0.r_multiple == pytest.approx(2.0)
    assert o1.r_multiple < o0.r_multiple       # entry + exit slippage both bite


def test_gap_through_stop_fills_worse_than_the_stop():
    t = _long_slip(0.0)                          # sl ~3988
    gap = _bar(3980, 3985, 3975, 3982)          # opens 3980, already below the stop -> gap-through
    o = reconcile(t, [gap])
    assert o.exit_reason == "sl_hit"
    assert o.exit_price < t.sl_price            # filled at the gap open, worse than the stop
    assert o.r_multiple < -1.0                  # worse than a clean -1R


# --------------------------------------------------------------------------- #
# commission + overnight swap
# --------------------------------------------------------------------------- #
def test_rollovers_counts_weekday_nights_and_skips_weekends():
    from shadow.reconciler import _rollovers
    mon = datetime(2026, 7, 13, 20, tzinfo=UTC)                            # Monday
    assert _rollovers(mon, datetime(2026, 7, 13, 21, tzinfo=UTC), 22) == 0   # before 22:00
    assert _rollovers(mon, datetime(2026, 7, 13, 23, tzinfo=UTC), 22) == 1   # crossed Mon 22:00
    assert _rollovers(mon, datetime(2026, 7, 15, 23, tzinfo=UTC), 22) == 3   # Mon+Tue+Wed nights
    # Held across a weekend: no swap is charged on Sat/Sun (the weekend carry is the triple day).
    fri = datetime(2026, 7, 10, 20, tzinfo=UTC)                            # Friday
    assert _rollovers(fri, datetime(2026, 7, 13, 12, tzinfo=UTC), 22) == 1   # only Fri; Sat+Sun skipped


def test_commission_reduces_r():
    tp = _bar(4000, 4025, 3999, 4020)
    o0 = reconcile(_long(spread=0.0), [tp], ShadowConfig(commission_pct=0.0))
    o1 = reconcile(_long(spread=0.0), [tp], ShadowConfig(commission_pct=0.03))
    assert o0.r_multiple == pytest.approx(2.0) and o1.r_multiple < o0.r_multiple


def test_overnight_swap_reduces_r_for_positions_held_past_rollover():
    tp = _bar(4000, 4025, 3999, 4020, n=9)  # closes 22:30, past the 22:00 rollover
    o0 = reconcile(_long(spread=0.0), [tp], ShadowConfig(swap_pct_per_night=0.0))
    o1 = reconcile(_long(spread=0.0), [tp], ShadowConfig(swap_pct_per_night=0.05))
    assert o1.r_multiple < o0.r_multiple      # one night of swap deducted


# --------------------------------------------------------------------------- #
# real GOLD financing terms: long/short split, triple-swap day, DST rollover,
# frozen-at-open, legacy fallback, honesty
# --------------------------------------------------------------------------- #
def _trade(direction, opened):
    return open_virtual_trade(direction, 4000.0, 0.3, 0.6, spread_pct=0.0,
                              spread_provenance="modeled", opened_at=opened)


def test_swap_long_short_split_charges_by_direction():
    from shadow.reconciler import _extra_cost
    opened, closed = datetime(2026, 7, 15, 21, tzinfo=UTC), datetime(2026, 7, 15, 23, tzinfo=UTC)  # 1 night
    cfg = ShadowConfig(swap_long_pct_per_night=0.01, swap_short_pct_per_night=0.03)
    assert _extra_cost(_trade(Direction.BUY, opened), cfg, closed) == pytest.approx(4000 * 0.01 / 100)
    assert _extra_cost(_trade(Direction.SELL, opened), cfg, closed) == pytest.approx(4000 * 0.03 / 100)


def test_triple_swap_weekday_counts_triple():
    from shadow.reconciler import _rollovers
    o, c = datetime(2026, 7, 15, 21, tzinfo=UTC), datetime(2026, 7, 15, 23, tzinfo=UTC)  # one boundary
    wd = datetime(2026, 7, 15, 22, tzinfo=UTC).weekday()
    assert _rollovers(o, c, 22) == 1.0
    assert _rollovers(o, c, 22, triple_weekday=wd) == 3.0          # that weekday is charged 3x
    assert _rollovers(o, c, 22, triple_weekday=(wd + 1) % 7) == 1.0  # a different weekday is not


def test_rollover_is_dst_aware_in_a_real_timezone():
    from shadow.reconciler import _rollovers
    tz = "Europe/Bucharest"  # UTC+3 in summer, UTC+2 in winter; rollover at 00:00 local
    # summer: local midnight == 21:00Z, so a [20:30Z, 21:30Z] window crosses one boundary
    assert _rollovers(datetime(2026, 7, 15, 20, 30, tzinfo=UTC),
                      datetime(2026, 7, 15, 21, 30, tzinfo=UTC), 0, tz) == 1.0
    # winter: local midnight == 22:00Z, so the SAME wall-clock window does NOT cross it
    assert _rollovers(datetime(2026, 1, 15, 20, 30, tzinfo=UTC),
                      datetime(2026, 1, 15, 21, 30, tzinfo=UTC), 0, tz) == 0.0


def test_financing_terms_are_frozen_at_open_and_survive_recovery():
    from shadow.reconciler import _extra_cost
    from shadow.virtual_broker import cost_manifest, shadow_config_from_costs
    opened, closed = datetime(2026, 7, 15, 21, tzinfo=UTC), datetime(2026, 7, 15, 23, tzinfo=UTC)  # Wed
    trade = _trade(Direction.SELL, opened)
    opened_cfg = ShadowConfig(swap_short_pct_per_night=0.03, triple_swap_weekday=2,
                              swap_currency="USD", terms_version="xtb-2026-07")
    manifest = cost_manifest(trade, opened_cfg)
    # A LATER live config (different terms) must NOT re-price this open trade.
    rebuilt = shadow_config_from_costs(manifest, timeout_bars=96,
                                       fallback=ShadowConfig(swap_short_pct_per_night=0.99))
    assert rebuilt.swap_short_pct_per_night == 0.03      # frozen, not the live fallback 0.99
    assert rebuilt.triple_swap_weekday == 2
    assert rebuilt.terms_version == "xtb-2026-07" and rebuilt.swap_currency == "USD"
    # SELL over a Wednesday boundary pays 3x swapShort; frozen terms reproduce it exactly.
    assert _extra_cost(trade, rebuilt, closed) == pytest.approx(3 * 4000 * 0.03 / 100)
    assert _extra_cost(trade, rebuilt, closed) == _extra_cost(trade, opened_cfg, closed)


def test_legacy_cost_manifest_reconciles_as_single_rate():
    from shadow.virtual_broker import shadow_config_from_costs, swap_rate_for
    legacy = {"commission_pct": 0.0, "swap_pct_per_night": 0.02, "rollover_hour_utc": 22}  # no new keys
    cfg = shadow_config_from_costs(legacy, timeout_bars=96)
    assert cfg.swap_pct_per_night == 0.02
    assert cfg.swap_long_pct_per_night is None and cfg.swap_short_pct_per_night is None
    assert cfg.triple_swap_weekday is None and cfg.rollover_tz == "UTC"
    assert swap_rate_for(cfg, Direction.BUY) == 0.02 == swap_rate_for(cfg, Direction.SELL)


def test_cost_manifest_flags_missing_real_terms_then_clears_when_wired():
    from shadow.virtual_broker import cost_manifest
    trade = _trade(Direction.BUY, datetime(2026, 7, 15, 21, tzinfo=UTC))
    bare = cost_manifest(trade, ShadowConfig())
    assert "swap" in bare["not_modeled"] and "note" in bare and "terms_version unset" in bare["note"]
    wired = cost_manifest(trade, ShadowConfig(commission_pct=0.02, swap_long_pct_per_night=0.01,
                                              triple_swap_weekday=2, rollover_tz="Europe/Bucharest",
                                              terms_version="xtb-2026-07"))
    assert "swap" in wired["modeled"] and "commission" in wired["modeled"] and "note" not in wired
    assert wired["swap_effective_pct_per_night"] == 0.01   # BUY -> the long rate


def test_execution_hash_captures_financing_terms():
    from shadow.virtual_broker import execution_hash, execution_manifest
    base = dict(modeled_spread_pct=0.02, slippage_pct=0.005, single_position=True,
                cooldown_bars=0, risk_config_version="v", prefilter_version="pf")
    hashes = {
        execution_hash(execution_manifest(config=ShadowConfig(), **base)),
        execution_hash(execution_manifest(config=ShadowConfig(swap_long_pct_per_night=0.01), **base)),
        execution_hash(execution_manifest(config=ShadowConfig(triple_swap_weekday=2), **base)),
        execution_hash(execution_manifest(config=ShadowConfig(rollover_tz="Europe/Bucharest"), **base)),
    }
    assert len(hashes) == 4      # every financing term moves the decision fingerprint


# --------------------------------------------------------------------------- #
# intrabar reconciliation granularity (M1 resolves the M15 both-hit ambiguity)
# --------------------------------------------------------------------------- #
def _c(o, h, l, c, start, minutes):
    return Candle(open_time=start, close_time=start + timedelta(minutes=minutes),
                  open=o, high=h, low=l, close=c, volume=1.0)


def test_m1_bars_resolve_an_m15_both_hit_ambiguity():
    opened = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                               spread_provenance="modeled", opened_at=opened)  # sl 3988, tp 4024
    # ONE M15 bar whose range spans BOTH the stop and the target -> ambiguous (pessimistic primary).
    m15 = [_c(4000, 4030, 3980, 4020, opened, 15)]
    o15 = reconcile(trade, m15, ShadowConfig())
    assert o15.ambiguous is True

    # The SAME window at M1: price reaches the target FIRST, only later dips to the stop. M1
    # granularity orders the touches, so the outcome is a definite TP (no ambiguity band).
    m1 = [
        _c(4000, 4025, 3999, 4024, opened, 1),                              # TP touched here first
        _c(4024, 4026, 3980, 3985, opened + timedelta(minutes=5), 1),       # SL only later (already closed)
    ]
    o1 = reconcile(trade, m1, ShadowConfig(reconcile_timeframe="1min"))
    assert o1.ambiguous is False and o1.exit_reason == "tp_hit"
    assert o1.r_multiple == pytest.approx(2.0)                              # (4024-4000)/12


def test_covers_window_fails_when_trade_is_older_than_the_bar_window():
    """P0-2: a trade older than the fetched window (bars start after entry, with open-market time
    in between) is NOT covered -> must not be reconciled (a touch could hide in the gap)."""
    from data_collector.session import calendar_for
    from shadow.reconciler import covers_window
    cal = calendar_for("csv")
    opened = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)   # Monday, market open
    now = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    late = [_c(4000, 4001, 3999, 4000, datetime(2026, 7, 6, 17, 30, tzinfo=UTC), 15),
            _c(4000, 4001, 3999, 4000, datetime(2026, 7, 6, 17, 45, tzinfo=UTC), 15)]
    assert covers_window(late, opened, now, "15min", cal) is False
    full = [_c(4000, 4001, 3999, 4000, opened + timedelta(minutes=15 * i), 15) for i in range(16)]
    assert covers_window(full, opened, now, "15min", cal) is True


def test_covers_window_treats_a_session_break_as_expected_not_a_hole():
    """P0-3: a valid series crossing the daily market break (21:00-22:00 UTC / 17:00-18:00 ET) is
    still fully covered — a closed-market gap is NOT a coverage hole."""
    from data_collector.session import calendar_for
    from shadow.reconciler import covers_window
    cal = calendar_for("csv")
    opened = datetime(2026, 7, 6, 20, 45, tzinfo=UTC)
    now = datetime(2026, 7, 6, 22, 15, tzinfo=UTC)
    bars = [_c(4000, 4001, 3999, 4000, datetime(2026, 7, 6, 20, 45, tzinfo=UTC), 15),   # ->21:00
            _c(4000, 4001, 3999, 4000, datetime(2026, 7, 6, 22, 0, tzinfo=UTC), 15)]     # 22:00->
    assert covers_window(bars, opened, now, "15min", cal) is True


def test_reconcile_timeframe_is_part_of_the_execution_fingerprint():
    from shadow.virtual_broker import execution_hash, execution_manifest
    base = dict(modeled_spread_pct=0.02, slippage_pct=0.005, single_position=True,
                cooldown_bars=0, risk_config_version="v", prefilter_version="pf")
    h15 = execution_hash(execution_manifest(config=ShadowConfig(reconcile_timeframe="15min"), **base))
    h1 = execution_hash(execution_manifest(config=ShadowConfig(reconcile_timeframe="1min"), **base))
    assert h15 != h1


def test_finer_bars_used_only_with_continuous_coverage_else_fall_back():
    """An M1 series that does not cover the trade from entry (a gap where an SL could hide) must
    NOT be used — fall back to M15. Full contiguous coverage is used."""
    from data_collector.session import calendar_for
    from shadow.reconciler import select_reconcile_bars_for_trade
    cal = calendar_for("csv")
    opened = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)   # Monday, market open
    now = datetime(2026, 7, 6, 14, 15, tzinfo=UTC)
    m15 = [_c(4000, 4030, 3980, 4020, opened, 15)]
    late_m1 = [_c(4000, 4001, 3999, 4000, opened + timedelta(minutes=5), 1)]   # starts AFTER entry
    bars, tf, fell, covered = select_reconcile_bars_for_trade(
        late_m1, m15, opened_at=opened, now=now, want_tf="1min", trigger_tf="15min", calendar=cal)
    assert (tf, fell, covered) == ("15min", True, True)           # M1 not covered -> fall back to M15
    good_m1 = [_c(4000, 4001, 3999, 4000, opened + timedelta(minutes=i), 1) for i in range(15)]
    bars, tf, fell, covered = select_reconcile_bars_for_trade(
        good_m1, m15, opened_at=opened, now=now, want_tf="1min", trigger_tf="15min", calendar=cal)
    assert (bars, tf, fell, covered) == (good_m1, "1min", False, True)   # full coverage -> use M1
    # not wanting finer -> coarse, covered
    assert select_reconcile_bars_for_trade(
        [], m15, opened_at=opened, now=now, want_tf="15min", trigger_tf="15min",
        calendar=cal) == (m15, "15min", False, True)


def test_timeout_is_a_fixed_duration_across_reconcile_granularity():
    """timeout_bars is a TRIGGER-timeframe horizon (~24h at 96 M15 bars). Reconciling at M1 must
    NOT expire the trade after 96 M1 bars (=96 min) — the horizon scales to 1440 M1 bars."""
    opened = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                               spread_provenance="modeled", opened_at=opened)
    flat_m1 = [_c(4000, 4001, 3999, 4000, opened + timedelta(minutes=i), 1) for i in range(200)]
    o = reconcile(trade, flat_m1, ShadowConfig(reconcile_timeframe="1min", trigger_timeframe="15min"))
    assert o.status == "open"                                     # 200 min < 24h horizon
    flat_m15 = [_c(4000, 4001, 3999, 4000, opened + timedelta(minutes=15 * i), 15) for i in range(100)]
    o15 = reconcile(trade, flat_m15, ShadowConfig())
    assert o15.status == "expired" and o15.exit_reason == "timeout"   # M15 behaviour unchanged


def test_shadow_config_rejects_invalid_values():
    """Strict bounds/enums: bad timeframe/hour/weekday/tz must fail at construction, not silently
    produce a wrong-outcome config downstream."""
    from pydantic import ValidationError
    for bad in (dict(timeout_bars=0), dict(rollover_hour_utc=24), dict(rollover_hour_utc=-1),
                dict(triple_swap_weekday=7), dict(reconcile_timeframe="5min"),
                dict(trigger_timeframe="2h"), dict(rollover_tz="Not/AZone")):
        with pytest.raises(ValidationError):
            ShadowConfig(**bad)
    # valid boundary values still construct
    ShadowConfig(timeout_bars=1, rollover_hour_utc=0, triple_swap_weekday=6,
                 reconcile_timeframe="1min", trigger_timeframe="15min", rollover_tz="Europe/Bucharest")


def test_reconcile_timeframe_survives_recovery():
    from shadow.virtual_broker import cost_manifest, shadow_config_from_costs
    trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                               spread_provenance="modeled", opened_at=datetime(2026, 7, 15, tzinfo=UTC))
    m = cost_manifest(trade, ShadowConfig(reconcile_timeframe="1min"))
    assert m["reconcile_timeframe"] == "1min"
    assert shadow_config_from_costs(m, timeout_bars=96).reconcile_timeframe == "1min"


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def test_summarize_edge_and_ambiguity_band():
    from shadow.metrics import summarize

    rows = [
        {"status": "closed", "exit_reason": "tp_hit", "r_multiple": 1.9, "r_pessimistic": 1.9, "r_optimistic": 1.9, "ambiguous": False},
        {"status": "closed", "exit_reason": "sl_hit", "r_multiple": -1.1, "r_pessimistic": -1.1, "r_optimistic": -1.1, "ambiguous": False},
        {"status": "closed", "exit_reason": "ambiguous", "r_multiple": -1.1, "r_pessimistic": -1.1, "r_optimistic": 1.9, "ambiguous": True},
        {"status": "open", "exit_reason": None, "r_multiple": None, "r_pessimistic": None, "r_optimistic": None, "ambiguous": False},
    ]
    s = summarize(rows)
    assert s["trades_closed"] == 3 and s["trades_open"] == 1
    assert s["win_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert s["ambiguity_rate"] == pytest.approx(1 / 3, abs=0.01)
    # band: pessimistic uses -1.1 for the ambiguous trade, optimistic uses +1.9
    assert s["avg_r_pessimistic"] < s["avg_r_optimistic"]
    assert s["exit_reasons"]["ambiguous"] == 1


def test_summarize_empty():
    from shadow.metrics import summarize

    assert summarize([])["trades_closed"] == 0
