"""Faza 5 evaluation protocol: bootstrap CI, drawdown, calibration/ECE, coverage, walk-forward,
and the deterministic baselines. Pure functions asserted on known values; the baseline runner is
an integration smoke over synthetic windows (no DB, no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from features.mtf import TRIGGER_TF
from shadow.evaluation import (
    FlatMaker,
    RandomMaker,
    bootstrap_expectancy_ci,
    calibration,
    evaluate_over_windows,
    max_drawdown_r,
    regime_coverage,
    walk_forward_windows,
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


# ---- calibration / ECE ---- #
def test_calibration_ece_known_values():
    # constant 0.7 confidence, 50% win rate -> one bin, ECE = |0.7 - 0.5| = 0.2.
    c = calibration([(0.7, True), (0.7, False)])
    assert c["n"] == 2 and c["ece"] == 0.2 and len(c["bins"]) == 1
    assert c["bins"][0]["avg_confidence"] == 0.7 and c["bins"][0]["win_rate"] == 0.5
    # perfectly calibrated -> ECE 0.
    perfect = calibration([(0.9, True)] * 9 + [(0.9, False)], bins=10)
    assert perfect["ece"] == 0.0
    assert calibration([])["ece"] is None


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


# ---- walk-forward split ---- #
def test_walk_forward_splits_m15_contiguously_without_overlap():
    windows = _windows(n=260)
    folds = walk_forward_windows(windows, folds=2)
    assert len(folds) == 2
    m15_total = windows[TRIGGER_TF]
    seg0, seg1 = folds[0][TRIGGER_TF], folds[1][TRIGGER_TF]
    assert len(seg0) + len(seg1) == len(m15_total)              # partition, no loss
    assert seg0[-1].close_time < seg1[0].close_time             # contiguous, non-overlapping
    # higher-TF context is kept up to each fold's end, never beyond it.
    for fold in folds:
        end = fold[TRIGGER_TF][-1].close_time
        assert all(c.close_time <= end for c in fold["1day"])
    assert folds[0]["_meta"]["fold"] == 0


# ---- baselines ---- #
def test_flat_baseline_never_trades():
    report = run(evaluate_over_windows(
        _windows(), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"flat": FlatMaker()}))
    flat = report["makers"]["flat"]["overall"]
    assert flat["trades_closed"] == 0                          # flat opens nothing -> expectancy N/A


def test_evaluate_reports_all_baselines_with_full_metrics():
    report = run(evaluate_over_windows(
        _windows(), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"confluence": None, "random": RandomMaker(), "flat": FlatMaker()}
        if False else None))   # default -> confluence + random + flat
    assert set(report["makers"]) == {"confluence", "random", "flat"}
    for name in ("confluence", "random", "flat"):
        m = report["makers"][name]["overall"]
        for key in ("expectancy_ci", "max_drawdown_r", "regime_coverage", "confidence_calibration"):
            assert key in m


def test_walk_forward_reports_per_fold():
    report = run(evaluate_over_windows(
        _windows(), symbol="GOLD", provider_name="csv", modeled_spread_pct=0.02,
        makers={"confluence": None} if False else {"random": RandomMaker()}, folds=2))
    entry = report["makers"]["random"]
    assert "folds" in entry and len(entry["folds"]) == 2
    assert entry["folds"][0]["meta"]["fold"] == 0
