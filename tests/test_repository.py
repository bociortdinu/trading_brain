"""Snapshot upsert policy (insert / unchanged / enrich / conflict). DB-gated."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from features.mtf import build_feature_packet
from tests.synthetic import trend

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DB_DSN")


def _is_explicit_test_database(dsn: str) -> bool:
    try:
        dbname = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "")
        return dbname.lower().endswith("_test")
    except Exception:
        return False


if DSN and not _is_explicit_test_database(DSN):
    raise RuntimeError(
        "BRAIN_TEST_DB_DSN must target a database whose name ends in '_test'; "
        "refusing to run destructive repository tests against an operational database"
    )


def _db_ok() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="no reachable BRAIN_TEST_DB_DSN (..._test)")


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


# The fact tables are APPEND-ONLY for the app role (no DELETE) — cleanup/retention is an ADMIN
# operation. Use BRAIN_TEST_ADMIN_DB_DSN when provided (CI + local re-grant); fall back to the app
# DSN so the suite still runs against a DB whose grants predate the append-only revoke.
_ADMIN_DSN = os.environ.get("BRAIN_TEST_ADMIN_DB_DSN")


def _cleanup_dsn() -> str:
    return _ADMIN_DSN or DSN


def _cleanup(symbol):
    with psycopg.connect(_cleanup_dsn()) as c:
        snaps = "(SELECT id FROM market_snapshots WHERE symbol = %s)"
        decs = f"(SELECT id FROM decisions WHERE snapshot_id IN {snaps})"
        c.execute(f"DELETE FROM trades WHERE decision_id IN {decs}", (symbol,))
        c.execute(f"DELETE FROM llm_calls WHERE snapshot_id IN {snaps}", (symbol,))
        c.execute(f"DELETE FROM decisions WHERE snapshot_id IN {snaps}", (symbol,))
        c.execute("DELETE FROM snapshot_conflicts WHERE symbol = %s", (symbol,))
        # spread_observations + snapshot_evaluations CASCADE on the snapshot delete below.
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


def test_different_providers_coexist_as_distinct_observations():
    """R2-16d: a bar observed via two providers is TWO observations (different venue/feed), not a
    collision. Both are stored; `latest_snapshot_bar_close` is provider-scoped."""
    from database.repository import latest_snapshot_bar_close, upsert_snapshot

    sym = "TST_" + os.urandom(3).hex()
    try:
        s1, id_csv = upsert_snapshot(DSN, _packet(sym, provider="csv"))
        s2, id_poly = upsert_snapshot(DSN, _packet(sym, provider="polygon"))
        assert s1 == "inserted" and s2 == "inserted"     # NOT a conflict — they coexist
        assert id_csv != id_poly
        with psycopg.connect(DSN) as c:
            n = c.execute("SELECT count(*) FROM market_snapshots WHERE symbol=%s", (sym,)).fetchone()[0]
        assert n == 2                                     # two distinct source observations
        # same source re-observed -> idempotent (still one row for that provider)
        assert upsert_snapshot(DSN, _packet(sym, provider="csv"))[0] in ("unchanged", "enriched")
        # provider-scoped latest is source-specific
        assert latest_snapshot_bar_close(DSN, sym, "csv") is not None
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
        dec_id, _ = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="deterministic-fake",
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
        dec_id, _ = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="fake",
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
                   spread_pct=None, provenance=None, spread_observation_id=None,
                   run_id=None, input_fingerprint=None, blocked_reason=None, data_provider="csv"):
    """Snapshot -> evaluation -> decision; returns (dec_id). Shared by the idempotency tests."""
    from database.repository import insert_decision, insert_evaluation, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict

    end = end or datetime(2026, 7, 1, tzinfo=timezone.utc)
    if snapshot_id is None:
        _, snapshot_id = upsert_snapshot(DSN, _packet(sym, spread=0.03, provider=data_provider))
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
    dec_id, _ = insert_decision(DSN, snapshot_id=snapshot_id, evaluation_id=eval_id, model=model,
                                record=rec, ai_input=ai_input,
                                ai_output=decision.model_dump(mode="json"),
                                mode="shadow", data_provider=data_provider,
                                spread_observation_id=spread_observation_id,
                                run_id=run_id, input_fingerprint=input_fingerprint,
                                blocked_reason=blocked_reason)
    return dec_id


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


def _clear_reservations(run_id):
    with psycopg.connect(DSN) as c:
        c.execute("DELETE FROM decision_reservations WHERE run_id = %s", (run_id,))
        c.commit()


def test_concurrent_workers_exactly_one_may_call_the_model():
    """THE atomicity proof. A plain SELECT-then-INSERT let two concurrent workers both miss, both
    PAY, and only the loser's ROW get discarded — uniqueness never refunds a charge. The claim is
    taken before the call, so exactly one worker may ever call the model for a given input."""
    import threading

    from database.repository import reserve_decision

    fp, run_id = "fp-" + os.urandom(4).hex(), "conc-" + os.urandom(3).hex()
    workers = 8
    barrier = threading.Barrier(workers)
    results, lock = [], threading.Lock()

    def claim(i):
        barrier.wait()   # release all workers at once to maximise the overlap
        state, _token = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker=f"w{i}")
        with lock:
            results.append(state)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(workers)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("reserved") == 1, f"exactly one worker may pay, got {results}"
        assert results.count("held") == workers - 1
    finally:
        _clear_reservations(run_id)


def test_reservation_lease_survives_a_crashed_worker():
    """A worker that dies mid-call must not block its input forever: the claim carries a lease,
    and only once that lease expires may another worker take over."""
    from database.repository import reserve_decision

    fp, run_id = "fp-" + os.urandom(4).hex(), "lease-" + os.urandom(3).hex()
    try:
        # A claims with a live lease, then "crashes" (never completes).
        assert reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="A",
                                lease_seconds=300)[0] == "reserved"
        # B must NOT be allowed to pay while A's lease is alive.
        assert reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="B")[0] == "held"
        # Once the lease has expired, the work is reclaimable.
        with psycopg.connect(DSN) as c:
            c.execute("UPDATE decision_reservations SET lease_expires_at = now() - interval '1s' "
                      "WHERE input_fingerprint = %s AND run_id = %s", (fp, run_id))
            c.commit()
        assert reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="B")[0] == "reserved"
    finally:
        _clear_reservations(run_id)


def test_done_is_terminal_and_failed_is_retryable():
    from database.repository import complete_decision_reservation, reserve_decision

    sym = "TST_" + os.urandom(3).hex()
    fp, run_id = "fp-" + os.urandom(4).hex(), "term-" + os.urandom(3).hex()
    try:
        dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint=fp)
        state, tok_a = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="A")
        assert state == "reserved"
        # A failed call may legitimately be retried by anyone.
        complete_decision_reservation(DSN, input_fingerprint=fp, run_id=run_id, status="failed",
                                      claim_token=tok_a)
        state, tok_b = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="B")
        assert state == "reserved"
        # A completed decision is never redone (and never re-paid).
        complete_decision_reservation(DSN, input_fingerprint=fp, run_id=run_id, status="done",
                                      claim_token=tok_b, decision_id=dec_id)
        assert reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="C")[0] == "done"
    finally:
        _clear_reservations(run_id)
        _cleanup(sym)


def test_done_requires_a_decision_in_repo_and_in_db():
    """'done' means a COMPLETE chain: it must carry a real decision. The repository refuses a
    NULL decision_id up front, and the DB CHECK is the backstop against any other writer."""
    from database.repository import complete_decision_reservation, reserve_decision

    fp, run_id = "fp-" + os.urandom(4).hex(), "req-" + os.urandom(3).hex()
    try:
        _, tok = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="A")
        with pytest.raises(ValueError):   # repository guard
            complete_decision_reservation(DSN, input_fingerprint=fp, run_id=run_id,
                                          status="done", claim_token=tok, decision_id=None)
        with psycopg.connect(DSN) as c:   # DB backstop
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("UPDATE decision_reservations SET status='done', decision_id=NULL "
                          "WHERE input_fingerprint=%s AND run_id=%s", (fp, run_id))
            c.rollback()
    finally:
        _clear_reservations(run_id)


def test_a_stale_worker_cannot_complete_a_reservation_it_lost():
    """OWNERSHIP. Without a claim token, a worker whose lease expired could wake up and stamp
    'done' over the claim someone else now holds — leaving status=done, worker=<the other one>,
    decision_id=NULL, and every later worker told 'already decided' about a decision that does
    not exist. Reproduced exactly that before the token existed."""
    from database.repository import (
        StaleClaimError, complete_decision_reservation, reserve_decision,
    )

    sym = "TST_" + os.urandom(3).hex()
    fp, run_id = "fp-" + os.urandom(4).hex(), "stale-" + os.urandom(3).hex()
    try:
        dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint=fp)
        state, tok_a = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="A")
        assert state == "reserved"
        with psycopg.connect(DSN) as c:   # A hangs; its lease lapses
            c.execute("UPDATE decision_reservations SET lease_expires_at = now() - interval '1s' "
                      "WHERE input_fingerprint = %s AND run_id = %s", (fp, run_id))
            c.commit()
        state, tok_b = reserve_decision(DSN, input_fingerprint=fp, run_id=run_id, worker="B")
        assert state == "reserved" and tok_b != tok_a   # B legitimately took over

        # A wakes up stale: its completion must be REFUSED, not silently applied.
        with pytest.raises(StaleClaimError):
            complete_decision_reservation(DSN, input_fingerprint=fp, run_id=run_id,
                                          status="done", claim_token=tok_a, decision_id=dec_id)
        with psycopg.connect(DSN) as c:
            status, worker = c.execute(
                "SELECT status, worker FROM decision_reservations WHERE input_fingerprint = %s",
                (fp,)).fetchone()
        assert (status, worker) == ("in_progress", "B"), "B's live claim must be untouched"
        # And B can still finish its own work.
        complete_decision_reservation(DSN, input_fingerprint=fp, run_id=run_id, status="done",
                                      claim_token=tok_b, decision_id=dec_id)
    finally:
        _clear_reservations(run_id)
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


def _synthetic_windows():
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return {name: trend(n=250, step=1.0, tf_min=m, start=end - timedelta(minutes=m * 250))
            for name, m in (("1day", 1440), ("4h", 240), ("1h", 60), ("15min", 15))}


def test_llm_cap_does_not_strand_a_reservation():
    """The cap used to be checked AFTER the claim, so stopping on budget left an 'in_progress'
    reservation with no decision behind it — blocking that bar until its lease lapsed."""
    from shadow.runner import ConfluenceStrategy, _CountingMaker, backtest_over_windows
    from tests.helpers import run as arun

    sym, run_id = "TST_" + os.urandom(3).hex(), "cap-" + os.urandom(3).hex()
    try:
        rows = arun(backtest_over_windows(
            _synthetic_windows(), symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
            decision_maker=_CountingMaker(ConfluenceStrategy()), max_llm_calls=0,
            persist_dsn=DSN, run_id=run_id, model_name="fake"))
        assert rows[-1]["stage"] == "llm_cap_reached"
        with psycopg.connect(DSN) as c:
            left = c.execute("SELECT count(*) FROM decision_reservations WHERE run_id = %s",
                             (run_id,)).fetchone()[0]
        assert left == 0, "stopping on the budget must not leave a claim behind"
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM decision_reservations WHERE run_id = %s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_a_stateful_run_refuses_a_second_concurrent_worker():
    """Per-bar reservations stop double PAYING, they do not make a stateful run parallelisable:
    two workers just split the bars, each tracks its own busy_until, and 'one position at a time'
    silently stops holding (measured before the lock: 19 trades, 11 overlapping pairs). The run
    lock refuses the second worker instead."""
    import threading

    from database.repository import RunLockedError
    from shadow.runner import ConfluenceStrategy, _CountingMaker, backtest_over_windows

    sym, run_id = "TST_" + os.urandom(3).hex(), "lock-" + os.urandom(3).hex()
    windows = _synthetic_windows()
    outcomes = {}

    def go(tag):
        try:
            asyncio.run(backtest_over_windows(
                windows, symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
                decision_maker=_CountingMaker(ConfluenceStrategy()), persist_dsn=DSN,
                run_id=run_id, model_name="fake", worker_id=tag))
            outcomes[tag] = "ran"
        except RunLockedError:
            outcomes[tag] = "refused"

    try:
        threads = [threading.Thread(target=go, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes.values()) == ["ran", "refused"], f"got {outcomes}"
        assert _overlapping_pairs(run_id) == 0, "a serialised run must never hold two positions"
    finally:
        with psycopg.connect(DSN) as c:
            for t in ("trades", "decisions", "decision_reservations"):
                c.execute(f"DELETE FROM {t} WHERE run_id = %s", (run_id,))
            c.commit()
        _cleanup(sym)


def _trades_of(run_id):
    with psycopg.connect(DSN) as c:
        return c.execute(
            "SELECT opened_at, side, closed_at, status, exit_reason, r_multiple FROM trades "
            "WHERE run_id = %s ORDER BY opened_at", (run_id,)).fetchall()


def _overlapping_pairs(run_id):
    """Count pairs of trades whose holding intervals overlap. An OPEN trade (closed_at IS NULL)
    is held to +infinity — the earlier no-op `closed_at or opened` made this blind to exactly the
    open trades it was meant to catch, so a still-open position never counted as an overlap."""
    with psycopg.connect(DSN) as c:
        return c.execute(
            """
            SELECT count(*) FROM trades a JOIN trades b
              ON a.run_id = b.run_id AND a.id < b.id
             AND b.opened_at < COALESCE(a.closed_at, 'infinity'::timestamptz)
             AND a.opened_at < COALESCE(b.closed_at, 'infinity'::timestamptz)
            WHERE a.run_id = %s
            """,
            (run_id,),
        ).fetchone()[0]


def test_interrupted_run_resumes_to_the_same_result_as_an_uninterrupted_one():
    """Crash-in-the-middle -> resume must reproduce the uninterrupted run.

    A resume that only knew 'already decided' forgot any position still open and would stack a
    second entry on top of it, so the resumed run reported different trades than the run it was
    supposed to be continuing. Here the first pass is cut short by the LLM budget (a partial,
    persisted run), then resumed, and its trades must match a clean run bar for bar."""
    from shadow.runner import _CountingMaker, backtest_over_windows
    from tests.helpers import run as arun
    from tests.synthetic import trend

    sym = "TST_" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    windows = {name: trend(n=250, step=1.0, tf_min=m, start=end - timedelta(minutes=m * 250))
               for name, m in (("1day", 1440), ("4h", 240), ("1h", 60), ("15min", 15))}
    interrupted, clean = "cut-" + os.urandom(3).hex(), "clean-" + os.urandom(3).hex()

    def go(run_id, cap):
        return arun(backtest_over_windows(
            windows, symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
            decision_maker=_CountingMaker(_CountingFake()), max_llm_calls=cap,
            persist_dsn=DSN, run_id=run_id, model_name="fake"))

    try:
        cut = go(interrupted, 3)                      # dies after 3 decisions
        assert any(r["stage"] == "llm_cap_reached" for r in cut)
        partial = _trades_of(interrupted)
        assert len(partial) >= 1, "the interrupted run must have persisted real work"

        resumed_rows = go(interrupted, None)          # resume the SAME run to completion
        assert any(r["stage"] == "resumed" for r in resumed_rows)

        clean_rows = go(clean, None)                   # an uninterrupted run for comparison
        assert _trades_of(interrupted) == _trades_of(clean), (
            "a resumed run must reproduce the uninterrupted one, trade for trade")
        # FULL report equality, not just the trades table: every bar's disposition
        # (direction / approved / blocked / outcome) must match, stage aside.
        assert _report_shape(resumed_rows) == _report_shape(clean_rows), (
            "the resumed run's per-bar report must equal the uninterrupted one")
    finally:
        with psycopg.connect(DSN) as c:
            for r in (interrupted, clean):
                c.execute("DELETE FROM trades WHERE run_id = %s", (r,))
                c.execute("DELETE FROM decisions WHERE run_id = %s", (r,))
                c.execute("DELETE FROM decision_reservations WHERE run_id = %s", (r,))
            c.commit()
        _cleanup(sym)


def _report_shape(rows):
    """Per-bar disposition, ignoring `stage` (a resumed bar legitimately reads 'resumed' where a
    fresh one reads 'decided'). Outcome floats are rounded so DB round-trips compare equal."""
    def norm(o):
        if not o:
            return None
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in o.items() if k != "exit_price"}
    return [(r["as_of"], r["direction"], r["approved"], r["blocked"], norm(r["outcome"]))
            for r in rows if r["stage"] != "llm_cap_reached"]


def test_blocked_disposition_is_persisted_and_reconstructed_on_resume():
    """approved-but-blocked (a position was already open) is a real disposition, not a blank. It
    is stored on the decision and rebuilt on resume, so a resumed report can tell it apart from
    'approved and traded'."""
    from database.repository import load_decided_outcome
    from shadow.runner import _resume_row

    sym = "TST_" + os.urandom(3).hex()
    fp, run_id = "fp-" + os.urandom(4).hex(), "blk-" + os.urandom(3).hex()
    try:
        _seed_decision(sym, run_id=run_id, input_fingerprint=fp, blocked_reason="position_open")
        loaded = load_decided_outcome(DSN, input_fingerprint=fp, run_id=run_id)
        assert loaded["blocked_reason"] == "position_open"
        assert loaded["status"] is None   # blocked -> no trade row
        row = _resume_row(DSN, datetime(2026, 7, 1, tzinfo=timezone.utc), run_id, fp, "done")
        assert row["approved"] is True and row["blocked"] == "position_open"
        assert row["outcome"] is None     # reconstructed as approved-but-not-traded
    finally:
        _clear_reservations(run_id)
        _cleanup(sym)


class _VerdictMaker:
    """A maker whose verdict we control per run, so a recovery run can be given a DIFFERENT
    verdict than the one persisted — proving recovery reuses the STORED decision, not a fresh
    call. Counts calls so we can assert the recovery makes none."""

    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    async def decide(self, inp):
        from core.models import Direction
        from decision.schema import DecisionOutput

        self.calls += 1
        if self.verdict == "bull" and inp.confluence == "aligned_bull":
            return DecisionOutput(direction=Direction.BUY, confidence=0.7, rationale="x")
        return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="x")


def test_recovery_reuses_the_stored_decision_and_never_recalls_the_maker():
    """The blocking bug the reviewer reproduced: after a reclaimed lease the runner used to re-call
    the maker and attach the NEW verdict's trade to the OLD decision. Here the first run decides
    BUY and persists; ONE approved bar is then corrupted to look like a crash-after-decision (its
    trade deleted, its reservation reset to an expired in_progress). The resume runs a maker that
    would say NO_TRADE — yet it must make ZERO calls and rebuild the BUY trade the stored decision
    describes."""
    from shadow.runner import backtest_over_windows
    from tests.helpers import run as arun

    sym = "TST_" + os.urandom(3).hex()
    run_id = "recov-" + os.urandom(3).hex()
    windows = _synthetic_windows()

    def go(maker):
        return arun(backtest_over_windows(
            windows, symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
            decision_maker=maker, persist_dsn=DSN, run_id=run_id, model_name="fake"))

    try:
        go(_VerdictMaker("bull"))                      # full BUY run: decisions + trades persisted
        with psycopg.connect(DSN) as c:                # corrupt ONE approved bar -> crash shape
            dec = c.execute(
                "SELECT d.id, d.input_fingerprint FROM decisions d JOIN trades t ON t.decision_id=d.id "
                "WHERE d.run_id=%s AND d.risk_verdict='approved' AND d.blocked_reason IS NULL "
                "ORDER BY d.as_of LIMIT 1", (run_id,)).fetchone()
            c.execute("DELETE FROM trades WHERE decision_id=%s", (dec[0],))
            c.execute("UPDATE decision_reservations SET status='in_progress', claim_token='stale', "
                      "lease_expires_at = now() - interval '1s' "
                      "WHERE input_fingerprint=%s AND run_id=%s", (dec[1], run_id))
            c.commit()

        recovery = _VerdictMaker("notrade")            # would say NO_TRADE if asked
        go(recovery)
        assert recovery.calls == 0, "recovery must reuse the stored decision, never re-call the maker"
        with psycopg.connect(DSN) as c:
            side = c.execute("SELECT side FROM trades WHERE decision_id=%s", (dec[0],)).fetchone()
            done = c.execute("SELECT status, decision_id FROM decision_reservations "
                             "WHERE input_fingerprint=%s AND run_id=%s", (dec[1], run_id)).fetchone()
        assert side is not None and side[0] == "buy", "trade must match the PERSISTED decision, not NO_TRADE"
        assert done == ("done", dec[0]), "the recovered chain must be terminal against its decision"
    finally:
        with psycopg.connect(DSN) as c:
            for t in ("trades", "decisions", "decision_reservations"):
                c.execute(f"DELETE FROM {t} WHERE run_id = %s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_resume_after_a_crash_between_trade_and_release_validates_the_chain():
    """Crash in the OTHER window: the trade WAS persisted but the reservation was never released,
    so it is left 'in_progress'. Resume re-claims it, sees the trade already exists, rebuilds
    nothing, and just finalises 'done'. No maker call, no duplicate trade."""
    from shadow.runner import backtest_over_windows
    from tests.helpers import run as arun

    sym = "TST_" + os.urandom(3).hex()
    run_id = "rel-" + os.urandom(3).hex()
    windows = _synthetic_windows()

    def go(maker):
        return arun(backtest_over_windows(
            windows, symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
            decision_maker=maker, persist_dsn=DSN, run_id=run_id, model_name="fake"))

    try:
        go(_VerdictMaker("bull"))
        with psycopg.connect(DSN) as c:                # trade kept, reservation left in_progress
            dec = c.execute(
                "SELECT d.id, d.input_fingerprint FROM decisions d JOIN trades t ON t.decision_id=d.id "
                "WHERE d.run_id=%s AND d.risk_verdict='approved' AND d.blocked_reason IS NULL "
                "ORDER BY d.as_of LIMIT 1", (run_id,)).fetchone()
            before = c.execute("SELECT id, opened_at, closed_at FROM trades WHERE decision_id=%s",
                               (dec[0],)).fetchone()
            c.execute("UPDATE decision_reservations SET status='in_progress', claim_token='stale', "
                      "lease_expires_at = now() - interval '1s' "
                      "WHERE input_fingerprint=%s AND run_id=%s", (dec[1], run_id))
            c.commit()

        recovery = _VerdictMaker("notrade")
        go(recovery)
        assert recovery.calls == 0
        with psycopg.connect(DSN) as c:
            after = c.execute("SELECT id, opened_at, closed_at FROM trades WHERE decision_id=%s",
                              (dec[0],)).fetchall()
            done = c.execute("SELECT status FROM decision_reservations "
                             "WHERE input_fingerprint=%s AND run_id=%s", (dec[1], run_id)).fetchone()
        assert len(after) == 1 and after[0] == before, "the existing trade must be untouched, not duplicated"
        assert done[0] == "done"
    finally:
        with psycopg.connect(DSN) as c:
            for t in ("trades", "decisions", "decision_reservations"):
                c.execute(f"DELETE FROM {t} WHERE run_id = %s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_resume_reconstructs_a_trade_after_a_crash_between_decision_and_trade():
    """The narrow window the reviewer flagged: the decision is persisted, then the process dies
    BEFORE the trade. 'done' is released only after the trade, so the claim is left 'in_progress'
    (not 'done'); its lease lapses and the resume re-claims it, reuses the decision, and rebuilds
    the missing trade. The run then equals a clean one."""
    import shadow.runner as runner
    from shadow.runner import _CountingMaker, backtest_over_windows
    from tests.helpers import run as arun

    sym = "TST_" + os.urandom(3).hex()
    crashed, clean = "crash-" + os.urandom(3).hex(), "ok-" + os.urandom(3).hex()
    windows = _synthetic_windows()

    real_persist_trade = runner._persist_trade
    state = {"n": 0}

    def crash_on_second_trade(*a, **k):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("simulated crash AFTER the decision, BEFORE the trade")
        return real_persist_trade(*a, **k)

    def go(run_id):
        return arun(backtest_over_windows(
            windows, symbol=sym, provider_name="csv", modeled_spread_pct=0.02,
            decision_maker=_CountingMaker(_CountingFake()), persist_dsn=DSN, run_id=run_id,
            model_name="fake"))

    try:
        runner._persist_trade = crash_on_second_trade
        with pytest.raises(RuntimeError):
            go(crashed)
        # State a crash leaves: a decision with no trade, and its reservation NOT 'done'.
        with psycopg.connect(DSN) as c:
            orphan = c.execute(
                "SELECT count(*) FROM decisions d WHERE d.run_id = %s AND d.risk_verdict='approved' "
                "AND d.blocked_reason IS NULL "  # a blocked bar is legitimately trade-less
                "AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.decision_id = d.id)", (crashed,)
            ).fetchone()[0]
        assert orphan >= 1, "the crash must have left a decided-but-untraded bar"
        # Expire the lease so the resume may re-claim the interrupted bar.
        with psycopg.connect(DSN) as c:
            c.execute("UPDATE decision_reservations SET lease_expires_at = now() - interval '1s' "
                      "WHERE run_id = %s", (crashed,))
            c.commit()

        runner._persist_trade = real_persist_trade
        go(crashed)                                    # resume: rebuilds the missing trade
        go(clean)                                      # reference run
        with psycopg.connect(DSN) as c:
            still_orphan = c.execute(
                "SELECT count(*) FROM decisions d WHERE d.run_id = %s AND d.risk_verdict='approved' "
                "AND d.blocked_reason IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.decision_id = d.id)", (crashed,)
            ).fetchone()[0]
        assert still_orphan == 0, "resume must rebuild the trade the crash skipped"
        assert _trades_of(crashed) == _trades_of(clean)
    finally:
        runner._persist_trade = real_persist_trade
        with psycopg.connect(DSN) as c:
            for r in (crashed, clean):
                c.execute("DELETE FROM trades WHERE run_id = %s", (r,))
                c.execute("DELETE FROM decisions WHERE run_id = %s", (r,))
                c.execute("DELETE FROM decision_reservations WHERE run_id = %s", (r,))
            c.commit()
        _cleanup(sym)


def test_open_trade_is_reconciled_with_the_rates_it_was_opened_with():
    """A live config change must NOT silently re-price a position that is already open. The trade
    is opened with a non-zero overnight swap and held past a rollover; when it later closes, its
    R must be net of the swap it was OPENED with even though the CURRENT config has swap=0."""
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import upsert_shadow_trade
    from shadow.online import reconcile_open_trades
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import ShadowConfig, cost_manifest, open_virtual_trade

    sym, run_id = "TST_" + os.urandom(3).hex(), "swap-" + os.urandom(3).hex()
    opened = datetime(2026, 7, 2, 1, 45, tzinfo=timezone.utc)   # Thu, before the 02:00 rollover
    opened_cfg = ShadowConfig(swap_pct_per_night=0.05, rollover_hour_utc=2)
    try:
        dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint="swapfp")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                                   spread_provenance="modeled", opened_at=opened)
        upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                            outcome=reconcile(trade, [], opened_cfg), timeframe="15min",
                            timeout_bars=96, costs=cost_manifest(trade, opened_cfg))

        # CONTIGUOUS covered bars crossing the 02:00 rollover (one swap night); TP hits on bar 2.
        b1 = Candle(open_time=opened, close_time=opened + timedelta(minutes=15),
                    open=4000, high=4001, low=3999, close=4000, volume=1.0)          # 01:45->02:00
        tp_bar = Candle(open_time=opened + timedelta(minutes=15),
                        close_time=opened + timedelta(minutes=30),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)        # 02:00->02:15 TP
        bars, now = [b1, tp_bar], opened + timedelta(minutes=30)
        # CURRENT config says swap=0. The fix must ignore it for this already-open trade.
        assert reconcile_open_trades(DSN, bars, symbol=sym, provider_name="csv", now=now,
                                     shadow_config=ShadowConfig(swap_pct_per_night=0.0)) == 1
        with psycopg.connect(DSN) as c:
            r_stored = float(c.execute("SELECT r_multiple FROM trades WHERE decision_id=%s",
                                       (dec_id,)).fetchone()[0])

        # Reference: reconcile the SAME trade directly with the opened config vs a swap-free one.
        r_with_swap = reconcile(trade, bars, opened_cfg).r_multiple
        r_no_swap = reconcile(trade, bars, ShadowConfig(swap_pct_per_night=0.0)).r_multiple
        assert r_with_swap < r_no_swap, "the swap must actually move R (test precondition)"
        assert r_stored == pytest.approx(r_with_swap), "closed with the OPENED swap, not current 0"
        assert r_stored != pytest.approx(r_no_swap), "must NOT have used the current swap=0"
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
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
        dec_id, _ = insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="fake",
                                 record=rec, ai_input={}, ai_output=decision.model_dump(mode="json"),
                                 mode="shadow", data_provider="csv")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.02,
                                   spread_provenance="observed_xtb", opened_at=end)
        upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                            outcome=reconcile(trade, []), timeframe="15min", timeout_bars=96)  # open
        tp_bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        now = end + timedelta(minutes=15)
        assert reconcile_open_trades(DSN, [tp_bar], symbol=sym, provider_name="csv",
                                     now=now) == 1   # closes the open trade
        with psycopg.connect(DSN) as c:
            row = c.execute("SELECT status, exit_reason FROM trades WHERE decision_id=%s AND run_id=%s",
                            (dec_id, run_id)).fetchone()
        assert row[0] == "closed" and row[1] == "tp_hit"
        assert reconcile_open_trades(DSN, [tp_bar], symbol=sym, provider_name="csv",
                                     now=now) == 0   # idempotent: none left open
    finally:
        _cleanup(sym)


def test_reconcile_open_trades_skips_an_uncovered_trade_fail_closed():
    """P0-2 fail-closed end to end: an open trade OLDER than the fetched window is NOT expired or
    closed on incomplete data — even a TP bar cannot close it, because an earlier SL/TP could hide
    in the uncovered open-market gap. It stays open until coverage is available."""
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import upsert_shadow_trade
    from shadow.online import reconcile_open_trades
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import ShadowConfig, cost_manifest, open_virtual_trade

    sym, run_id = "TST_" + os.urandom(3).hex(), "cov-" + os.urandom(3).hex()
    opened = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)   # Monday
    cfg = ShadowConfig()
    try:
        dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint="covfp")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                                   spread_provenance="modeled", opened_at=opened)
        upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                            outcome=reconcile(trade, [], cfg), timeframe="15min",
                            timeout_bars=96, costs=cost_manifest(trade, cfg))
        # A TP bar TWO DAYS later with `now` two days later: hundreds of open-market M15 bars are
        # missing between entry and this bar -> uncovered -> the trade must NOT close.
        tp_bar = Candle(open_time=datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc),
                        close_time=datetime(2026, 7, 8, 14, 15, tzinfo=timezone.utc),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        now = datetime(2026, 7, 8, 14, 15, tzinfo=timezone.utc)
        assert reconcile_open_trades(DSN, [tp_bar], symbol=sym, provider_name="csv", now=now) == 0
        with psycopg.connect(DSN) as c:
            status = c.execute("SELECT status FROM trades WHERE decision_id=%s",
                               (dec_id,)).fetchone()[0]
        assert status == "open"     # fail-closed: not expired/closed on incomplete data
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_reconcile_drains_open_trades_from_a_previous_run():
    """R2-5 lifecycle: a NEW run must reconcile (drain) positions left open by a PREVIOUS run for
    the same symbol — closing each under its OWN run_id — else old trades are orphaned and the new
    run's symbol-wide position gate blocks forever."""
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import upsert_shadow_trade
    from shadow.online import reconcile_open_trades
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import ShadowConfig, cost_manifest, open_virtual_trade

    sym, old_run = "TST_" + os.urandom(3).hex(), "old-" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cfg = ShadowConfig()
    try:
        dec_id = _seed_decision(sym, run_id=old_run, input_fingerprint="drainfp")
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                                   spread_provenance="modeled", opened_at=end)
        upsert_shadow_trade(DSN, decision_id=dec_id, run_id=old_run, symbol=sym, trade=trade,
                            outcome=reconcile(trade, [], cfg), timeframe="15min",
                            timeout_bars=96, costs=cost_manifest(trade, cfg))
        tp_bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        now = end + timedelta(minutes=15)
        # Reconcile by SYMBOL (a new run tick has a different run_id) -> still closes the old trade.
        assert reconcile_open_trades(DSN, [tp_bar], symbol=sym, provider_name="csv", now=now) == 1
        with psycopg.connect(DSN) as c:
            row = c.execute("SELECT status, run_id FROM trades WHERE decision_id=%s",
                           (dec_id,)).fetchone()
        assert row[0] == "closed" and row[1] == old_run   # closed under ITS OWN run, not the new one
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (old_run,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (old_run,))
            c.commit()
        _cleanup(sym)


def test_reconcile_skips_a_trade_frozen_under_a_different_provider():
    """R3-2: a trade opened with polygon bars must NOT be reconciled by a csv tick — the two feeds
    are different price series. The cross-provider trade stays OPEN (skipped as source mismatch);
    only the same-provider trade is closed. This proves reconcile uses each trade's FROZEN provider,
    not the current process's provider."""
    from core.models import Direction
    from data_collector.providers.base import Candle
    from database.repository import upsert_shadow_trade
    from shadow.online import reconcile_open_trades
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import ShadowConfig, cost_manifest, open_virtual_trade

    sym = "TST_" + os.urandom(3).hex()
    run = "run-" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cfg = ShadowConfig()
    try:
        # One trade frozen under polygon, one under csv, same symbol.
        poly_dec = _seed_decision(sym, run_id=run, input_fingerprint="polyfp",
                                  data_provider="polygon")
        csv_dec = _seed_decision(sym, run_id=run, input_fingerprint="csvfp",
                                 data_provider="csv")
        for dec_id in (poly_dec, csv_dec):
            trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                                       spread_provenance="modeled", opened_at=end)
            upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run, symbol=sym, trade=trade,
                                outcome=reconcile(trade, [], cfg), timeframe="15min",
                                timeout_bars=96, costs=cost_manifest(trade, cfg))
        tp_bar = Candle(open_time=end, close_time=end + timedelta(minutes=15),
                        open=4000, high=4030, low=3999, close=4025, volume=1.0)
        now = end + timedelta(minutes=15)
        # A csv tick: closes ONLY the csv-frozen trade; the polygon trade is skipped, stays open.
        assert reconcile_open_trades(DSN, [tp_bar], symbol=sym, provider_name="csv", now=now) == 1
        with psycopg.connect(DSN) as c:
            poly_status = c.execute("SELECT status FROM trades WHERE decision_id=%s",
                                    (poly_dec,)).fetchone()[0]
            csv_status = c.execute("SELECT status FROM trades WHERE decision_id=%s",
                                   (csv_dec,)).fetchone()[0]
        assert poly_status == "open"     # cross-provider trade NOT reconciled with foreign bars
        assert csv_status == "closed"    # same-provider trade closed normally
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run,))
            c.commit()
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


def test_verified_scope_requires_a_real_commit_not_just_clean():
    """R2-7: a run with git_dirty='false' but git_commit unknown/absent must NOT count as verified —
    a clean flag without a provable commit is not reproducible. Mirrors the dashboard's scope."""
    from database.repository import assert_run_manifest

    good, bad, ukn = ("v-good-" + os.urandom(2).hex(), "v-bad-" + os.urandom(2).hex(),
                      "v-ukn-" + os.urandom(2).hex())
    verified_sql = (
        "SELECT rm.run_id FROM run_manifests rm "
        "WHERE rm.manifest->>'run_kind' IN ('shadow_online','executable_backtest') "
        "AND rm.manifest->>'git_dirty'='false' "
        "AND COALESCE(rm.manifest->>'git_commit','unknown') NOT IN ('unknown','')")
    try:
        assert_run_manifest(DSN, good, {"run_kind": "shadow_online", "git_dirty": "false",
                                        "git_commit": "abc1234"}, "hgood")
        assert_run_manifest(DSN, bad, {"run_kind": "shadow_online", "git_dirty": "false",
                                       "git_commit": "unknown"}, "hbad")
        assert_run_manifest(DSN, ukn, {"run_kind": "shadow_online", "git_dirty": "false"}, "hukn")
        with psycopg.connect(DSN) as c:
            verified = {r[0] for r in c.execute(verified_sql).fetchall()}
        assert good in verified
        assert bad not in verified and ukn not in verified   # clean flag alone is not enough
    finally:
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("DELETE FROM run_manifests WHERE run_id = ANY(%s)", ([good, bad, ukn],))
            c.commit()


def test_backtest_run_manifest_includes_full_eligibility_policy():
    """R2-3: the backtest execution manifest must fold in the eligibility policy INCLUDING
    max_clock_skew_seconds, so two runs with different eligibility can't share a run/fingerprint."""
    from datetime import timedelta

    from shadow.runner import ConfluenceStrategy, backtest_over_windows
    from tests.helpers import run as run_async
    from tests.synthetic import trend

    end = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def w(m):
        return trend(n=250, step=1.0, tf_min=m, start=end - timedelta(minutes=m * 250))

    windows = {"1day": w(1440), "4h": w(240), "1h": w(60), "15min": w(15)}
    run_id, sym = "elig-" + os.urandom(3).hex(), "TST_elig_" + os.urandom(3).hex()
    try:
        run_async(backtest_over_windows(
            windows, symbol=sym, provider_name="csv", decision_maker=ConfluenceStrategy(),
            modeled_spread_pct=0.02, persist_dsn=DSN, run_id=run_id))
        with psycopg.connect(DSN) as c:
            manifest = c.execute("SELECT manifest FROM run_manifests WHERE run_id=%s",
                                 (run_id,)).fetchone()[0]
        assert "eligibility_policy" in manifest                       # was omitted in the backtest
        assert "max_clock_skew_seconds" in manifest["eligibility_policy"]   # was omitted in as_policy()
    finally:
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM run_manifests WHERE run_id=%s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_llm_audit_is_atomic_with_the_decision():
    """A paid call must never become invisible: the decision and its llm_calls audit are one
    transaction. If the audit insert fails, the decision rolls back too — never a committed
    decision with a lost paid call (which the separate-transaction version did leave behind)."""
    import database.repository as repo
    from database.repository import insert_decision, insert_evaluation, upsert_snapshot
    from decision.llm_client import LlmCallResult
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict

    sym, run_id = "TST_" + os.urandom(3).hex(), "atomic-" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    real = repo._llm_call_row
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        eval_id = insert_evaluation(DSN, snap_id, _eval("replay", True, []))
        decision = DecisionOutput(direction="BUY", confidence=0.8, rationale="x")
        risk = RiskVerdict(approved=True, reason=None, direction="BUY", confidence=0.8,
                           sl_pct=0.3, tp_pct=0.6, risk_config_version="v")
        rec = DecisionRecord(stage="decided", symbol=sym, as_of=end, mode="replay",
                             prefilter=PrefilterResult(passed=True, reasons=[], config_version="pf"),
                             decision=decision, risk=risk, input_hash="h",
                             manifest={"prompt_version": "p", "output_schema_version": "s",
                                       "feature_pipeline_version": "1.2.0", "strategy_version": "st",
                                       "risk_config_version": "v"})
        result = LlmCallResult(ok=True, requested_model="m", input_hash="h")

        repo._llm_call_row = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit boom"))
        with pytest.raises(RuntimeError):
            insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="m", record=rec,
                            ai_input={}, ai_output={}, mode="shadow", data_provider="csv",
                            run_id=run_id, input_fingerprint="atomfp", llm_result=result)
        with psycopg.connect(DSN) as c:
            d = c.execute("SELECT count(*) FROM decisions WHERE run_id=%s", (run_id,)).fetchone()[0]
            l = c.execute("SELECT count(*) FROM llm_calls WHERE snapshot_id=%s", (snap_id,)).fetchone()[0]
        assert (d, l) == (0, 0), "audit failure must roll the decision back too, not leave it committed"
    finally:
        repo._llm_call_row = real
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_fact_tables_are_append_only_for_the_app_role():
    """R2-16b: the app role can neither UPDATE nor DELETE the immutable fact tables — closing the
    delete+reinsert loophole that could rewrite history. Retention/cleanup is an ADMIN operation.
    Requires the append-only grant (re-run `migrate`); skips if DELETE is still granted (older DB)."""
    for table in ("run_manifests", "llm_calls", "spread_observations", "snapshot_evaluations"):
        with psycopg.connect(DSN) as c:
            try:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"DELETE FROM {table}")     # app role must NOT be able to delete facts
            except pytest.fail.Exception:
                pytest.skip(f"{table}: DELETE still granted (re-run migrate to apply append-only)")
            finally:
                c.rollback()


def test_run_manifest_pins_a_run_to_one_config():
    """A run_id is ONE frozen setup. The first use records its execution-manifest hash; a later
    use with a different config is REFUSED (else two configs mix into one experiment)."""
    from database.repository import RunConfigMismatch, assert_run_manifest

    run_id = "rm-" + os.urandom(3).hex()
    try:
        assert_run_manifest(DSN, run_id, {"slippage": 0.005}, "hashA")
        assert_run_manifest(DSN, run_id, {"slippage": 0.005}, "hashA")   # same -> idempotent
        with pytest.raises(RunConfigMismatch):
            assert_run_manifest(DSN, run_id, {"slippage": 0.010}, "hashB")   # different -> refused
        with psycopg.connect(DSN) as c:   # the app role cannot rewrite the pin (UPDATE revoked)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE run_manifests SET manifest_hash='x' WHERE run_id=%s", (run_id,))
            c.rollback()
    finally:
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("DELETE FROM run_manifests WHERE run_id=%s", (run_id,))
            c.commit()


def test_online_verifies_run_manifest_before_any_fetch_or_mutation():
    """A shadow_tick started with a config incompatible with the run_id must abort with ZERO side
    effects. The old order fetched + reconciled (mutating open trades) and could return at the
    position gate WITHOUT ever checking the manifest. Now the manifest is verified first, so a
    mismatch raises before the provider is even touched."""
    import asyncio

    from config.settings import Settings
    from database.repository import RunConfigMismatch, assert_run_manifest
    from shadow.online import shadow_tick
    from shadow.virtual_broker import ShadowConfig

    run_id = "rm-order-" + os.urandom(3).hex()

    class ExplodingProvider:
        async def get_ohlcv(self, *a, **k):
            raise AssertionError("provider was fetched BEFORE the run-manifest check")

    try:
        assert_run_manifest(DSN, run_id, {"pinned": "A"}, "hashA")   # pin to a different config
        settings = Settings(db_dsn=DSN)
        with pytest.raises(RunConfigMismatch):        # NOT AssertionError -> no fetch happened
            asyncio.run(shadow_tick(
                settings, ExplodingProvider(), "csv", decision_maker=object(),
                run_id=run_id, shadow_config=ShadowConfig(commission_pct=0.07)))
    finally:
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("DELETE FROM run_manifests WHERE run_id=%s", (run_id,))
            c.commit()


def test_online_decision_and_open_trade_are_one_atomic_chain():
    """Online writes the decision + its open trade in ONE transaction, so a crash can't leave a
    committed decision with no trade (online had no recovery for that window). And a re-run with
    the same fingerprint (a reclaimed crash between the atomic commit and the reservation
    completion) does NOT create a second trade."""
    from core.models import Direction
    from database.repository import insert_decision, insert_evaluation, upsert_snapshot
    from decision.pipeline import DecisionRecord
    from decision.prefilter import PrefilterResult
    from decision.schema import DecisionOutput
    from risk.engine import RiskVerdict
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import cost_manifest, open_virtual_trade

    sym, run_id = "TST_" + os.urandom(3).hex(), "atomtr-" + os.urandom(3).hex()
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
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
        trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.02,
                                   spread_provenance="observed_xtb", opened_at=end)
        open_trade = {"symbol": sym, "trade": trade, "outcome": reconcile(trade, []),
                      "timeframe": "15min", "timeout_bars": 96,
                      "costs": cost_manifest(trade, __import__("shadow.virtual_broker", fromlist=["ShadowConfig"]).ShadowConfig()),
                      "observed_at": None}

        def persist():
            return insert_decision(DSN, snapshot_id=snap_id, evaluation_id=eval_id, model="m",
                                   record=rec, ai_input={}, ai_output={}, mode="shadow",
                                   data_provider="csv", run_id=run_id, input_fingerprint="atomtrfp",
                                   open_trade=open_trade)

        dec_id, inserted = persist()
        assert inserted is True
        with psycopg.connect(DSN) as c:
            n = c.execute("SELECT count(*) FROM trades WHERE decision_id=%s", (dec_id,)).fetchone()[0]
        assert n == 1, "the decision and its open trade land together (atomic chain)"

        # Re-run same fingerprint (reclaimed crash) -> existing decision, NO duplicate trade.
        dec_id2, inserted2 = persist()
        assert dec_id2 == dec_id and inserted2 is False
        with psycopg.connect(DSN) as c:
            n = c.execute("SELECT count(*) FROM trades WHERE decision_id=%s", (dec_id,)).fetchone()[0]
        assert n == 1, "a reclaimed re-run must not open a second trade"
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_llm_call_records_retry_count_and_links_the_decision():
    """Pay-per-token audit: a call that RETRIED before succeeding records how many retries, and a
    successful call is tied to the decision it produced; a failed call is logged unlinked."""
    from database.repository import insert_llm_call, upsert_snapshot
    from decision.llm_client import LlmCallResult

    sym, run_id = "TST_" + os.urandom(3).hex(), "llm-" + os.urandom(3).hex()
    try:
        _, snap_id = upsert_snapshot(DSN, _packet(sym, spread=0.02))
        dec_id = _seed_decision(sym, snapshot_id=snap_id, run_id=run_id, input_fingerprint="llmfp")
        ok = LlmCallResult(ok=True, requested_model="claude-sonnet-5", request_id="r",
                           input_tokens=900, output_tokens=200, estimated_cost_usd=0.004,
                           latency_ms=800, retry_count=2, input_hash="h")
        fail = LlmCallResult(ok=False, error="exhausted_retries:RateLimitError",
                             requested_model="claude-sonnet-5", retry_count=3, input_hash="h")
        ok_id = insert_llm_call(DSN, ok, snapshot_id=snap_id, decision_id=dec_id)
        fail_id = insert_llm_call(DSN, fail, snapshot_id=snap_id)   # no decision -> unlinked
        with psycopg.connect(DSN) as c:
            got = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT id, retry_count, decision_id FROM llm_calls WHERE id = ANY(%s)",
                ([ok_id, fail_id],)).fetchall()}
        assert got[ok_id] == (2, dec_id), "successful call: retry_count + linked decision"
        assert got[fail_id] == (3, None), "failed call: retry_count recorded, decision NULL"
    finally:
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("DELETE FROM llm_calls WHERE snapshot_id=%s", (snap_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.commit()
        _cleanup(sym)


def test_feedback_is_as_of_safe_only_trades_closed_before_the_decision():
    """Feedback loop (Faza 5) must NEVER leak the future: a decision at `as_of` may only see
    trades that CLOSED strictly before `as_of`. A trade that closes AFTER it contributes nothing
    to regime stats or the recent-trades list."""
    from core.models import Direction
    from database.feedback import build_feedback, recent_closed_trades, regime_performance
    from database.repository import upsert_shadow_trade
    from shadow.reconciler import Outcome
    from shadow.virtual_broker import open_virtual_trade

    sym, run_id = "TST_" + os.urandom(3).hex(), "fb-" + os.urandom(3).hex()
    as_of = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    try:
        # Two closed trades on the same run: one closed BEFORE as_of, one AFTER.
        for i, (closed_at, r) in enumerate([(as_of - timedelta(hours=2), 0.8),
                                            (as_of + timedelta(hours=2), -1.0)]):
            dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint=f"fb{i}")
            trade = open_virtual_trade(Direction.BUY, 4000.0, 0.3, 0.6, spread_pct=0.0,
                                       spread_provenance="modeled", opened_at=closed_at - timedelta(hours=1))
            outcome = Outcome(status="closed", exit_reason="tp_hit" if r > 0 else "sl_hit",
                              exit_price=4000.0, closed_at=closed_at, r_multiple=r,
                              r_pessimistic=r, r_optimistic=r)
            upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                                outcome=outcome, timeframe="15min", timeout_bars=96)

        fb = build_feedback(DSN, run_id=run_id, before=as_of)
        assert len(fb["recent_trades"]) == 1, "only the trade closed before as_of is visible"
        assert fb["recent_trades"][0]["r_multiple"] == 0.8
        regimes = regime_performance(DSN, run_id=run_id, before=as_of)
        assert sum(int(r["trades"]) for r in regimes) == 1   # the future trade is excluded
        # Far enough in the future, BOTH are visible.
        later = build_feedback(DSN, run_id=run_id, before=as_of + timedelta(hours=5))
        assert len(later["recent_trades"]) == 2
        assert len(recent_closed_trades(DSN, run_id=run_id, before=as_of, k=5)) == 1
    finally:
        with psycopg.connect(DSN) as c:
            c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
            c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
            c.commit()
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
