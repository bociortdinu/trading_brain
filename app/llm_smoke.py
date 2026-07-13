"""ONE real Anthropic API smoke-test (NO order execution). Sends a representative, frozen
DecisionInput through the fail-closed client and reports the manifest. Never prints the API
key or the prompt. Requires BRAIN_ANTHROPIC_API_KEY; without it, prints SKIPPED and exits 0.

    python -m app.llm_smoke                 # baseline model (settings.decision_model)
    python -m app.llm_smoke --benchmark     # benchmark model (settings.benchmark_model), same input
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from config.settings import load_settings
from decision.schema import DecisionInput, NewsContext, TimeframeView

UTC = timezone.utc

# A frozen, representative snapshot (so baseline vs benchmark run on identical input).
FROZEN_INPUT = DecisionInput(
    symbol="GOLD",
    as_of=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    mode="replay",
    price=4108.18,
    regime_h1="bull_trend",
    macro_bias="up",
    major_trend="bull_trend",
    confluence="aligned_bull",
    spread_pct=0.03,
    spread_provenance="modeled",
    timeframes={
        "1day": TimeframeView(regime="bull_trend", ema_align="up", adx=28.4, rsi=61.0,
                              atr_pct=0.9, ema50_slope_pct=0.12),
        "4h": TimeframeView(regime="bull_trend", ema_align="up", adx=31.0, rsi=59.5,
                            atr_pct=0.5, ema50_slope_pct=0.08),
        "1h": TimeframeView(regime="bull_trend", ema_align="up", adx=26.7, rsi=57.2,
                            atr_pct=0.3, ema50_slope_pct=0.05,
                            nearest_support_pct=0.4, nearest_resistance_pct=0.6),
        "15min": TimeframeView(regime="range", ema_align="mixed", adx=18.0, rsi=52.0,
                               atr_pct=0.12, ema50_slope_pct=0.01),
    },
    news=NewsContext(status="unavailable"),
    feature_pipeline_version="1.2.0",
)


async def _run(model: str, api_key: str) -> int:
    from decision.llm_client import AnthropicDecisionMaker

    maker = AnthropicDecisionMaker(api_key, model, max_tokens=1024)
    try:
        res = await maker.call(FROZEN_INPUT)
    finally:
        await maker.aclose()

    print("=== Anthropic API smoke-test (no execution) ===")
    print(f"payload input_hash : {res.input_hash}")
    print(f"requested_model    : {res.requested_model}")
    print(f"effective_model    : {res.effective_model}")
    print(f"request_id         : {res.request_id}")
    print(f"stop_reason        : {res.stop_reason}")
    print(f"ok / error         : {res.ok} / {res.error}")
    if res.output is not None:
        print(f"validated_output   : direction={res.output.direction.value} "
              f"confidence={res.output.confidence} key_factors={res.output.key_factors}")
        print(f"rationale          : {res.output.rationale[:200]}")
    print(f"tokens in/out      : {res.input_tokens}/{res.output_tokens}")
    print(f"cache read/create  : {res.cache_read_input_tokens}/{res.cache_creation_input_tokens}")
    print(f"latency_ms         : {res.latency_ms}")
    print(f"estimated_cost_usd : {res.estimated_cost_usd}")
    return 0 if res.ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="One Anthropic API smoke-test (no execution).")
    parser.add_argument("--benchmark", action="store_true", help="use the benchmark model")
    args = parser.parse_args()
    settings = load_settings()
    if not settings.anthropic_api_key:
        print("SKIPPED: BRAIN_ANTHROPIC_API_KEY not set (no real API call made).")
        return 0
    model = settings.benchmark_model if args.benchmark else settings.decision_model
    return asyncio.run(_run(model, settings.anthropic_api_key))


if __name__ == "__main__":
    raise SystemExit(main())
