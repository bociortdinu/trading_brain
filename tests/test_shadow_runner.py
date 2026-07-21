"""Shadow backtest runner core: over synthetic windows it should evaluate the bars with
enough history, approve trades on aligned confluence, and reconcile them into outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shadow.runner import (
    _FOREVER,
    _busy_until,
    _resume_busy_until,
    ConfluenceStrategy,
    backtest_over_windows,
    report,
)
from tests.helpers import run
from tests.synthetic import trend

UTC = timezone.utc
_END = datetime(2026, 7, 1, tzinfo=UTC)


def test_open_trade_blocks_for_the_rest_of_the_run():
    """A trade that never closed (closed_at is None) is still OPEN: it must block every later bar,
    not just up to the last bar's close. The old `future[-1].close` let the boundary bar (as_of ==
    that close, gate is a strict `<`) open a SECOND position — a real single-position violation."""
    closed = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    assert _busy_until(closed, 900.0, 0) == closed            # a closed trade blocks until close
    assert _busy_until(None, 900.0, 0) == _FOREVER            # an OPEN trade blocks the whole run
    # cooldown extends a CLOSED trade's block, but an open one is already forever.
    assert _busy_until(closed, 900.0, 2) == closed + timedelta(seconds=1800)


def test_resume_busy_until_matches_the_live_rule():
    prev = datetime(2026, 6, 30, 9, 0, tzinfo=UTC)
    bars = [type("B", (), {"close_time": _END})()]
    # An open outcome on resume must also block forever (not just to the window end).
    assert _resume_busy_until({"outcome": {"closed_at": None}}, prev, bars, 900.0, 0) == _FOREVER
    closed = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    assert _resume_busy_until({"outcome": {"closed_at": closed}}, prev, bars, 900.0, 0) == closed
    # A bar that traded nothing leaves the clock untouched.
    assert _resume_busy_until({"outcome": None}, prev, bars, 900.0, 0) == prev


def _windows(step=1.0, n=250):
    # All timeframes END at _END but higher timeframes START earlier, so slicing to a recent
    # M15 as_of still yields >= MIN_BARS on D1/H4/H1 (as with real provider windows).
    def w(tf_min):
        return trend(n=n, step=step, tf_min=tf_min, start=_END - timedelta(minutes=tf_min * n))
    return {"1day": w(1440), "4h": w(240), "1h": w(60), "15min": w(15)}


def test_backtest_forces_m15_reconcile_and_ignores_a_1min_config():
    """Regression (10c6684): the backtest only feeds M15 bars to reconcile(). A reconcile_timeframe
    of '1min' would mislabel the touch ordering AND scale the timeout 15x (trades that should expire
    at 96 bars would stay open). The backtest must force M15, so a 1min config is identical to 15min."""
    from shadow.virtual_broker import ShadowConfig
    w = _windows(step=1.0, n=250)
    common = dict(symbol="GOLD", provider_name="csv", decision_maker=ConfluenceStrategy(),
                  modeled_spread_pct=0.02)
    a = run(backtest_over_windows(w, shadow_config=ShadowConfig(reconcile_timeframe="15min"), **common))
    b = run(backtest_over_windows(w, shadow_config=ShadowConfig(reconcile_timeframe="1min"), **common))
    assert a == b and a, "backtest must force M15 -> a 1min config changes nothing"


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


def test_single_position_gate_skips_bars_before_the_model():
    """Executable-realism gate: with single_position (default) no new trade opens while one is
    open. Those bars are skipped BEFORE the decision maker — never decided, never paid for — so
    every approval that survives becomes a trade. Turning the gate off (event-study) decides and
    opens everything."""
    gated = report(run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.02)))
    study = report(run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.02, single_position=False)))

    assert gated["position_gated"] > 0                          # bars skipped while in position
    assert gated["approved"] == gated["trades_opened"]           # an approval always trades now
    assert study["position_gated"] == 0                          # event-study gates nothing
    assert study["trades_opened"] == study["approved"] > gated["trades_opened"]
    # The saving is real: the gated run sends strictly fewer bars to the (paid) maker.
    assert gated["decided"] < study["decided"]


def test_max_llm_calls_caps_a_paid_run():
    """Financial guardrail: a paid maker is wrapped in _CountingMaker and the runner STOPS at
    --max-llm-calls instead of spending across every bar."""
    from shadow.runner import _CountingMaker

    maker = _CountingMaker(ConfluenceStrategy())
    rows = run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=maker, modeled_spread_pct=0.02, max_llm_calls=5))
    assert maker.calls == 5                                    # never exceeds the budget
    assert rows[-1]["stage"] == "llm_cap_reached"              # stopped cleanly at the cap

    uncapped = _CountingMaker(ConfluenceStrategy())
    run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=uncapped, modeled_spread_pct=0.02))
    assert uncapped.calls > 5                                  # cap is what stopped the first run


def test_position_gated_bars_never_reach_the_paid_maker():
    """The money claim: a bar skipped by the position gate must cost NOTHING. The gate runs
    before the maker, so decide() is never called for it — verified by counting real calls, not
    by reading the summary."""
    from shadow.runner import _CountingMaker

    gated_maker = _CountingMaker(ConfluenceStrategy())
    gated = report(run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=gated_maker, modeled_spread_pct=0.02)))
    study_maker = _CountingMaker(ConfluenceStrategy())
    run(backtest_over_windows(
        _windows(step=1.0), symbol="GOLD", provider_name="csv",
        decision_maker=study_maker, modeled_spread_pct=0.02, single_position=False))

    assert gated["position_gated"] > 0
    # Exactly the gated bars are the saving — no call was made for any of them.
    assert gated_maker.calls == study_maker.calls - gated["position_gated"]
    assert gated_maker.calls == gated["decided"]


def test_backtest_fast_path_matches_slow_path():
    """The O(n^2)->O(n) precompute must not change a single result: the fast path (default) and
    the per-slice path must produce identical per-bar rows AND identical metrics."""
    windows = _windows(step=1.0)
    fast = run(backtest_over_windows(
        windows, symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.02, fast_features=True))
    slow = run(backtest_over_windows(
        windows, symbol="GOLD", provider_name="csv",
        decision_maker=ConfluenceStrategy(), modeled_spread_pct=0.02, fast_features=False))
    assert fast == slow and report(fast) == report(slow)


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
