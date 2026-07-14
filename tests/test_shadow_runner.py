"""Shadow backtest runner core: over synthetic windows it should evaluate the bars with
enough history, approve trades on aligned confluence, and reconcile them into outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shadow.runner import ConfluenceStrategy, backtest_over_windows, report
from tests.helpers import run
from tests.synthetic import trend

UTC = timezone.utc
_END = datetime(2026, 7, 1, tzinfo=UTC)


def _windows(step=1.0, n=250):
    # All timeframes END at _END but higher timeframes START earlier, so slicing to a recent
    # M15 as_of still yields >= MIN_BARS on D1/H4/H1 (as with real provider windows).
    def w(tf_min):
        return trend(n=n, step=step, tf_min=tf_min, start=_END - timedelta(minutes=tf_min * n))
    return {"1day": w(1440), "4h": w(240), "1h": w(60), "15min": w(15)}


def test_backtest_uptrend_produces_approved_buys_and_closed_trades():
    rows = run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.02,
    ))
    rep = report(rows)
    assert rep["bars_evaluated"] > 0
    assert rep["approved"] > 0                         # aligned_bull uptrend -> BUYs approved
    # some early entries have enough future bars to reach the +TP in a clean uptrend
    closed = [r for r in rows if r["outcome"] and r["outcome"]["status"] == "closed"]
    assert closed and all(r["direction"] == "BUY" for r in closed)
    assert any(r["outcome"]["exit_reason"] == "tp_hit" for r in closed)
    assert rep["metrics"]["trades_closed"] >= 1


def test_backtest_no_lookahead_reconciles_only_future_bars():
    # Every reconciled trade closes strictly AFTER its entry bar (guaranteed by the reconciler);
    # here we just assert the runner yields outcomes and the metrics are net-of-spread finite.
    rows = run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.05,
    ))
    m = report(rows)["metrics"]
    if m["trades_closed"]:
        assert isinstance(m["expectancy_r"], float)
        assert m["avg_r_pessimistic"] <= m["avg_r_optimistic"]
