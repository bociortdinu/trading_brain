"""Faza 5 — walk-forward evaluation protocol (deterministic parts).

Replaces the naive "50-100 trades" threshold with an honest report:
- BASELINES a strategy must beat: the deterministic confluence rule (no-LLM), random, and flat.
- Expectancy with a BOOTSTRAP confidence interval, not a point estimate.
- Max drawdown in R.
- Confidence CALIBRATION (reliability bins + ECE) — meaningful once a maker emits varied
  confidence (the LLM); constant-confidence makers collapse to one bin, honestly.
- Regime COVERAGE (trades per regime) — you cannot trust a stratum with too few trades.
- WALK-FORWARD: contiguous out-of-sample segments, each reported separately.

What is NOT here (honest): the "LLM beats no-LLM / no-feedback net of costs" verdict needs an
actual paid LLM run (deferred by the user); the no-feedback-vs-feedback baseline is an LLM-only
comparison. This module provides the framework + the deterministic baselines + all the metrics.
"""

from __future__ import annotations

import random as _random
from statistics import mean

from core.models import Direction
from data_collector.providers.base import Candle
from decision.schema import DecisionOutput
from features.mtf import TRIGGER_TF
from shadow.metrics import summarize


def bootstrap_expectancy_ci(r_multiples: list[float], *, iters: int = 2000, alpha: float = 0.05,
                            seed: int = 12345) -> dict:
    """Bootstrap CI for mean R: resample WITH replacement `iters` times, take the alpha/2 and
    1-alpha/2 percentiles of the resampled means. Deterministic (seeded)."""
    rs = [float(x) for x in r_multiples]
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "alpha": alpha}
    rng = _random.Random(seed)
    means = []
    for _ in range(iters):
        means.append(mean(rng.choices(rs, k=n)))
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return {"n": n, "mean": round(mean(rs), 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "alpha": alpha}


def max_drawdown_r(r_multiples: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative-R equity curve (>= 0). 0 for no trades."""
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for r in r_multiples:
        equity += float(r)
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 4)


def calibration(pairs: list[tuple[float, bool]], *, bins: int = 10) -> dict:
    """Reliability bins + Expected Calibration Error for (confidence, won) pairs.

    Each bin: mean confidence, empirical win rate (accuracy), count. ECE = sum over bins of
    (count/N) * |confidence - accuracy|. A well-calibrated maker has confidence ~= win rate; a
    constant-confidence maker collapses to a single occupied bin (reported honestly, not hidden).
    """
    pts = [(float(c), bool(w)) for c, w in pairs]
    n = len(pts)
    if n == 0:
        return {"n": 0, "ece": None, "bins": []}
    edges = [i / bins for i in range(bins + 1)]
    out_bins = []
    ece = 0.0
    for b in range(bins):
        lo, hi = edges[b], edges[b + 1]
        # last bin is closed on the right so confidence == 1.0 lands somewhere.
        in_bin = [(c, w) for c, w in pts if (lo <= c < hi) or (b == bins - 1 and c == 1.0)]
        if not in_bin:
            continue
        cnt = len(in_bin)
        conf = mean(c for c, _ in in_bin)
        acc = mean(1.0 if w else 0.0 for _, w in in_bin)
        ece += (cnt / n) * abs(conf - acc)
        out_bins.append({"lo": round(lo, 2), "hi": round(hi, 2), "count": cnt,
                         "avg_confidence": round(conf, 3), "win_rate": round(acc, 3)})
    return {"n": n, "ece": round(ece, 4), "bins": out_bins}


def regime_coverage(rows: list[dict]) -> dict:
    """Closed trades per regime — the stratum sizes. `rows` carry a 'regime' key (may be None)."""
    cov: dict[str, int] = {}
    for r in rows:
        if r.get("r_multiple") is None:
            continue
        key = r.get("regime") or "unknown"
        cov[key] = cov.get(key, 0) + 1
    return dict(sorted(cov.items(), key=lambda kv: (-kv[1], kv[0])))


def walk_forward_windows(windows: dict[str, list[Candle]], *, folds: int) -> list[dict[str, list[Candle]]]:
    """Split into `folds` CONTIGUOUS out-of-sample segments by M15 time. Each fold keeps the FULL
    higher-timeframe history up to that fold's end (higher-TF context must not be truncated), but
    only the fold's own slice of M15 bars is 'the test window'. Folds are non-overlapping in M15."""
    m15 = windows[TRIGGER_TF]
    n = len(m15)
    if folds < 1 or n < folds:
        return [windows]
    size = n // folds
    out = []
    for f in range(folds):
        start = f * size
        end = n if f == folds - 1 else (f + 1) * size
        seg_m15 = m15[start:end]
        if not seg_m15:
            continue
        fold_end = seg_m15[-1].close_time
        seg_start = seg_m15[0].open_time
        fold = {}
        for tf, bars in windows.items():
            if tf == TRIGGER_TF:
                fold[tf] = seg_m15
            else:
                # keep higher-TF bars up to the fold end (context), from the start (warm-up).
                fold[tf] = [c for c in bars if c.close_time <= fold_end]
        fold["_meta"] = {"fold": f, "m15_from": seg_start.isoformat(), "m15_to": fold_end.isoformat()}
        out.append(fold)
    return out


# --------------------------------------------------------------------------- #
# Deterministic baselines a real strategy must beat (no paid LLM).
# --------------------------------------------------------------------------- #
class FlatMaker:
    """The trivial baseline: never trade. Its expectancy is exactly 0 — anything that does not
    beat FLAT net of costs has no reason to place orders."""

    async def decide(self, inp) -> DecisionOutput:
        return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="flat")


class RandomMaker:
    """Seeded coin-flip BUY/SELL/NO_TRADE. Beating RANDOM net of costs is the floor for 'signal'.
    Deterministic given the seed so a run is reproducible."""

    def __init__(self, seed: int = 7, p_trade: float = 0.5):
        self._rng = _random.Random(seed)
        self._p = p_trade

    async def decide(self, inp) -> DecisionOutput:
        if self._rng.random() >= self._p:
            return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="rnd-flat")
        d = self._rng.choice([Direction.BUY, Direction.SELL])
        return DecisionOutput(direction=d, confidence=0.5, rationale="rnd")


def _metrics_for(rows: list[dict]) -> dict:
    """Full metric bundle for a set of backtest rows: edge summary + bootstrap CI + drawdown +
    regime coverage + confidence calibration."""
    outcomes = [r["outcome"] for r in rows if r.get("outcome") is not None]
    closed = [o for o in outcomes if o.get("r_multiple") is not None]
    rs = [float(o["r_multiple"]) for o in closed]
    # (confidence, won) pairs and regime rows for calibration/coverage.
    cal_pairs, cov_rows = [], []
    for r in rows:
        o = r.get("outcome")
        if not o or o.get("r_multiple") is None:
            continue
        cov_rows.append({"regime": r.get("regime"), "r_multiple": o["r_multiple"]})
        if r.get("confidence") is not None:
            cal_pairs.append((r["confidence"], float(o["r_multiple"]) > 0))
    m = summarize(outcomes)
    m["expectancy_ci"] = bootstrap_expectancy_ci(rs)
    m["max_drawdown_r"] = max_drawdown_r(rs)
    m["regime_coverage"] = regime_coverage(cov_rows)
    m["confidence_calibration"] = calibration(cal_pairs)
    return m


async def evaluate_over_windows(windows, *, symbol, provider_name, modeled_spread_pct,
                                slippage_pct=0.0, makers=None, folds=1) -> dict:
    """Run each baseline maker over the walk-forward folds (in memory, NOTHING persisted) and
    return a comparison report: per maker, overall + per fold, the full metric bundle.

    `makers` is {name: maker}; defaults to the three baselines. The LLM maker can be passed too,
    but the honest 'LLM beats baseline net of costs' verdict is only meaningful on a paid run."""
    from shadow.runner import ConfluenceStrategy, backtest_over_windows

    makers = makers or {"confluence": ConfluenceStrategy(), "random": RandomMaker(), "flat": FlatMaker()}
    fold_windows = walk_forward_windows(windows, folds=folds)

    report: dict = {"symbol": symbol, "folds": folds, "makers": {}}
    for name, maker in makers.items():
        overall_rows: list[dict] = []
        per_fold = []
        for fw in fold_windows:
            meta = fw.get("_meta", {})
            w = {k: v for k, v in fw.items() if k != "_meta"}
            rows = await backtest_over_windows(
                w, symbol=symbol, provider_name=provider_name, modeled_spread_pct=modeled_spread_pct,
                slippage_pct=slippage_pct, decision_maker=maker)
            overall_rows.extend(rows)
            if folds > 1:
                per_fold.append({"meta": meta, "metrics": _metrics_for(rows)})
        entry = {"overall": _metrics_for(overall_rows)}
        if folds > 1:
            entry["folds"] = per_fold
        report["makers"][name] = entry
    return report


def format_report(report: dict) -> str:
    """Human-readable one-screen summary of an evaluate_over_windows result."""
    lines = [f"Walk-forward evaluation — {report['symbol']} ({report['folds']} fold(s))", ""]
    header = f"{'maker':<12} {'trades':>7} {'win%':>6} {'exp_R':>7} {'CI(exp_R)':>18} {'maxDD_R':>8} {'ECE':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, entry in report["makers"].items():
        m = entry["overall"]
        ci = m.get("expectancy_ci") or {}
        ci_s = f"[{ci.get('lo')}, {ci.get('hi')}]" if ci.get("lo") is not None else "—"
        ece = (m.get("confidence_calibration") or {}).get("ece")
        lines.append(
            f"{name:<12} {m.get('trades_closed', 0):>7} "
            f"{(m.get('win_rate') or 0) * 100:>5.1f}% {m.get('expectancy_r', 0):>7} "
            f"{ci_s:>18} {m.get('max_drawdown_r', 0):>8} {ece if ece is not None else '—':>6}")
    lines.append("")
    lines.append("Baselines to beat NET OF COSTS: a real edge > confluence(no-LLM), > random, > flat(0).")
    lines.append("NOTE: LLM-vs-baseline needs a paid --maker claude run (deferred); this run is deterministic.")
    return "\n".join(lines)


def main() -> int:
    import argparse
    import asyncio

    from config.settings import load_settings
    from data_collector.providers.factory import build_provider

    parser = argparse.ArgumentParser(description="Faza 5 walk-forward evaluation (deterministic baselines).")
    parser.add_argument("--count", type=int, default=1500, help="M15 bars to fetch")
    parser.add_argument("--folds", type=int, default=2, help="walk-forward out-of-sample folds")
    args = parser.parse_args()
    settings = load_settings()

    async def _run() -> dict:
        provider = build_provider(settings)
        symbol = settings.symbol_query
        provider_symbol = settings.provider_symbol(symbol)
        try:
            windows = {tf: await provider.get_ohlcv(provider_symbol, tf, args.count)
                       for tf in settings.timeframes}
        finally:
            aclose = getattr(provider, "aclose", None)
            if aclose:
                await aclose()
        return await evaluate_over_windows(
            windows, symbol=symbol, provider_name=settings.market_data_provider,
            modeled_spread_pct=settings.replay_spread_pct, slippage_pct=settings.slippage_pct,
            folds=args.folds)

    print(format_report(asyncio.run(_run())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
