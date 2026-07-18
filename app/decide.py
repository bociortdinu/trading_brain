"""Run ONE decision through the pipeline and persist it. NO order execution.

Flow: fetch closed OHLCV -> packet -> persist snapshot (immutable) -> persist eligibility
(append-only) -> prefilter -> decide (LLM or deterministic) -> Risk Engine -> persist a
`decisions` row FK'd to the exact authorizing evaluation. The verdict comes straight from
the Risk Engine — an approved verdict is never fabricated.

    python -m app.decide --replay --fake   # deterministic maker (no API); shadow
    python -m app.decide --replay          # real Anthropic maker (needs BRAIN_ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from app.collect import (
    build_packet_from_windows,
    compute_eligibility,
    fetch_windows,
    m15_closes,
    observe_xtb_spread,
)
from config.settings import load_settings
from core.models import Direction
from data_collector.providers.factory import build_provider
from data_collector.session import calendar_for
from database.repository import (
    insert_decision,
    insert_evaluation,
    insert_llm_call,
    insert_spread_observation,
    upsert_snapshot,
)
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig
from decision.schema import DecisionInput, DecisionOutput, build_decision_input
from risk.engine import RiskConfig
from features.mtf import TRIGGER_TF


class DeterministicMaker:
    """Fake DecisionMaker for demos/tests: always proposes a BUY. Used to exercise the
    persistence path WITHOUT a real API call; the Risk Engine still decides the verdict."""
    async def decide(self, inp: DecisionInput) -> DecisionOutput:
        return DecisionOutput(direction=Direction.BUY, confidence=0.7,
                              rationale="deterministic stub (no live LLM)")


async def _run(settings, *, mode: str, use_fake: bool, all_regimes: bool = False) -> None:
    provider = build_provider(settings)
    provider_name = settings.market_data_provider
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)
    now = datetime.now(timezone.utc)
    try:
        windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()

    closes = m15_closes(windows)
    if not closes:
        raise SystemExit(f"no closed {TRIGGER_TF} bars for {provider_symbol}")
    as_of = closes[-1]
    packet = build_packet_from_windows(
        windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
        provider_symbol=provider_symbol, ingested_at=now,
    )
    quote_time = observed_at = None
    basis = None
    # A live quote describes NOW — only ONLINE may attach one. Replay never observes.
    if mode == "online":
        spread_pct, basis = await observe_xtb_spread(settings, packet.price, packet.bar_close)
        if basis is not None:
            packet = packet.model_copy(update={"spread_pct": spread_pct, "basis_observed": basis})
            if basis.get("quote_time"):
                quote_time = datetime.fromisoformat(basis["quote_time"])
            if basis.get("observed_at"):
                observed_at = datetime.fromisoformat(basis["observed_at"])

    # Recapture the clock AFTER the quote (anti future-quote; see app/collect).
    now = datetime.now(timezone.utc)
    result = compute_eligibility(windows, as_of, settings, mode=mode, now=now,
                                 quote_time=quote_time, provider_name=provider_name)
    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    print(f"[db] {status} snapshot id={snap_id}  bar_close={packet.bar_close.isoformat()}")
    if snap_id is None or status == "conflict":
        raise SystemExit(f"snapshot not usable for a decision (status={status})")
    # The spread is a separate, append-only fact ABOUT the snapshot (never part of it).
    spread_obs_id = None
    if basis is not None and packet.spread_pct is not None:
        spread_obs_id = insert_spread_observation(
            settings.db_dsn, snapshot_id=snap_id, spread_pct=packet.spread_pct,
            provenance="observed_xtb", observed_at=observed_at or now,
            quote_time=quote_time, basis=basis,
        )
    eval_id = insert_evaluation(settings.db_dsn, snap_id, result)
    print(f"[db] evaluation id={eval_id} mode={result.mode} eligible={result.eligible} reasons={result.reasons}")

    if use_fake:
        maker, model_name = DeterministicMaker(), "deterministic-fake"
    else:
        if not settings.anthropic_api_key:
            raise SystemExit("BRAIN_ANTHROPIC_API_KEY is required for the real maker (or use --fake)")
        from decision.llm_client import AnthropicDecisionMaker
        maker = AnthropicDecisionMaker(settings.anthropic_api_key, settings.decision_model,
                                       max_tokens=settings.decision_max_tokens)
        model_name = settings.decision_model

    # Shadow experimentation may record decisions across ALL regimes (to measure the model);
    # --shadow-all-regimes lifts only the regime skip, keeping every other gate intact.
    pf_config = PrefilterConfig(blocked_regimes=[]) if all_regimes else PrefilterConfig()
    record = await run_decision(packet, result, maker, mode=mode,
                                prefilter_config=pf_config, risk_config=RiskConfig(),
                                calendar=calendar_for(provider_name))
    inp = build_decision_input(packet, mode=mode)
    print(f"[decision] stage={record.stage} "
          f"{'prefilter='+str(record.prefilter.reasons) if record.stage=='prefiltered_out' else ''}")
    if record.risk is not None:
        print(f"[risk] approved={record.risk.approved} reason={record.risk.reason} "
              f"sl={record.risk.sl_pct} tp={record.risk.tp_pct} "
              f"execution_ready={record.risk.execution_ready} pending={record.risk.pending_execution_gates}")

    # Audit-log the LLM call (success OR failure) with its full manifest + cost.
    last = getattr(maker, "last_result", None)
    if record.stage == "llm_failed":
        if last is not None:   # a failed call yielded no decision -> log it unlinked
            insert_llm_call(settings.db_dsn, last, snapshot_id=snap_id)
        print(f"[llm] FAILED: {record.llm_error} (logged to llm_calls; no decision persisted)")
        return

    tokens = {}
    if last is not None:
        tokens = {"input": last.input_tokens, "output": last.output_tokens,
                  "latency_ms": last.latency_ms,
                  "cache_hit": bool(last.cache_read_input_tokens)}
    ai_output = record.decision.model_dump(mode="json") if record.decision else None
    dec_id, _ = insert_decision(   # decision + paid-call audit in ONE transaction (atomic)
        settings.db_dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name,
        record=record, ai_input=inp.model_dump(mode="json"), ai_output=ai_output,
        mode="shadow", data_provider=provider_name, tokens=tokens,
        spread_observation_id=spread_obs_id, llm_result=last,
    )
    verdict = "approved" if (record.risk and record.risk.approved) else "rejected"
    print(f"[db] decision id={dec_id} verdict={verdict} -> evaluation_id={eval_id} (shadow, NOT executed)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run + persist one shadow decision (no execution).")
    parser.add_argument("--replay", action="store_true", help="market mode replay (freshness vs as_of)")
    parser.add_argument("--fake", action="store_true", help="deterministic maker (no API call)")
    parser.add_argument("--shadow-all-regimes", action="store_true",
                        help="shadow: do not skip on regime (record all regimes)")
    args = parser.parse_args()
    settings = load_settings()
    mode = "replay" if args.replay else settings.market_mode
    asyncio.run(_run(settings, mode=mode, use_fake=args.fake, all_regimes=args.shadow_all_regimes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
