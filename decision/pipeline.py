"""Decision pipeline: bind evaluation -> prefilter -> (LLM decide) -> Risk Engine.

Pure orchestration, NO execution and NO persistence here. It composes the deterministic
gates around the injected DecisionMaker and returns an auditable DecisionRecord.

STRICT binding (fail-closed): the eligibility verdict used to authorize a decision MUST
- have been evaluated in the SAME mode as the decision (an eligible REPLAY verdict may not
  authorize an ONLINE decision), and
- belong to the EXACT bar being decided (eligibility.as_of == packet.bar_close).
A mismatch raises DecisionBindingError before any LLM call.

The real Anthropic-backed DecisionMaker is decision/llm_client.py; tests inject a fake.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from data_collector.session import XauUsdCalendar
from decision.prefilter import PrefilterConfig, PrefilterResult, prefilter
from decision.schema import (
    DECISION_PROMPT_VERSION,
    DECISION_SCHEMA_VERSION,
    STRATEGY_VERSION,
    DecisionInput,
    DecisionMaker,
    DecisionOutput,
    NewsContext,
    build_decision_input,
)
from risk.engine import RiskConfig, RiskVerdict, evaluate_risk


class DecisionBindingError(RuntimeError):
    """The eligibility verdict does not match the decision's mode / bar. Fail-closed."""


def _manifest(inp: DecisionInput, prefilter_cfg: PrefilterConfig, risk_cfg: RiskConfig,
              eligibility) -> dict:
    return {
        "prompt_version": DECISION_PROMPT_VERSION,
        "output_schema_version": DECISION_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "feature_pipeline_version": inp.feature_pipeline_version,
        "prefilter_config_version": prefilter_cfg.version,
        "risk_config_version": risk_cfg.version,
        "eligibility_policy_version": eligibility.policy_version,
        "eligibility_mode": eligibility.mode,
        "input_hash": inp.input_hash(),
    }


class DecisionRecord(BaseModel):
    stage: Literal["prefiltered_out", "decided", "llm_failed"]
    symbol: str
    as_of: object
    mode: str
    prefilter: PrefilterResult
    decision: DecisionOutput | None = None
    risk: RiskVerdict | None = None
    input_hash: str
    manifest: dict
    llm_error: str | None = None   # set when the LLM call failed (stage == "llm_failed")

    @property
    def risk_approved(self) -> bool:
        """Risk checks passed -> SHADOW-ELIGIBLE. NOT execution-ready (see RiskVerdict)."""
        return self.stage == "decided" and self.risk is not None and self.risk.approved


def _bind_evaluation(eligibility, packet, mode: str) -> None:
    if eligibility.mode != mode:
        raise DecisionBindingError(
            f"evaluation mode {eligibility.mode!r} != decision mode {mode!r}")
    if eligibility.as_of != packet.bar_close:
        raise DecisionBindingError(
            f"evaluation as_of {eligibility.as_of} != bar_close {packet.bar_close}")


async def run_decision(
    packet,
    eligibility,
    decision_maker: DecisionMaker,
    *,
    mode: str,
    prefilter_config: PrefilterConfig,
    risk_config: RiskConfig,
    calendar: XauUsdCalendar,   # REQUIRED: the PROVIDER's calendar. No default -> no silent
                                # fallback to Polygon (which would open the session gate when
                                # XTB is closed, e.g. Sunday 21:00-22:00 UTC).
    news: NewsContext | None = None,
    feedback=None,
) -> DecisionRecord:
    # Fail-closed binding: right mode, right bar. Raises before any LLM call.
    _bind_evaluation(eligibility, packet, mode)

    inp = build_decision_input(packet, mode=mode, news=news, feedback=feedback)
    manifest = _manifest(inp, prefilter_config, risk_config, eligibility)
    pf = prefilter(packet, eligibility, prefilter_config)

    # Gate the LLM: no call on an ineligible / low-value bar.
    if not pf.passed:
        return DecisionRecord(
            stage="prefiltered_out", symbol=inp.symbol, as_of=inp.as_of, mode=mode,
            prefilter=pf, decision=None, risk=None, input_hash=inp.input_hash(), manifest=manifest,
        )

    try:
        decision = await decision_maker.decide(inp)   # THE LLM (injected)
    except Exception as exc:  # noqa: BLE001 — a failed LLM call is auditable, not a crash
        return DecisionRecord(
            stage="llm_failed", symbol=inp.symbol, as_of=inp.as_of, mode=mode,
            prefilter=pf, decision=None, risk=None, input_hash=inp.input_hash(),
            manifest=manifest, llm_error=type(exc).__name__,
        )

    risk = evaluate_risk(decision, packet, risk_config,
                         spread_provenance=inp.spread_provenance, calendar=calendar)
    return DecisionRecord(
        stage="decided", symbol=inp.symbol, as_of=inp.as_of, mode=mode,
        prefilter=pf, decision=decision, risk=risk, input_hash=inp.input_hash(), manifest=manifest,
    )
