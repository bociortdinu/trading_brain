"""Decision layer: strict output schema, evaluation binding, deterministic prefilter,
rigid Risk Engine (mandatory spread, session, no clamp), and pipeline composition.
No execution, no real LLM (a deterministic fake is injected)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.models import Direction
from data_collector.session import DEFAULT_CALENDAR, calendar_for
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


# Sunday 2026-07-12 21:30 UTC = 17:30 ET: Polygon (opens 17:00 ET) is OPEN, XTB (opens 18:00
# ET) is CLOSED. This is the exact window the reviewer flagged: the Risk Engine must use the
# PROVIDER's calendar, or it will authorize an entry when XTB is shut.
_XTB_CLOSED_POLYGON_OPEN = datetime(2026, 7, 12, 21, 30, tzinfo=UTC)


def test_risk_calendar_is_provider_specific_xtb_vs_polygon():
    packet = _packet(bar=_XTB_CLOSED_POLYGON_OPEN, atr=0.2, spread=0.05)
    poly = evaluate_risk(_out(Direction.BUY, 0.9), packet, RiskConfig(),
                         calendar=calendar_for("polygon"))
    xtb = evaluate_risk(_out(Direction.BUY, 0.9), packet, RiskConfig(),
                        calendar=calendar_for("xtb"))
    assert poly.session_open is True and poly.approved            # Polygon: open
    assert xtb.session_open is False and xtb.reason == "market_closed"   # XTB: shut


def test_pipeline_threads_provider_calendar_not_polygon_default():
    """Regression: run_decision must reject on the XTB calendar at a Sunday-gap bar that the
    Polygon calendar would approve. Guards against the silent DEFAULT_CALENDAR fallback."""
    packet = _packet(bar=_XTB_CLOSED_POLYGON_OPEN, atr=0.2, spread=0.05)
    elig = _elig(True, as_of=_XTB_CLOSED_POLYGON_OPEN)
    approved = _pipe(packet, elig, _FakeLLM(_out(Direction.BUY, 0.9)),
                     calendar=calendar_for("polygon"))
    rejected = _pipe(packet, elig, _FakeLLM(_out(Direction.BUY, 0.9)),
                     calendar=calendar_for("xtb"))
    assert approved.risk_approved is True
    assert rejected.risk_approved is False and rejected.risk.reason == "market_closed"


def test_risk_rejects_out_of_bounds_sl_never_clamps():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=5.0, spread=0.05), RiskConfig())
    assert not v.approved and "sl_out_of_bounds" in v.reason and v.sl_pct is None


def test_risk_rejects_spread_wider_than_stop_fraction():
    v = evaluate_risk(_out(Direction.BUY, 0.9), _packet(atr=0.2, spread=0.2), RiskConfig())
    assert not v.approved and "spread_vs_sl" in v.reason


# --------------------------------------------------------------------------- #
# pipeline composition + evaluation binding (no execution)
# --------------------------------------------------------------------------- #
def _pipe(packet, elig, llm, mode="replay", calendar=DEFAULT_CALENDAR):
    return run(run_decision(packet, elig, llm, mode=mode,
                            prefilter_config=PrefilterConfig(), risk_config=RiskConfig(),
                            calendar=calendar))


# ---- scheduled-release blackout ---- #
def _calendar_at(when, impact="high"):
    from data_collector.news.economic_calendar import EconomicCalendar, ScheduledEvent

    return EconomicCalendar([ScheduledEvent(label="CPI", release_name="Consumer Price Index",
                                            scheduled_at=when, impact=impact)])


def _pipe_cal(packet, elig, llm, econ_calendar=None, econ_calendar_config=None):
    return run(run_decision(packet, elig, llm, mode="replay",
                            prefilter_config=PrefilterConfig(), risk_config=RiskConfig(),
                            calendar=DEFAULT_CALENDAR, econ_calendar=econ_calendar,
                            econ_calendar_config=econ_calendar_config))


def test_blackout_stops_the_bar_before_the_paid_model():
    """The money claim: a bar inside a high-impact release window must not reach the maker.
    _ExplodingLLM raises if called, so this fails loudly rather than silently spending."""
    rec = _pipe_cal(_packet(spread=0.05), _elig(True), _ExplodingLLM(),
                    econ_calendar=_calendar_at(_BAR))
    assert rec.stage == "prefiltered_out"
    assert any(r.startswith("news_blackout:CPI@") for r in rec.prefilter.reasons)


def test_blackout_reason_names_the_event():
    """An unexplained skip is not auditable — the reason must identify which release caused it."""
    rec = _pipe_cal(_packet(spread=0.05), _elig(True), _ExplodingLLM(),
                    econ_calendar=_calendar_at(_BAR))
    assert _BAR.isoformat() in next(r for r in rec.prefilter.reasons if "news_blackout" in r)


def test_bar_outside_the_window_still_reaches_the_model():
    from datetime import timedelta

    llm = _FakeLLM(_out(Direction.BUY, 0.9))
    rec = _pipe_cal(_packet(spread=0.05), _elig(True), llm,
                    econ_calendar=_calendar_at(_BAR + timedelta(hours=6)))
    assert llm.called == 1 and rec.stage == "decided"


def test_medium_impact_release_does_not_blackout_by_default():
    llm = _FakeLLM(_out(Direction.BUY, 0.9))
    rec = _pipe_cal(_packet(spread=0.05), _elig(True), llm,
                    econ_calendar=_calendar_at(_BAR, impact="medium"))
    assert llm.called == 1 and rec.stage == "decided"


def test_upcoming_release_is_passed_to_the_model_as_news():
    """Outside the blackout the event is still decision-relevant context, not a skip."""
    from datetime import timedelta

    from data_collector.news.economic_calendar import CalendarConfig

    captured = {}

    class _Capture:
        async def decide(self, inp):
            captured["news"] = inp.news
            return _out(Direction.BUY, 0.9)

    _pipe_cal(_packet(spread=0.05), _elig(True), _Capture(),
              econ_calendar=_calendar_at(_BAR + timedelta(minutes=90)),
              econ_calendar_config=CalendarConfig(context_lookahead_minutes=240))
    assert captured["news"].status == "ok"
    assert captured["news"].items == [{"event": "CPI", "impact": "high", "in_minutes": 90}]


def test_no_calendar_leaves_news_unavailable_and_never_blacks_out():
    """Fail-OPEN on a missing calendar, but the model is told 'unavailable' — never 'ok, no
    events', which would assert knowledge we do not have."""
    llm = _FakeLLM(_out(Direction.BUY, 0.9))
    captured = {}

    class _Capture:
        async def decide(self, inp):
            captured["news"] = inp.news
            return _out(Direction.BUY, 0.9)

    rec = _pipe_cal(_packet(spread=0.05), _elig(True), _Capture(), econ_calendar=None)
    assert rec.stage == "decided"
    assert captured["news"].status == "unavailable"


def test_explicit_news_wins_over_the_calendar():
    captured = {}

    class _Capture:
        async def decide(self, inp):
            captured["news"] = inp.news
            return _out(Direction.BUY, 0.9)

    from datetime import timedelta

    run(run_decision(_packet(spread=0.05), _elig(True), _Capture(), mode="replay",
                     prefilter_config=PrefilterConfig(), risk_config=RiskConfig(),
                     calendar=DEFAULT_CALENDAR, news=NewsContext(status="unavailable"),
                     econ_calendar=_calendar_at(_BAR + timedelta(minutes=90))))
    assert captured["news"].status == "unavailable"


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


def test_execution_config_is_part_of_the_decision_fingerprint():
    """A crash-recovery rebuilds a trade from the persisted decision using the CURRENT execution
    config. Folding that config into the fingerprint makes a config change a DIFFERENT decision,
    so recovery can only ever reuse a decision produced under the IDENTICAL config."""
    from decision.schema import decision_fingerprint
    from shadow.virtual_broker import ShadowConfig, execution_hash, execution_manifest

    base = dict(input_hash="h", model="m", provider="csv", risk_config_version="v")
    common = dict(single_position=True, cooldown_bars=0,
                  risk_config={"version": "v"}, prefilter_config={"version": "pf"})
    h_a = execution_hash(execution_manifest(modeled_spread_pct=0.02, slippage_pct=0.005,
                                            config=ShadowConfig(), **common))
    h_b = execution_hash(execution_manifest(modeled_spread_pct=0.02, slippage_pct=0.010,   # diff slip
                                            config=ShadowConfig(), **common))
    h_c = execution_hash(execution_manifest(modeled_spread_pct=0.02, slippage_pct=0.005,
                                            config=ShadowConfig(commission_pct=0.01), **common))  # diff comm
    assert len({h_a, h_b, h_c}) == 3                                   # every knob moves the hash
    fp = lambda h: decision_fingerprint(**base, execution_hash=h)      # noqa: E731
    assert len({fp(h_a), fp(h_b), fp(h_c)}) == 3                       # -> three distinct decisions
    assert fp(h_a) == fp(h_a)                                          # deterministic


# ---- model-proposed SL/TP (validated, never trusted) ---- #
def _out_sl(direction=Direction.BUY, conf=0.8, sl=None, tp=None):
    return DecisionOutput(direction=direction, confidence=conf, rationale="ok",
                          proposed_sl_pct=sl, proposed_tp_pct=tp)


def test_atr_policy_ignores_a_proposal_entirely():
    """Default policy must be unaffected by anything the model proposes — otherwise every past
    run's sizing would silently change."""
    from risk.engine import compute_sl_tp, resolve_sl_tp

    cfg = RiskConfig()                                   # sl_tp_source="atr"
    sl, tp, source = resolve_sl_tp(_out_sl(sl=1.0, tp=9.0), 0.2, cfg)
    assert (sl, tp) == compute_sl_tp(0.2, cfg) and source == "atr"


def test_model_policy_uses_a_sound_proposal():
    from risk.engine import resolve_sl_tp

    cfg = RiskConfig(sl_tp_source="model")
    sl, tp, source = resolve_sl_tp(_out_sl(sl=0.4, tp=1.2), 0.2, cfg)
    assert (sl, tp, source) == (0.4, 1.2, "model")


def test_a_wide_stop_with_a_near_target_is_discarded():
    """The failure mode self-sizing invites: a huge stop and a close target flatter the win rate
    while being a poor strategy. It must fall back, not be traded."""
    from risk.engine import compute_sl_tp, resolve_sl_tp

    cfg = RiskConfig(sl_tp_source="model")
    sl, tp, source = resolve_sl_tp(_out_sl(sl=2.0, tp=0.3), 0.2, cfg)
    assert (sl, tp) == compute_sl_tp(0.2, cfg)
    assert source == "atr_fallback:rr_too_low"


def test_missing_proposal_falls_back_and_says_so():
    from risk.engine import compute_sl_tp, resolve_sl_tp

    cfg = RiskConfig(sl_tp_source="model")
    sl, tp, source = resolve_sl_tp(_out_sl(), 0.2, cfg)
    assert (sl, tp) == compute_sl_tp(0.2, cfg)
    assert source == "atr_fallback:no_proposal"


def test_a_proposed_stop_outside_the_bounds_is_still_rejected():
    """A proposal gets no easier a path than a computed value: the same min/max SL band applies."""
    packet = _packet(atr=0.2, spread=0.02)
    v = evaluate_risk(_out_sl(sl=5.0, tp=15.0), packet, RiskConfig(sl_tp_source="model"))
    assert not v.approved and "sl_out_of_bounds" in v.reason


def test_a_proposed_stop_the_spread_would_eat_is_rejected():
    packet = _packet(atr=0.2, spread=0.05)
    v = evaluate_risk(_out_sl(sl=0.06, tp=0.20), packet, RiskConfig(sl_tp_source="model"))
    assert not v.approved and "spread_vs_sl" in v.reason


def test_verdict_records_where_the_sizing_came_from():
    """A track record must be able to separate model-sized from ATR-sized trades rather than
    averaging two different policies together."""
    packet = _packet(atr=0.2, spread=0.02)
    model = evaluate_risk(_out_sl(sl=0.4, tp=1.2), packet, RiskConfig(sl_tp_source="model"))
    atr = evaluate_risk(_out_sl(sl=0.4, tp=1.2), packet, RiskConfig())
    assert model.approved and model.sl_tp_source == "model" and model.sl_pct == 0.4
    assert atr.approved and atr.sl_tp_source == "atr"


def test_schema_still_refuses_smuggled_fields():
    """SL/TP are now proposable, but the output contract stays closed otherwise."""
    with pytest.raises(ValidationError):
        DecisionOutput(direction=Direction.BUY, confidence=0.8, rationale="x", volume=1.0)


def test_a_negative_proposed_stop_is_rejected_by_the_schema():
    with pytest.raises(ValidationError):
        DecisionOutput(direction=Direction.BUY, confidence=0.8, rationale="x",
                       proposed_sl_pct=-0.5, proposed_tp_pct=1.0)
