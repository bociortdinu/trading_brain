"""Shadow backtest runner: replay historical bars, produce reconciled shadow trades + edge
metrics — WITHOUT real money and (with the deterministic strategy) WITHOUT paid LLM calls.

For each M15 close that has enough history on every timeframe, it builds the as-of packet
with a MODELED spread (never the live quote — anti look-ahead), runs the SAME decision
pipeline (prefilter -> decide -> Risk Engine), and if the verdict is approved opens a virtual
trade and reconciles it against the bars that FOLLOW the entry. The result is a distribution
of R-multiples (net of the round-trip spread) with the pessimistic/optimistic ambiguity band.

`ConfluenceStrategy` is a deterministic stand-in for the LLM so the mechanism can be
validated and metrics produced at zero cost; swap in the Anthropic maker for a real (paid)
measurement of the model's edge on identical inputs.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from core.models import Direction
from data_collector.providers.base import Candle
from data_collector.providers.factory import build_provider
from data_collector.session import calendar_for
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig
from decision.schema import DecisionOutput, build_decision_input
from features.eligibility import EligibilityConfig, evaluate_eligibility
from features.engineering import MIN_BARS
from features.mtf import TRIGGER_TF, build_feature_packet
from risk.engine import RiskConfig
from shadow.metrics import summarize
from shadow.reconciler import reconcile
from shadow.virtual_broker import ShadowConfig, open_virtual_trade


class ConfluenceStrategy:
    """Deterministic LLM stand-in for cost-free backtests: BUY on aligned_bull, SELL on
    aligned_bear, else NO_TRADE. NOT a real edge — it exercises the backtest + reconciler."""

    async def decide(self, inp) -> DecisionOutput:
        if inp.confluence == "aligned_bull":
            return DecisionOutput(direction=Direction.BUY, confidence=0.7, rationale="aligned_bull")
        if inp.confluence == "aligned_bear":
            return DecisionOutput(direction=Direction.SELL, confidence=0.7, rationale="aligned_bear")
        return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="not aligned")


def _slice(windows: dict[str, list[Candle]], as_of: datetime) -> dict[str, list[Candle]]:
    return {tf: [c for c in cs if c.close_time <= as_of] for tf, cs in windows.items()}


def _bar_seconds(bars: list[Candle]) -> float:
    """Median M15 bar length in seconds (robust to gaps), for the post-close cooldown clock."""
    deltas = sorted((bars[i + 1].open_time - bars[i].open_time).total_seconds()
                    for i in range(len(bars) - 1))
    return deltas[len(deltas) // 2] if deltas else 900.0


async def backtest_over_windows(
    windows: dict[str, list[Candle]],
    *,
    symbol: str,
    provider_name: str,
    decision_maker,
    modeled_spread_pct: float,
    slippage_pct: float = 0.0,
    prefilter_config: PrefilterConfig | None = None,
    risk_config: RiskConfig | None = None,
    eligibility_config: EligibilityConfig | None = None,
    shadow_config: ShadowConfig | None = None,
    min_bars: int = MIN_BARS,
    single_position: bool = True,   # one position at a time (executable); False = event-study
    cooldown_bars: int = 0,         # extra bars to wait AFTER a trade closes before re-entry
    persist_dsn: str | None = None,
    run_id: str | None = None,
    model_name: str = "deterministic-confluence",
) -> list[dict]:
    """Backtest over provided windows. Returns per-bar dicts:
    {as_of, stage, direction, approved, outcome(dict|None)}. When `persist_dsn`+`run_id` are
    given, the full chain (snapshot -> evaluation -> decision -> trade) is written for each
    APPROVED trade so the run leaves auditable shadow trades in the DB (idempotent on
    (decision_id, run_id))."""
    calendar = calendar_for(provider_name)
    prefilter_config = prefilter_config or PrefilterConfig()
    risk_config = risk_config or RiskConfig()
    eligibility_config = eligibility_config or EligibilityConfig()
    shadow_config = shadow_config or ShadowConfig()

    m15 = windows[TRIGGER_TF]
    out: list[dict] = []
    # Position policy: a single account holds ONE position at a time. Without this gate the
    # backtest opens a new trade on every approved bar (the reviewer saw 23 concurrent
    # positions), which is NOT an executable strategy — the summed R is meaningless. We block
    # new entries while a trade is open, plus an optional cooldown after it closes.
    busy_until = None  # datetime: no new entry strictly before this (open trade + cooldown)
    bar_seconds = _bar_seconds(m15)
    for as_of in (c.close_time for c in m15):
        sliced = _slice(windows, as_of)
        if any(len(sliced.get(tf, [])) < min_bars for tf in windows):
            continue  # not enough history on some timeframe yet
        packet = build_feature_packet(
            symbol, sliced, as_of=as_of, provider=provider_name, provider_symbol=symbol,
            ingested_at=as_of, spread_pct=modeled_spread_pct, calendar=calendar,
        )
        elig = evaluate_eligibility(sliced, TRIGGER_TF, as_of, mode="replay", now=as_of,
                                    config=eligibility_config, calendar=calendar)
        rec = await run_decision(packet, elig, decision_maker, mode="replay",
                                 prefilter_config=prefilter_config, risk_config=risk_config,
                                 calendar=calendar)
        row = {"as_of": as_of, "stage": rec.stage,
               "direction": rec.decision.direction.value if rec.decision else "NO_TRADE",
               "approved": rec.risk_approved, "blocked": None, "outcome": None}
        if rec.risk_approved:
            if single_position and busy_until is not None and as_of < busy_until:
                row["blocked"] = "position_open"   # approved but not tradeable -> not a trade
                out.append(row)
                continue
            future = [c for c in m15 if c.open_time >= as_of]
            entry_ref = future[0].open if future else packet.price  # fill at next bar's open (latency)
            trade = open_virtual_trade(
                rec.decision.direction, entry_ref, rec.risk.sl_pct, rec.risk.tp_pct,
                spread_pct=modeled_spread_pct, spread_provenance="modeled",
                slippage_pct=slippage_pct, opened_at=as_of,
            )
            o = reconcile(trade, future, shadow_config)
            row["outcome"] = o.model_dump()
            if single_position:
                closed_at = o.closed_at or (future[-1].close_time if future else as_of)
                busy_until = closed_at + timedelta(seconds=bar_seconds * cooldown_bars)
            if persist_dsn and run_id:
                _persist_chain(persist_dsn, run_id, model_name, symbol, provider_name,
                               packet, elig, rec, trade, o, shadow_config)
        out.append(row)
    return out


def _persist_chain(dsn, run_id, model_name, symbol, provider_name, packet, elig, rec, trade,
                   outcome, shadow_config) -> None:
    """Write snapshot -> evaluation -> decision -> trade for one approved backtest trade."""
    from database.repository import (
        find_shadow_trade_by_input, insert_decision, insert_evaluation, upsert_shadow_trade,
        upsert_snapshot,
    )
    from shadow.virtual_broker import cost_manifest

    # END-TO-END IDEMPOTENCY: re-running a backtest with the same run_id must NOT duplicate the
    # chain. Each run mints a new decision_id, so UNIQUE(decision_id, run_id) can't dedupe — we
    # dedupe on the frozen input (input_hash + model) within this run_id and skip if it exists.
    if find_shadow_trade_by_input(dsn, input_hash=rec.input_hash, model=model_name,
                                  run_id=run_id) is not None:
        return
    status, snap_id = upsert_snapshot(dsn, packet)
    if snap_id is None or status == "conflict":
        return
    eval_id = insert_evaluation(dsn, snap_id, elig)
    inp = build_decision_input(packet, mode="replay")
    dec_id = insert_decision(
        dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name, record=rec,
        ai_input=inp.model_dump(mode="json"),
        ai_output=rec.decision.model_dump(mode="json") if rec.decision else None,
        mode="shadow", data_provider=provider_name,
    )
    upsert_shadow_trade(
        dsn, decision_id=dec_id, run_id=run_id, symbol=symbol, trade=trade, outcome=outcome,
        timeframe=TRIGGER_TF, timeout_bars=shadow_config.timeout_bars,
        costs=cost_manifest(trade, shadow_config),
    )


def report(rows: list[dict]) -> dict:
    """Backtest summary: bars evaluated, approvals, position-gate blocks, and edge metrics over
    the trades ACTUALLY opened. `approved` counts every risk-approved signal; `blocked` are
    approvals suppressed because a position was already open; `trades_opened` is what the
    metrics are computed over. With single_position, approved == blocked + trades_opened."""
    outcomes = [r["outcome"] for r in rows if r["outcome"] is not None]
    blocked = sum(1 for r in rows if r.get("blocked"))
    return {
        "bars_evaluated": len(rows),
        "prefiltered_out": sum(1 for r in rows if r["stage"] == "prefiltered_out"),
        "approved": sum(1 for r in rows if r["approved"]),
        "blocked_position_open": blocked,
        "trades_opened": len(outcomes),
        "metrics": summarize(outcomes),
    }


def _build_maker(settings, kind: str):
    """deterministic = free ConfluenceStrategy; claude = the real paid AnthropicDecisionMaker
    (a full backtest is a BATCH of API calls — costs real money). Returns (maker, model_name)."""
    if kind == "deterministic":
        return ConfluenceStrategy(), "deterministic-confluence"
    if kind == "claude":
        if not settings.anthropic_api_key:
            raise SystemExit("--maker claude needs BRAIN_ANTHROPIC_API_KEY (real, paid API calls)")
        from decision.llm_client import AnthropicDecisionMaker
        maker = AnthropicDecisionMaker(settings.anthropic_api_key, settings.decision_model,
                                       max_tokens=settings.decision_max_tokens)
        return maker, settings.decision_model
    raise SystemExit(f"unknown maker {kind!r} (expected deterministic|claude)")


async def _run(settings, *, count: int, run_id: str | None, maker_kind: str = "deterministic") -> None:
    provider = build_provider(settings)
    symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(symbol)
    tfs = settings.timeframes
    try:
        windows = {tf: await provider.get_ohlcv(provider_symbol, tf, count) for tf in tfs}
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()
    maker, model_name = _build_maker(settings, maker_kind)
    rows = await backtest_over_windows(
        windows, symbol=symbol, provider_name=settings.market_data_provider,
        decision_maker=maker, modeled_spread_pct=settings.replay_spread_pct,
        slippage_pct=settings.slippage_pct, model_name=model_name,
        shadow_config=ShadowConfig(commission_pct=settings.commission_pct,
                                   swap_pct_per_night=settings.swap_pct_per_night),
        persist_dsn=settings.db_dsn if run_id else None, run_id=run_id,
    )
    rep = report(rows)
    print(f"[backtest] provider={settings.market_data_provider} symbol={symbol} "
          f"bars_evaluated={rep['bars_evaluated']} prefiltered_out={rep['prefiltered_out']} "
          f"approved={rep['approved']} blocked_position_open={rep['blocked_position_open']} "
          f"trades_opened={rep['trades_opened']}" + (f" persisted run_id={run_id}" if run_id else ""))
    print(f"[metrics] {rep['metrics']}")


def main() -> int:
    from datetime import datetime, timezone

    from config.settings import load_settings

    parser = argparse.ArgumentParser(description="Shadow backtest over historical bars (no execution).")
    parser.add_argument("--count", type=int, default=1500, help="bars per timeframe to fetch")
    parser.add_argument("--persist", action="store_true", help="write shadow trades to the DB")
    parser.add_argument("--run-id", help="experiment id for persisted trades (default: timestamped)")
    parser.add_argument("--maker", choices=["deterministic", "claude"], default="deterministic",
                        help="decision maker: deterministic (free) or claude (paid API batch)")
    args = parser.parse_args()
    run_id = None
    if args.persist:
        run_id = args.run_id or f"backtest-{args.maker}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    asyncio.run(_run(load_settings(), count=args.count, run_id=run_id, maker_kind=args.maker))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
