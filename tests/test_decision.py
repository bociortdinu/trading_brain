"""Decision layer: strict output schema, evaluation binding, deterministic prefilter,
rigid Risk Engine (mandatory spread, session, no clamp), and pipeline composition.
No execution, no real LLM (a deterministic fake is injected)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.models import Direction
from decision.pipeline import DecisionBindingError, run_decision
from decision.prefilter import PrefilterConfig, prefilter
from decision.schema import DecisionInput, DecisionOutput, NewsContext, build_decision_input
from features.eligibility import EligibilityResult
from features.mtf import FeaturePacket
from risk.engine import RiskConfig, compute_sl_tp, evaluate_risk
from tests.helpers import run

UTC = timezone.utc
_BAR = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)   # Fri 16:00 EDT -> market OPEN
_CLOSED_BAR = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)  # Saturday -> market CLOSED
_OK_DQ = {tf: {"verdict": "ok"} for tf in ("1day", "4h", "1h", "15min")}


def _packet(*, bar=_BAR, regime="bull_trend", atr=0.2, spread=None, dq=None,
            macro="up", major="bull_trend", confluence="aligned_bull"):
    return FeaturePacket(
        symbol="GOLD", bar_close=bar, price=4100.0,
        spread_pct=spread, regime=regime, adx_h1=30.0, atr_pct_m15=atr,
        macro_bias=macro, major_trend=major, confluence=confluence,
        timeframes={"1day": {"rsi": 55.0}, "4h": {}, "1h": {}, "15min": {"atr_pct": atr}},
        data_quality=dq or _OK_DQ, provider="csv", provider_symbol="C:XAUUSD",
        ingested_at=datetime.now(UTC), interval_list=["1day", "4h", "1h", "15min"],
    )


def _elig(eligible=True, reasons=None, mode="replay", as_of=_BAR):
    return EligibilityResult(
        eligible=eligible, reasons=reasons or [], mode=mode, as_of=as_of,
        policy_version="elig-2026.1+x", policy={}, evaluated_at=datetime.now(UTC),
    )


def _out(direction=Direction.BUY, confidence=0.8, rationale="ok"):
    return DecisionOutput(direction=direction, confidence=confidence, rationale=rationale)


class _FakeLLM:
    def __init__(self, out):
        self.out, self.called = out, 0

    async def decide(self, inp):
        self.called += 1
        return self.out


class _ExplodingLLM:
    async def decide(self, inp):
        raise AssertionError("LLM must not be called")


# --------------------------------------------------------------------------- #
# schema strictness
# --------------------------------------------------------------------------- #
def test_output_rejects_out_of_vocab_direction():
    with pytest.raises(ValidationError):
        DecisionOutput(direction="LONG", confidence=0.8, rationale="x")


def test_output_rejects_bad_confidence_and_empty_rationale():
    with pytest.raises(ValidationError):
        DecisionOutput(direction=Direction.BUY, confidence=1.5, rationale="x")
    with pytest.raises(ValidationError):
        DecisionOutput(direction=Direction.BUY, confidence=0.5, rationale="")


def test_output_forbids_extra_fields():
    with pytest.raises(ValidationError):
        DecisionOutput(direction=Direction.BUY, confidence=0.8, rationale="x", sl_pct=1.0)


def test_input_carries_tf_view_and_strict_mode():
    inp = build_decision_input(_packet(atr=0.2), mode="replay")
    assert inp.timeframes["1day"].rsi == 55.0 and inp.timeframes["15min"].atr_pct == 0.2
    assert inp.spread_provenance == "unavailable"   # replay, no spread
    with pytest.raises(ValidationError):
        DecisionInput(symbol="G", as_of=_BAR, mode="onlien", price=1.0, regime_h1="x",
                      macro_bias="x", major_trend="x", confluence="x", feature_pipeline_version="v")


def test_input_hash_is_deterministic_and_sensitive():
    p = _packet()
    assert build_decision_input(p, mode="replay").input_hash() == build_decision_input(p, mode="replay").input_hash()
    assert build_decision_input(p, mode="replay").input_hash() != build_decision_input(p, mode="online").input_hash()


def test_spread_provenance_online_observed():
    inp = build_decision_input(_packet(spread=0.05), mode="online")
    assert inp.spread_provenance == "observed_xtb"


def test_news_status_distinguishes_unavailable_from_empty():
    assert build_decision_input(_packet(), mode="replay").news.status == "unavailable"
    ok = build_decision_input(_packet(), mode="replay", news=NewsContext(status="ok", items=[]))
    assert ok.news.status == "ok" and ok.news.items == []   # no_relevant_news != unavailable


# --------------------------------------------------------------------------- #
# prefilter
# --------------------------------------------------------------------------- #
def test_prefilter_passes_clean_eligible_bar():
    r = prefilter(_packet(), _elig(True), PrefilterConfig())
    assert r.passed and r.reasons == []


def test_prefilter_blocks_ineligible_choppy_wide_spread_insufficient():
    dq = {**_OK_DQ, "1h": {"verdict": "insufficient"}}
    r = prefilter(_packet(regime="choppy", spread=0.5, dq=dq),
                  _elig(False, ["missing_xtb_quote"], mode="online"), PrefilterConfig())
    assert not r.passed
    assert any("ineligible" in x for x in r.reasons)
    assert any("blocked_regime" in x for x in r.reasons)
    assert any("spread_too_wide" in x for x in r.reasons)
    assert any("insufficient_data:1h" in x for x in r.reasons)


# --------------------------------------------------------------------------- #
# risk engine
# --------------------------------------------------------------------------- #
def test_compute_sl_tp_deterministic():
    assert compute_sl_tp(0.2, RiskConfig()) == (0.3, 0.6)


def test_risk_approves_valid_buy_shadow_eligible_not_execution_ready():
    v = evaluate_risk(_out(Direction.BUY, 0.8), _packet(atr=0.2, spread=0.05), RiskConfig(),
                      spread_provenance="observed_xtb")
    assert v.approved and v.sl_pct == 0.3 and v.tp_pct == 0.6
    assert v.execution_ready is False and set(v.pending_execution_gates) == {"cooldown_frequency", "existing_positions"}


def test_risk_rejects_missing_spread_for_entry():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=0.2, spread=None), RiskConfig(),
                      spread_provenance="unavailable")
    assert not v.approved and v.reason.startswith("missing_spread")


def test_risk_rejects_no_trade_low_conf_and_no_atr():
    assert evaluate_risk(_out(Direction.NO_TRADE, 0.9), _packet(spread=0.05), RiskConfig()).reason == "no_trade"
    assert not evaluate_risk(_out(Direction.BUY, 0.4), _packet(spread=0.05), RiskConfig()).approved
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=None, spread=0.05), RiskConfig())
    assert not v.approved and v.reason == "no_atr"


def test_risk_rejects_market_closed():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(bar=_CLOSED_BAR, atr=0.2, spread=0.05), RiskConfig())
    assert not v.approved and v.reason == "market_closed" and v.session_open is False


def test_risk_rejects_out_of_bounds_sl_never_clamps():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=5.0, spread=0.05), RiskConfig())
    assert not v.approved and "sl_out_of_bounds" in v.reason and v.sl_pct is None


def test_risk_rejects_spread_wider_than_stop_fraction():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=0.2, spread=0.2), RiskConfig())
    assert not v.approved and "spread_vs_sl" in v.reason


# --------------------------------------------------------------------------- #
# pipeline composition + evaluation binding (no execution)
# --------------------------------------------------------------------------- #
def _pipe(packet, elig, llm, mode="replay"):
    return run(run_decision(packet, elig, llm, mode=mode,
                            prefilter_config=PrefilterConfig(), risk_config=RiskConfig()))


def test_pipeline_binding_rejects_mode_mismatch():
    # replay-eligible evaluation must NOT authorize an online decision
    with pytest.raises(DecisionBindingError, match="mode"):
        _pipe(_packet(spread=0.05), _elig(True, mode="replay"), _ExplodingLLM(), mode="online")


def test_pipeline_binding_rejects_wrong_bar():
    other = _elig(True, mode="replay", as_of=datetime(2026, 7, 10, 19, 45, tzinfo=UTC))
    with pytest.raises(DecisionBindingError, match="as_of"):
        _pipe(_packet(spread=0.05), other, _ExplodingLLM())


def test_pipeline_skips_llm_when_prefiltered_out():
    rec = _pipe(_packet(regime="choppy", spread=0.05), _elig(True), _ExplodingLLM())
    assert rec.stage == "prefiltered_out" and rec.decision is None and not rec.risk_approved


def test_pipeline_decides_and_approves():
    llm = _FakeLLM(_out(Direction.BUY, 0.8))
    rec = _pipe(_packet(atr=0.2, spread=0.05), _elig(True), llm)
    assert llm.called == 1 and rec.stage == "decided" and rec.risk_approved
    assert rec.risk.sl_pct == 0.3 and rec.risk.execution_ready is False
    assert rec.manifest["eligibility_mode"] == "replay" and rec.manifest["input_hash"] == rec.input_hash


def test_pipeline_replay_missing_spread_is_rejected_not_approved():
    llm = _FakeLLM(_out(Direction.BUY, 0.9))
    rec = _pipe(_packet(atr=0.2, spread=None), _elig(True), llm)   # replay bar, no spread
    assert rec.stage == "decided" and not rec.risk_approved and rec.risk.reason.startswith("missing_spread")


def test_pipeline_llm_failure_is_captured_not_crashed():
    class _FailingLLM:
        async def decide(self, inp):
            raise RuntimeError("boom")

    rec = _pipe(_packet(atr=0.2, spread=0.05), _elig(True), _FailingLLM())
    assert rec.stage == "llm_failed" and rec.llm_error == "RuntimeError"
    assert rec.decision is None and rec.risk is None and not rec.risk_approved
