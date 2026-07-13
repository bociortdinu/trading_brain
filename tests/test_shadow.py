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
