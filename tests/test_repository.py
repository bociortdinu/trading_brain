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


def test_spread_less_snapshot_is_enriched():
    from database.repository import upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        assert upsert_snapshot(DSN, _packet(sym, spread=None))[0] == "inserted"  # no spread yet
        assert upsert_snapshot(DSN, _packet(sym, spread=0.05))[0] == "enriched"  # controlled fill
        with psycopg.connect(DSN) as c:
            val = c.execute("SELECT spread_pct FROM market_snapshots WHERE symbol=%s", (sym,)).fetchone()[0]
        assert float(val) == pytest.approx(0.05)
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


def test_enrichment_status_tracks_missing_spread():
    from database.repository import snapshot_enrichment_status, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    bar_close = datetime(2026, 7, 1, tzinfo=timezone.utc)
    try:
        upsert_snapshot(DSN, _packet(sym, spread=None))               # XTB was down
        assert snapshot_enrichment_status(DSN, sym, bar_close) == (True, True)
        upsert_snapshot(DSN, _packet(sym, spread=0.05))               # XTB recovered
        assert snapshot_enrichment_status(DSN, sym, bar_close) == (True, False)
    finally:
        _cleanup(sym)
