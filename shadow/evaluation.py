"""Faza 5 — temporal fold report + deterministic baselines.

Replaces the naive "50-100 trades" threshold with an honest report:
- BASELINES a strategy must beat NET OF COSTS: the confluence rule (no-LLM), random, flat(0). All
  three actually TRADE (their trade confidence clears the Risk Engine's min_confidence).
- Expectancy with a BOOTSTRAP confidence interval, not a point estimate.
- Max drawdown in R.
- Confidence DISCRIMINATION (win rate per confidence bucket) — NOT ECE: confidence is ordinal, so
  ECE would need a probabilistic calibration fit on train first.
- Regime COVERAGE (trades per regime) — you cannot trust a stratum with too few trades.
- TEMPORAL FOLDS: one continuous run (full warm-up + position state), OUTPUT sliced by time. This
  is NOT a walk-forward (no train→OOS split); a real one needs a trainable maker (the LLM).

HONEST GAPS: the "LLM beats no-LLM / with-feedback vs without net of costs" verdict needs a PAID
`--maker claude` run (deferred). `--feedback` wires the as_of-safe track record into a persisted
backtest, so that comparison becomes runnable once a paid run is authorised.
"""

from __future__ import annotations

import random as _random
from statistics import mean

from core.models import Direction
from decision.schema import DecisionOutput
from shadow.metrics import summarize


def _auto_block_size(n: int) -> int:
    """A simple, deterministic block length ~ n**(1/3) (a common rule of thumb) for the moving-block
    bootstrap. 1 for tiny samples (falls back to IID)."""
    return max(1, min(n, round(n ** (1 / 3))))


def bootstrap_expectancy_ci(r_multiples: list[float], *, iters: int = 2000, alpha: float = 0.05,
                            seed: int = 12345, block_size: int | None = None) -> dict:
    """Bootstrap CI for mean R. Trade returns are SERIALLY CORRELATED (regime runs, one-position
    sequencing), so an IID resample understates the CI. Uses a MOVING-BLOCK bootstrap: resample
    contiguous blocks of length `block_size` (auto ~ n**(1/3) when None; 1 == IID). Deterministic."""
    rs = [float(x) for x in r_multiples]
    n = len(rs)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "alpha": alpha, "block_size": 0}
    b = _auto_block_size(n) if block_size is None else max(1, min(block_size, n))
    starts_max = n - b                      # inclusive max start of a full contiguous block
    n_blocks = -(-n // b)                    # ceil(n / b)
    rng = _random.Random(seed)
    means = []
    for _ in range(iters):
        sample: list[float] = []
        for _ in range(n_blocks):
            s = rng.randint(0, starts_max)
            sample.extend(rs[s:s + b])
        means.append(mean(sample[:n]))      # trim to the original length
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return {"n": n, "mean": round(mean(rs), 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "alpha": alpha, "block_size": b}


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


def confidence_discrimination(pairs: list[tuple[float, bool]], *, bins: int = 10) -> dict:
    """Does a HIGHER confidence go with a HIGHER win rate? — the only honest question for an
    ORDINAL confidence (schema.py declares confidence ordinal, NOT a probability). We report win
    rate per confidence bucket + `monotonic` (win rate non-decreasing across occupied buckets) +
    `spread` (top bucket win rate − bottom).

    We deliberately do NOT compute ECE: Expected Calibration Error assumes confidence is a
    probability, which requires a probabilistic calibration fit on train and evaluated OOS. On raw
    ordinal confidence ECE is meaningless. Discrimination is what's measurable now."""
    pts = [(float(c), bool(w)) for c, w in pairs]
    n = len(pts)
    if n == 0:
        return {"n": 0, "monotonic": None, "spread": None, "buckets": []}
    edges = [i / bins for i in range(bins + 1)]
    buckets = []
    for b in range(bins):
        lo, hi = edges[b], edges[b + 1]
        in_bin = [(c, w) for c, w in pts if (lo <= c < hi) or (b == bins - 1 and c == 1.0)]
        if not in_bin:
            continue
        buckets.append({"lo": round(lo, 2), "hi": round(hi, 2), "count": len(in_bin),
                        "avg_confidence": round(mean(c for c, _ in in_bin), 3),
                        "win_rate": round(mean(1.0 if w else 0.0 for _, w in in_bin), 3)})
    wrs = [b["win_rate"] for b in buckets]
    monotonic = all(wrs[i] <= wrs[i + 1] for i in range(len(wrs) - 1)) if len(wrs) > 1 else None
    spread = round(wrs[-1] - wrs[0], 3) if len(wrs) > 1 else None
    return {"n": n, "monotonic": monotonic, "spread": spread, "buckets": buckets}


def regime_coverage(rows: list[dict]) -> dict:
    """Closed trades per regime — the stratum sizes. `rows` carry a 'regime' key (may be None)."""
    cov: dict[str, int] = {}
    for r in rows:
        if r.get("r_multiple") is None:
            continue
        key = r.get("regime") or "unknown"
        cov[key] = cov.get(key, 0) + 1
    return dict(sorted(cov.items(), key=lambda kv: (-kv[1], kv[0])))


def temporal_folds(rows: list[dict], *, folds: int) -> list[dict]:
    """Partition ONE continuous run's rows into `folds` contiguous time segments (by as_of).

    This is a TEMPORAL FOLD REPORT, NOT a walk-forward: there is no train→OOS split because the
    deterministic baselines don't train. The whole run is executed ONCE — full indicator warm-up,
    position state (busy_until) carried across the whole window — and only the OUTPUT is sliced by
    time. (An earlier version sliced the WINDOWS per fold, which discarded warm-up, reset the open
    position at each boundary and left folds below MIN_BARS empty — all wrong.) A real
    walk-forward with a train/calibrate stage needs a trainable maker (the LLM, deferred)."""
    ordered = sorted(rows, key=lambda r: r["as_of"])
    n = len(ordered)
    if folds < 1 or n < folds:
        return [{"meta": {"fold": 0}, "rows": ordered}] if ordered else []
    size = n // folds
    out = []
    for f in range(folds):
        start = f * size
        end = n if f == folds - 1 else (f + 1) * size
        seg = ordered[start:end]
        if not seg:
            continue
        out.append({"meta": {"fold": f, "from": seg[0]["as_of"].isoformat(),
                             "to": seg[-1]["as_of"].isoformat(), "bars": len(seg)}, "rows": seg})
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
    Deterministic given the seed so a run is reproducible.

    `trade_confidence` must clear the Risk Engine's min_confidence (0.60) or the baseline would
    never actually open a trade — an earlier version used 0.5 and produced ZERO trades, making
    the 'beat random' comparison meaningless."""

    def __init__(self, seed: int = 7, p_trade: float = 0.5, trade_confidence: float = 0.7):
        self._rng = _random.Random(seed)
        self._p = p_trade
        self._conf = trade_confidence

    async def decide(self, inp) -> DecisionOutput:
        if self._rng.random() >= self._p:
            return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="rnd-flat")
        d = self._rng.choice([Direction.BUY, Direction.SELL])
        return DecisionOutput(direction=d, confidence=self._conf, rationale="rnd")


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
    m["confidence_discrimination"] = confidence_discrimination(cal_pairs)
    return m


async def evaluate_over_windows(windows, *, symbol, provider_name, modeled_spread_pct,
                                slippage_pct=0.0, commission_pct=0.0, swap_pct_per_night=0.0,
                                shadow_config=None, makers=None, folds=1) -> dict:
    """Run each baseline maker over the FULL window ONCE (in memory, NOTHING persisted), then
    report the full metric bundle overall + per temporal fold. One continuous run preserves
    indicator warm-up and the single-position state; folds are a slice of the OUTPUT, not of the
    input (see temporal_folds). ALL makers run on the SAME frozen windows with the SAME cost
    config, so the comparison is controlled.

    `makers` is {name: maker}; defaults to the three baselines. The LLM maker can be passed too,
    but the honest 'LLM beats baseline net of costs' verdict is only meaningful on a paid run.
    `commission_pct`/`swap_pct_per_night` are applied to every arm — if 0 (default), R is NOT net
    of financing and the report says so."""
    from shadow.runner import ConfluenceStrategy, backtest_over_windows
    from shadow.virtual_broker import ShadowConfig

    makers = makers or {"confluence": ConfluenceStrategy(), "random": RandomMaker(), "flat": FlatMaker()}
    # Use the FULL financing config when given (long/short swap, triple-swap day, DST tz, terms
    # version, reconcile timeframe) — the same canonical config as online; else the legacy 2 rates.
    cfg = shadow_config or ShadowConfig(commission_pct=commission_pct,
                                        swap_pct_per_night=swap_pct_per_night)
    swap_on = (cfg.swap_pct_per_night != 0 or (cfg.swap_long_pct_per_night or 0) != 0
               or (cfg.swap_short_pct_per_night or 0) != 0)
    cost_components = {                              # per-component: is this cost ACTUALLY modeled?
        "spread": modeled_spread_pct != 0,
        "slippage": slippage_pct != 0,
        "commission": cfg.commission_pct != 0,
        "swap": swap_on,
    }
    financing_modeled = cost_components["commission"] or cost_components["swap"]

    report: dict = {"symbol": symbol, "folds": folds, "financing_modeled": financing_modeled,
                    "cost_components": cost_components, "terms_version": cfg.terms_version,
                    "reconcile_timeframe": cfg.reconcile_timeframe, "makers": {}}
    for name, maker in makers.items():
        rows = await backtest_over_windows(
            windows, symbol=symbol, provider_name=provider_name, modeled_spread_pct=modeled_spread_pct,
            slippage_pct=slippage_pct, shadow_config=cfg, decision_maker=maker)
        entry = {"overall": _metrics_for(rows)}
        if folds > 1:
            entry["folds"] = [{"meta": fold["meta"], "metrics": _metrics_for(fold["rows"])}
                              for fold in temporal_folds(rows, folds=folds)]
        report["makers"][name] = entry
    return report


def format_report(report: dict) -> str:
    """Human-readable one-screen summary of an evaluate_over_windows result."""
    lines = [f"Temporal fold report — {report['symbol']} ({report['folds']} fold(s), ONE continuous run)", ""]
    header = f"{'maker':<12} {'trades':>7} {'win%':>6} {'exp_R':>7} {'CI(exp_R)':>18} {'maxDD_R':>8} {'discr':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, entry in report["makers"].items():
        m = entry["overall"]
        ci = m.get("expectancy_ci") or {}
        ci_s = f"[{ci.get('lo')}, {ci.get('hi')}]" if ci.get("lo") is not None else "—"
        spread = (m.get("confidence_discrimination") or {}).get("spread")
        lines.append(
            f"{name:<12} {m.get('trades_closed', 0):>7} "
            f"{(m.get('win_rate') or 0) * 100:>5.1f}% {m.get('expectancy_r', 0):>7} "
            f"{ci_s:>18} {m.get('max_drawdown_r', 0):>8} {spread if spread is not None else '—':>6}")
    lines.append("")
    # Build the cost caption from the PER-COMPONENT flags — never an aggregate that would claim
    # swap is modelled just because commission is set (or vice versa).
    comps = report.get("cost_components", {})
    order = ("spread", "slippage", "commission", "swap")
    modeled = [k for k in order if comps.get(k)]
    missing = [k for k in order if k in comps and not comps.get(k)]
    fin = "net of " + ("+".join(modeled) if modeled else "NOTHING")
    if missing:
        fin += f" (NOT modelled: {', '.join(missing)})"
    lines.append(f"Beat these baselines ({fin}): a real edge > confluence(no-LLM), > random, > flat(0).")
    lines.append("`discr` = win-rate spread across confidence buckets (ordinal); NOT ECE — confidence is")
    lines.append("ordinal, so probabilistic calibration (ECE) needs a train fit first.")
    lines.append("NOT a walk-forward (no train→OOS split); the LLM-vs-baseline verdict needs a paid run.")
    return "\n".join(lines)


def main() -> int:
    import argparse
    import asyncio

    from config.settings import load_settings
    from data_collector.providers.factory import build_provider

    parser = argparse.ArgumentParser(description="Faza 5 temporal fold report (deterministic baselines).")
    parser.add_argument("--count", type=int, default=1500, help="M15 bars to fetch")
    parser.add_argument("--folds", type=int, default=2, help="contiguous temporal folds (report only)")
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
        from shadow.virtual_broker import shadow_config_from_settings
        return await evaluate_over_windows(
            windows, symbol=symbol, provider_name=settings.market_data_provider,
            modeled_spread_pct=settings.replay_spread_pct, slippage_pct=settings.slippage_pct,
            shadow_config=shadow_config_from_settings(settings),   # full financing config (same as online)
            folds=args.folds)

    print(format_report(asyncio.run(_run())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
