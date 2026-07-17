"""Snapshot upsert policy (insert / unchanged / enrich / conflict). DB-gated."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from features.mtf import build_feature_packet
from tests.synthetic import trend

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_DB_DSN")


def _db_ok() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="no reachable BRAIN_DB_DSN")


def _packet(symbol: str, *, spread=None, provider="csv"):
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    tf = {
        name: trend(step=1.0, tf_min=m, start=end - timedelta(minutes=m * 250))
        for name, m in (("1day", 1440), ("4h", 240), ("1h", 60), ("15min", 15))
    }
    # spread and basis are observed together (same XTB /quote) -> set both or neither.
    basis = {"feed_price": 2000.0, "xtb_spread_pct": spread} if spread is not None else None
    return build_feature_packet(
        symbol, tf, as_of=end, provider=provider, provider_symbol="C:XAUUSD",
        ingested_at=datetime.now(timezone.utc), spread_pct=spread, basis_observed=basis,
    )


def _cleanup(symbol):
    with psycopg.connect(DSN) as c:
        snaps = "(SELECT id FROM market_snapshots WHERE symbol = %s)"
        decs = f"(SELECT id FROM decisions WHERE snapshot_id IN {snaps})"
        c.execute(f"DELETE FROM trades WHERE decision_id IN {decs}", (symbol,))
        c.execute(f"DELETE FROM llm_calls WHERE snapshot_id IN {snaps}", (symbol,))
        c.execute(f"DELETE FROM decisions WHERE snapshot_id IN {snaps}", (symbol,))
        c.execute("DELETE FROM snapshot_conflicts WHERE symbol = %s", (symbol,))
        # snapshot_evaluations cascade on snapshot delete, but be explicit for clarity.
        c.execute(f"DELETE FROM snapshot_evaluations WHERE snapshot_id IN {snaps}", (symbol,))
        c.execute("DELETE FROM market_snapshots WHERE symbol = %s", (symbol,))
        c.commit()


def _eval(mode, eligible, reasons, *, policy_version="elig-2026.1+testpolicy", quote_present=None):
    from features.eligibility import EligibilityResult

    return EligibilityResult(
        eligible=eligible, reasons=reasons, mode=mode,
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc), policy_version=policy_version,
        policy={"logic": "test"}, evaluated_at=datetime.now(timezone.utc),
        quote_present=quote_present,
    )


def test_insert_then_unchanged():
    from database.repository import upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        assert upsert_snapshot(DSN, _packet(sym, spread=0.02))[0] == "inserted"
        assert upsert_snapshot(DSN, _packet(sym, spread=0.02))[0] == "unchanged"
    finally:
        _cleanup(sym)


def test_snapshot_is_immutable_and_carries_no_spread():
    """A snapshot is an IMMUTABLE observation of a closed bar. A later quote must NEVER mutate it
    (that enrichment is what let a snapshot assert a spread its decision never used). The spread
    is not even a column any more — it lives in spread_observations."""
    from database.repository import upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        assert upsert_snapshot(DSN, _packet(sym, spread=None))[0] == "inserted"
        # A packet that now carries a live quote does NOT rewrite the stored observation.
        assert upsert_snapshot(DSN, _packet(sym, spread=0.05))[0] == "unchanged"
        with psycopg.connect(DSN) as c:
            cols = [r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'market_snapshots'").fetchall()]
        assert "spread_pct" not in cols and "basis_observed" not in cols
    finally:
        _cleanup(sym)


def test_spread_observation_is_append_only_and_never_touches_the_snapshot():
    """Contextual spreads accumulate as separate facts ABOUT one snapshot: the same bar can hold
    an online observation and a modeled one without either overwriting the other."""
    from database.repository import insert_spread_observation, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    t0 = datetime(2026, 7, 1, 11, 8, tzinfo=timezone.utc)
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=None))
        a = insert_spread_observation(DSN, snapshot_id=snap_id, spread_pct=0.0177,
                                      provenance="observed_xtb", observed_at=t0,
                                      basis={"xtb_spread_pct": 0.0177})
        b = insert_spread_observation(DSN, snapshot_id=snap_id, spread_pct=0.02,
                                      provenance="modeled", observed_at=t0)
        assert a != b   # two distinct facts about the SAME bar; neither overwrote the other
        # Idempotent: re-recording the same observation returns the same row.
        assert insert_spread_observation(DSN, snapshot_id=snap_id, spread_pct=0.0177,
                                         provenance="observed_xtb", observed_at=t0) == a
        with psycopg.connect(DSN) as c:
            n = c.execute("SELECT count(*) FROM spread_observations WHERE snapshot_id=%s",
                          (snap_id,)).fetchone()[0]
        assert n == 2
    finally:
        _cleanup(sym)


def test_different_provider_is_conflict_and_persisted():
    from database.repository import upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        assert upsert_snapshot(DSN, _packet(sym, provider="csv"))[0] == "inserted"
        status, _ = upsert_snapshot(DSN, _packet(sym, provider="polygon"))
        assert status == "conflict"  # not silently blocked, not overwritten
        with psycopg.connect(DSN) as c:
            row = c.execute(
                "SELECT existing_provider, incoming_provider FROM snapshot_conflicts WHERE symbol=%s",
                (sym,),
            ).fetchone()
        assert row == ("csv", "polygon")  # conflict is durably persisted, not just logged
    finally:
        _cleanup(sym)


def test_online_and_replay_evaluations_coexist_without_overwrite():
    from database.repository import evaluations_for, insert_evaluation, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        # SAME bar: stale online (missing quote) AND eligible replay -> two distinct rows.
        insert_evaluation(DSN, snap_id, _eval("online", False, ["missing_xtb_quote"], quote_present=False))
        insert_evaluation(DSN, snap_id, _eval("replay", True, []))
        rows = {r["mode"]: r for r in evaluations_for(DSN, snap_id)}
        assert set(rows) == {"online", "replay"}
        assert rows["online"]["eligible"] is False and rows["online"]["reasons"] == ["missing_xtb_quote"]
        assert rows["replay"]["eligible"] is True  # replay verdict NOT overwritten by online
    finally:
        _cleanup(sym)


def test_reevaluation_is_append_only_latest_wins():
    from database.repository import evaluations_for, insert_evaluation, latest_evaluation_id, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        pol = "elig-2026.1+testpolicy"
        id1 = insert_evaluation(DSN, snap_id, _eval("online", False, ["missing_xtb_quote"], quote_present=False))
        # feed/quote recovered: same (mode, policy) -> APPEND a new row (prior is preserved).
        id2 = insert_evaluation(DSN, snap_id, _eval("online", True, [], quote_present=True))
        rows = [r for r in evaluations_for(DSN, snap_id) if r["mode"] == "online"]
        assert len(rows) == 2 and id1 != id2                       # both verdicts kept (audit)
        assert latest_evaluation_id(DSN, snap_id, "online", pol) == id2  # latest is 'current'
    finally:
        _cleanup(sym)


def test_decision_references_immutable_evaluation():
    from database.repository import insert_decision, insert_evaluation, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from risk.engine import RiskVerdict

    from decision.schema import DecisionOutput

    sym = "TST_" + os.urandom(3).hex()
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=None))  # replay-style, no spread
        eval_id = insert_evaluation(DSN, snap_id, _eval("replay", True, []))
        # LLM said BUY; the Risk Engine then rejected missing_spread (post-LLM). We persist
        # the real ai_output but a REJECTED verdict — never a fabricated approval.
        decision = DecisionOutput(direction="BUY", confidence=0.9, rationale="mtf aligned")
        risk = RiskVerdict(approved=False, reason="missing_spread:provenance=unavailable",
                           direction="BUY", confidence=0.9, risk_config_version="risk-mvp-2026.2")
        rec = DecisionRecord(
            stage="decided", symbol=sym, as_of=datetime(2026, 7, 1, tzinfo=timezone.utc), mode="replay",
            prefilter=PrefilterResult(passed=True, reasons=[], config_version="pf"),
            decision=decision, risk=risk, input_hash="deadbeef",
            manifest={"prompt_version": "p", "output_schema_version": "s",
                      "feature_pipeline_version": "1.2.0", "strategy_version": "st",
                      "risk_config_version": "risk-mvp-2026.2"},
        )
        # decisions.mode is the EXECUTION mode (shadow|live), distinct from the market mode
        # (online|replay, captured via the evaluation FK + manifest). Phase 2 is shadow-only.
        dec_id = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="deterministic-fake",
                                 record=rec, ai_input={"x": 1}, ai_output=decision.model_dump(mode="json"),
                                 mode="shadow", data_provider="csv")
        with psycopg.connect(DSN) as c:
            row = c.execute("SELECT evaluation_id, risk_verdict, risk_reason FROM decisions WHERE id=%s",
                            (dec_id,)).fetchone()
        assert row[0] == eval_id and row[1] == "rejected" and row[2].startswith("missing_spread")
    finally:
        _cleanup(sym)


def test_upsert_shadow_trade_idempotent_open_then_closed():
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import insert_decision, insert_evaluation, upsert_shadow_trade, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import open_virtual_trade

    sym = "TST_" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    run_id = "test-run"
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.03))
        eval_id = insert_evaluation(DSN, snap_id, _eval("replay", True, []))
        decision = DecisionOutput(direction="BUY", confidence=0.8, rationale="x")
        risk = RiskVerdict(approved=True, reason=None, direction="BUY", confidence=0.8,
                           sl_pct=0.3, tp_pct=0.6, risk_config_version="risk-mvp-2026.2")
        rec = DecisionRecord(stage="decided", symbol=sym, as_of=end, mode="replay",
                             prefilter=PrefilterResult(passed=True, reasons=[], config_version="pf"),
                             decision=decision, risk=risk, input_hash="h",
                             manifest={"prompt_version": "p", "output_schema_version": "s",
                                       "feature_pipeline_version": "1.2.0", "strategy_version": "st",
                                       "risk_config_version": "risk-mvp-2026.2"})
        dec_id = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="fake",
                                 record=rec, ai_input={}, ai_output=decision.model_dump(mode="json"),
                                 mode="shadow", data_provider="csv")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6,
                                   spread_pct=0.03, spread_provenance="modeled", opened_at=end)
        # 1. no post-entry bars yet -> OPEN row
        id1, st1 = upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym,
                                       trade=trade, outcome=reconcile(trade, []),
                                       timeframe="15min", timeout_bars=96)
        assert st1 == "inserted"
        # 2. a TP bar arrives -> reconcile again -> CLOSE THE SAME ROW in place (no duplicate)
        bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                     open=4000, high=4030, low=3999, close=4025, volume=1.0)
        id2, st2 = upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym,
                                       trade=trade, outcome=reconcile(trade, [bar]),
                                       timeframe="15min", timeout_bars=96)
        assert id2 == id1 and st2 == "updated"
        with psycopg.connect(DSN) as c:
            n, status, prov = c.execute(
                "SELECT count(*), max(status), max(spread_provenance) FROM trades WHERE decision_id=%s",
                (dec_id,)).fetchone()
        assert n == 1 and status == "closed" and prov == "modeled"  # one row, closed in place
    finally:
        _cleanup(sym)


def test_online_and_replay_decisions_share_a_bar_without_contradiction():
    """REGRESSION (reviewer's snapshot 392): an online decision (observed spread 0.0177, quote at
    11:08 for the 11:00 bar) and a replay decision (modeled 0.02) on the SAME bar used to be
    irreconcilable — the snapshot asserted ONE spread, so the other decision's FK pointed at a
    record contradicting its own frozen input. The snapshot now asserts NO spread, and each
    decision names the observation it consumed (NULL = a modeled constant, kept in ai_input)."""
    from database.repository import insert_spread_observation, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    bar_close = datetime(2026, 7, 1, tzinfo=timezone.utc)
    quote_at = bar_close + timedelta(minutes=8)
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=None))
        obs_id = insert_spread_observation(DSN, snapshot_id=snap_id, spread_pct=0.0177,
                                           provenance="observed_xtb", observed_at=quote_at)
        online_dec = _seed_decision(sym, input_hash="online", model="m", snapshot_id=snap_id,
                                    spread_pct=0.0177, provenance="observed_xtb",
                                    spread_observation_id=obs_id)
        replay_dec = _seed_decision(sym, input_hash="replay", model="m", snapshot_id=snap_id,
                                    spread_pct=0.02, provenance="modeled",
                                    spread_observation_id=None)
        with psycopg.connect(DSN) as c:
            rows = c.execute(
                "SELECT id, ai_input->>'spread_pct', ai_input->>'spread_provenance', "
                "spread_observation_id FROM decisions WHERE id = ANY(%s) ORDER BY id",
                ([online_dec, replay_dec],),
            ).fetchall()
        by_id = {r[0]: r for r in rows}
        # Each decision reproduces its OWN frozen input; neither is contradicted by the snapshot.
        assert by_id[online_dec][1] == "0.0177" and by_id[online_dec][3] == obs_id
        assert by_id[replay_dec][1] == "0.02" and by_id[replay_dec][3] is None
    finally:
        _cleanup(sym)


def _seed_decision(sym, *, input_hash="h", model="fake", end=None, snapshot_id=None,
                   spread_pct=None, provenance=None, spread_observation_id=None):
    """Snapshot -> evaluation -> decision; returns (dec_id). Shared by the idempotency tests."""
    from database.repository import insert_decision, insert_evaluation, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict

    end = end or datetime(2026, 7, 1, tzinfo=timezone.utc)
    if snapshot_id is None:
        _, snapshot_id = upsert_snapshot(DSN, _packet(sym, spread=0.03))
    eval_id = insert_evaluation(DSN, snapshot_id, _eval("replay", True, []))
    decision = DecisionOutput(direction="BUY", confidence=0.8, rationale="x")
    risk = RiskVerdict(approved=True, reason=None, direction="BUY", confidence=0.8,
                       sl_pct=0.3, tp_pct=0.6, risk_config_version="risk-mvp-2026.2")
    rec = DecisionRecord(stage="decided", symbol=sym, as_of=end, mode="replay",
                         prefilter=PrefilterResult(passed=True, reasons=[], config_version="pf"),
                         decision=decision, risk=risk, input_hash=input_hash,
                         manifest={"prompt_version": "p", "output_schema_version": "s",
                                   "feature_pipeline_version": "1.2.0", "strategy_version": "st",
                                   "risk_config_version": "risk-mvp-2026.2"})
    ai_input = {}
    if spread_pct is not None:
        ai_input = {"spread_pct": spread_pct, "spread_provenance": provenance}
    return insert_decision(DSN, snapshot_id=snapshot_id, evaluation_id=eval_id, model=model,
                           record=rec, ai_input=ai_input,
                           ai_output=decision.model_dump(mode="json"),
                           mode="shadow", data_provider="csv",
                           spread_observation_id=spread_observation_id)


def test_upsert_shadow_trade_never_reopens_a_closed_trade():
    """MONOTONE: once a shadow trade is closed it is terminal. A later upsert carrying an OPEN
    outcome (e.g. a stray reprocess) must NOT reopen or re-score it."""
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import upsert_shadow_trade
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import open_virtual_trade

    sym = "TST_" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    try:
        dec_id = _seed_decision(sym)
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6,
                                   spread_pct=0.03, spread_provenance="modeled", opened_at=end)
        tp_bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        id1, st1 = upsert_shadow_trade(DSN, decision_id=dec_id, run_id="r", symbol=sym,
                                       trade=trade, outcome=reconcile(trade, [tp_bar]),
                                       timeframe="15min", timeout_bars=96)
        assert st1 == "inserted"
        # Try to overwrite the CLOSED row with an OPEN outcome -> refused (unchanged).
        id2, st2 = upsert_shadow_trade(DSN, decision_id=dec_id, run_id="r", symbol=sym,
                                       trade=trade, outcome=reconcile(trade, []),
                                       timeframe="15min", timeout_bars=96)
        assert id2 == id1 and st2 == "unchanged"
        with psycopg.connect(DSN) as c:
            status = c.execute("SELECT status FROM trades WHERE id=%s", (id1,)).fetchone()[0]
        assert status == "closed"   # stayed closed; never reopened
    finally:
        _cleanup(sym)


def test_find_shadow_trade_by_input_dedupes_across_reruns():
    """END-TO-END IDEMPOTENCY: two runs produce DIFFERENT decision_ids for the SAME frozen
    input; the (input_hash, model, run_id) lookup still finds the existing trade so a re-run
    can skip it instead of duplicating the chain."""
    from core.models import Direction
    from database.repository import find_shadow_trade_by_input, upsert_shadow_trade
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import open_virtual_trade

    sym = "TST_" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    try:
        assert find_shadow_trade_by_input(DSN, input_hash="hh", model="m", run_id="r") is None
        dec_id = _seed_decision(sym, input_hash="hh", model="m")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6,
                                   spread_pct=0.03, spread_provenance="modeled", opened_at=end)
        tid, _ = upsert_shadow_trade(DSN, decision_id=dec_id, run_id="r", symbol=sym,
                                     trade=trade, outcome=reconcile(trade, []),
                                     timeframe="15min", timeout_bars=96)
        # A SECOND run mints a new decision_id for the same input, but the trade already exists.
        dec_id2 = _seed_decision(sym, input_hash="hh", model="m")
        assert dec_id2 != dec_id
        assert find_shadow_trade_by_input(DSN, input_hash="hh", model="m", run_id="r") == tid
        # different run_id or model -> not a match (experiments/makers stay separate)
        assert find_shadow_trade_by_input(DSN, input_hash="hh", model="m", run_id="other") is None
        assert find_shadow_trade_by_input(DSN, input_hash="hh", model="claude", run_id="r") is None
    finally:
        _cleanup(sym)


class _CountingFake:
    """Stand-in for the PAID maker: counts real decide() calls so a resume can be proven to make
    ZERO of them."""

    def __init__(self):
        self.calls = 0

    async def decide(self, inp):
        from core.models import Direction
        from decision.schema import DecisionOutput

        self.calls += 1
        if inp.confluence == "aligned_bull":
            return DecisionOutput(direction=Direction.BUY, confidence=0.7, rationale="x")
        return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="x")


def _counts(run_id):
    with psycopg.connect(DSN) as c:
        d = c.execute("SELECT count(*) FROM decisions WHERE run_id = %s", (run_id,)).fetchone()[0]
        t = c.execute("SELECT count(*) FROM trades WHERE run_id = %s", (run_id,)).fetchone()[0]
    return d, t


def test_backtest_rerun_resumes_without_repeating_decisions_or_paid_calls():
    """REAL end-to-end idempotency (not just a lookup): run the whole backtest TWICE with the
    same run_id. The second run must make ZERO decide() calls (no re-paying the LLM) and add no
    new decisions or trades — every bar resolves to `resumed`."""
    from shadow.runner import backtest_over_windows
    from tests.helpers import run as arun
    from tests.synthetic import trend

    sym = "TST_" + os.urandom(3).hex()
    run_id = "e2e-" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    windows = {name: trend(n=250, step=1.0, tf_min=m, start=end - timedelta(minutes=m * 250))
               for name, m in (("1day", 1440), ("4h", 240), ("1h", 60), ("15min", 15))}
    kw = dict(symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
              persist_dsn=DSN, run_id=run_id, model_name="fake")
    try:
        first = _CountingFake()
        rows1 = arun(backtest_over_windows(windows, decision_maker=first, **kw))
        decisions1, trades1 = _counts(run_id)
        assert first.calls > 0 and decisions1 > 0 and trades1 > 0   # the run really happened
        assert not any(r["stage"] == "resumed" for r in rows1)      # nothing to resume yet

        second = _CountingFake()
        rows2 = arun(backtest_over_windows(windows, decision_maker=second, **kw))
        assert second.calls == 0                                    # NO repeated (paid) calls
        assert _counts(run_id) == (decisions1, trades1)             # no duplicated chain
        assert any(r["stage"] == "resumed" for r in rows2)
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id = %s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id = %s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_reconcile_open_trades_closes_hit_trades_idempotently():
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import insert_decision, insert_evaluation, upsert_shadow_trade, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict
    from shadow.online import reconcile_open_trades
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import open_virtual_trade

    sym = "TST_" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    run_id = "online-test"
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        eval_id = insert_evaluation(DSN, snap_id, _eval("online", True, []))
        decision = DecisionOutput(direction="BUY", confidence=0.8, rationale="x")
        risk = RiskVerdict(approved=True, reason=None, direction="BUY", confidence=0.8,
                           sl_pct=0.3, tp_pct=0.6, risk_config_version="v")
        rec = DecisionRecord(stage="decided", symbol=sym, as_of=end, mode="online",
                             prefilter=PrefilterResult(passed=True, reasons=[], config_version="pf"),
                             decision=decision, risk=risk, input_hash="h",
                             manifest={"prompt_version": "p", "output_schema_version": "s",
                                       "feature_pipeline_version": "1.2.0", "strategy_version": "st",
                                       "risk_config_version": "v"})
        dec_id = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="fake",
                                 record=rec, ai_input={}, ai_output=decision.model_dump(mode="json"),
                                 mode="shadow", data_provider="csv")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.02,
                                   spread_provenance="observed_xtb", opened_at=end)
        upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                            outcome=reconcile(trade, []), timeframe="15min", timeout_bars=96)  # open
        tp_bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        assert reconcile_open_trades(DSN, [tp_bar], run_id=run_id) == 1   # closes the open trade
        with psycopg.connect(DSN) as c:
            row = c.execute("SELECT status, exit_reason FROM trades WHERE decision_id=%s AND run_id=%s",
                            (dec_id, run_id)).fetchone()
        assert row[0] == "closed" and row[1] == "tp_hit"
        assert reconcile_open_trades(DSN, [tp_bar], run_id=run_id) == 0   # idempotent: none left open
    finally:
        _cleanup(sym)


def test_insert_llm_call_logs_success_and_failure():
    from database.repository import insert_llm_call, upsert_snapshot
    from decision.llm_client import LlmCallResult

    sym = "TST_" + os.urandom(3).hex()
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        ok = LlmCallResult(ok=True, requested_model="claude-haiku-4-5",
                           effective_model="claude-haiku-4-5-20251001", request_id="req_1",
                           stop_reason="end_turn", input_tokens=1000, output_tokens=300,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0,
                           estimated_cost_usd=0.003, latency_ms=1200, input_hash="h")
        fail = LlmCallResult(ok=False, error="api_status:429", requested_model="claude-haiku-4-5",
                             input_hash="h")
        insert_llm_call(DSN, ok, snapshot_id=snap_id)
        insert_llm_call(DSN, fail, snapshot_id=snap_id)
        with psycopg.connect(DSN) as c:
            rows = c.execute("SELECT ok, error, effective_model, estimated_cost_usd FROM llm_calls "
                             "WHERE snapshot_id=%s ORDER BY ok DESC", (snap_id,)).fetchall()
        assert len(rows) == 2
        assert rows[0][0] is True and rows[0][2] == "claude-haiku-4-5-20251001"  # success logged w/ cost
        assert rows[1][0] is False and rows[1][1] == "api_status:429"            # failure logged w/ error
    finally:
        _cleanup(sym)


def test_spread_status_tracks_a_bar_with_no_observation_yet():
    """The ONLINE scheduler retries the quote only for a bar that has no spread observation yet —
    now answered from spread_observations, not from a (removed) snapshot column."""
    from database.repository import (
        insert_spread_observation, snapshot_spread_status, upsert_snapshot,
    )

    sym = "TST_" + os.urandom(3).hex()
    bar_close = datetime(2026, 7, 1, tzinfo=timezone.utc)
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=None))   # XTB was down
        assert snapshot_spread_status(DSN, sym, bar_close) == (True, True)
        insert_spread_observation(DSN, snapshot_id=snap_id, spread_pct=0.05,
                                  provenance="observed_xtb", observed_at=bar_close)
        assert snapshot_spread_status(DSN, sym, bar_close) == (True, False)  # XTB recovered
    finally:
        _cleanup(sym)
