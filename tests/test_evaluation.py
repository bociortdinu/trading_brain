"""Faza 5 report: bootstrap CI, drawdown, confidence DISCRIMINATION (ordinal, not ECE), regime
coverage, temporal folds, and the deterministic baselines (which must actually trade). Pure
functions asserted on known values; the baseline runner is an integration smoke over synthetic
windows (no DB, no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shadow.evaluation import (
    FlatMaker,
    RandomMaker,
    bootstrap_expectancy_ci,
    confidence_discrimination,
    evaluate_over_windows,
    max_drawdown_r,
    regime_coverage,
    temporal_folds,
)
from tests.helpers import run
from tests.synthetic import trend

UTC = timezone.utc
_END = datetime(2026, 7, 1, tzinfo=UTC)


def _windows(step=1.0, n=260):
    def w(tf_min):
        return trend(n=n, step=step, tf_min=tf_min, start=_END - timedelta(minutes=tf_min * n))
    return {"1day": w(1440), "4h": w(240), "1h": w(60), "15min": w(15)}


# ---- max drawdown ---- #
def test_max_drawdown_r():
    assert max_drawdown_r([]) == 0.0
    assert max_drawdown_r([1, 2, 3]) == 0.0                 # monotonic up -> no drawdown
    assert max_drawdown_r([1, -0.5, -0.5, 1]) == 1.0        # peak 1 -> trough 0
    assert max_drawdown_r([-1, -1]) == 2.0                  # straight down from 0


# ---- bootstrap CI ---- #
def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    ci = bootstrap_expectancy_ci([1.0, 1.0, 1.0])
    assert ci["mean"] == 1.0 and ci["lo"] == 1.0 and ci["hi"] == 1.0   # zero variance
    a = bootstrap_expectancy_ci([1.0, -1.0, 0.5, -0.3, 2.0])
    b = bootstrap_expectancy_ci([1.0, -1.0, 0.5, -0.3, 2.0])
    assert a == b                                                       # seeded -> reproducible
    assert a["lo"] <= a["mean"] <= a["hi"]
    assert bootstrap_expectancy_ci([])["mean"] is None


def test_block_bootstrap_widens_ci_for_serially_correlated_returns():
    """Trade returns are serially correlated; an IID resample understates the CI. The moving-block
    bootstrap (default) must give a WIDER interval than IID on a strongly-correlated sequence."""
    rs = [1.0] * 30 + [-1.0] * 30                       # long win run then long loss run
    iid = bootstrap_expectancy_ci(rs, block_size=1)
    blk = bootstrap_expectancy_ci(rs)                  # auto block ~ n**(1/3)
    assert blk["block_size"] > 1
    assert (blk["hi"] - blk["lo"]) > (iid["hi"] - iid["lo"])


# ---- confidence discrimination (ordinal, NOT ECE) ---- #
def test_confidence_discrimination_ordinal():
    # higher confidence -> higher win rate is MONOTONE and shows a positive spread.
    pairs = [(0.6, False), (0.6, False), (0.9, True), (0.9, True)]
    d = confidence_discrimination(pairs)
    buckets = {b["lo"]: b["win_rate"] for b in d["buckets"]}
    assert buckets[0.6] == 0.0 and buckets[0.9] == 1.0
    assert d["monotonic"] is True and d["spread"] == 1.0
    # a single confidence level -> no ordering to judge (monotonic/spread undefined).
    flat = confidence_discrimination([(0.7, True), (0.7, False)])
    assert flat["monotonic"] is None and flat["spread"] is None and len(flat["buckets"]) == 1
    assert confidence_discrimination([])["n"] == 0


# ---- regime coverage ---- #
def test_regime_coverage_counts_only_closed():
    rows = [
        {"regime": "bull_trend", "r_multiple": 1.0},
        {"regime": "bull_trend", "r_multiple": -1.0},
        {"regime": "range", "r_multiple": 0.5},
        {"regime": "range", "r_multiple": None},     # open -> ignored
        {"regime": None, "r_multiple": 0.2},          # missing regime -> 'unknown'
    ]
    assert regime_coverage(rows) == {"bull_trend": 2, "range": 1, "unknown": 1}


# ---- temporal folds (slice the OUTPUT of one continuous run, not the input) ---- #
def test_temporal_folds_partition_rows_by_time():
    rows = [{"as_of": _END + timedelta(minutes=15 * i)} for i in range(10)]
    folds = temporal_folds(rows, folds=2)
    assert len(folds) == 2
    assert folds[0]["meta"]["bars"] == 5 and folds[1]["meta"]["bars"] == 5
    assert [r["as_of"] for r in folds[0]["rows"]] == [rows[i]["as_of"] for i in range(5)]
    assert folds[0]["rows"][-1]["as_of"] < folds[1]["rows"][0]["as_of"]   # contiguous
    assert temporal_folds([], folds=2) == []


# ---- baselines ---- #
def test_all_baselines_actually_trade():
    """The point of a baseline is to trade so it can be beaten. RandomMaker's trade confidence now
    clears the risk gate — an earlier 0.5 was below min_confidence (0.60) and produced ZERO
    trades, making the whole 'beat random' comparison meaningless. On the same trending window the
    prefilter passes, random opens BUY/SELL and they resolve."""
    report = run(evaluate_over_windows(
        _windows(n=320), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"random": RandomMaker(p_trade=1.0)}))   # always trade -> exercises the gate
    assert report["makers"]["random"]["overall"]["trades_closed"] > 0    # <- the bug: was 0

    flat = run(evaluate_over_windows(
        _windows(n=320), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"flat": FlatMaker()}))
    assert flat["makers"]["flat"]["overall"]["trades_closed"] == 0       # flat legitimately never trades


def test_eval_reports_per_component_costs_and_uses_full_financing_config():
    """P1: report each cost component separately (a single rate being set is NOT 'full financing
    modelled'), and accept the SAME canonical financing config the online path uses."""
    from shadow.virtual_broker import ShadowConfig
    r0 = run(evaluate_over_windows(_windows(), symbol="GOLD", provider_name="csv",
                                   modeled_spread_pct=0.02, slippage_pct=0.005))
    assert r0["cost_components"] == {"spread": True, "slippage": True,
                                     "commission": False, "swap": False}
    assert r0["financing_modeled"] is False
    cfg = ShadowConfig(swap_long_pct_per_night=0.01, triple_swap_weekday=2, terms_version="xtb-2026-07")
    r1 = run(evaluate_over_windows(_windows(), symbol="GOLD", provider_name="csv",
                                   modeled_spread_pct=0.02, slippage_pct=0.005, shadow_config=cfg))
    assert r1["cost_components"]["swap"] is True and r1["financing_modeled"] is True
    assert r1["terms_version"] == "xtb-2026-07"


def test_evaluate_reports_all_baselines_with_full_metrics():
    report = run(evaluate_over_windows(
        _windows(), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02))
    assert set(report["makers"]) == {"confluence", "random", "flat"}
    for name in ("confluence", "random", "flat"):
        m = report["makers"][name]["overall"]
        for key in ("expectancy_ci", "max_drawdown_r", "regime_coverage", "confidence_discrimination"):
            assert key in m


def test_temporal_fold_report_has_per_fold_metrics():
    report = run(evaluate_over_windows(
        _windows(), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"random": RandomMaker()}, folds=2))
    entry = report["makers"]["random"]
    assert "folds" in entry and len(entry["folds"]) == 2
    assert entry["folds"][0]["meta"]["fold"] == 0
