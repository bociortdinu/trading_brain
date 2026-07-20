"""Central financial gateway for PAID AI calls — exercised with a FAKE transport, so NO real
Anthropic request is ever made. Proves: the gate/allowlist/run_id refusals, the atomic budget
reservation (fail-closed, no request when there is no budget), and the pre-attempt audit ledger
(started -> completed/timeout/error) with budget accounting derived from it."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from tests.helpers import run

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DB_DSN")


def _db_ok() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="no reachable BRAIN_TEST_DB_DSN (..._test)")


def _settings(*, enabled=True, budgets=(1000.0, 1000.0, 1000.0), model="claude-haiku-4-5",
              allow=("claude-haiku-4-5",), attempts=1):
    return SimpleNamespace(
        paid_ai_enabled=enabled, anthropic_api_key="sk-test", decision_model=model,
        paid_ai_model_allowlist=list(allow), decision_max_tokens=1024,
        paid_max_http_attempts=attempts, paid_budget_run_usd=budgets[0],
        paid_budget_day_usd=budgets[1], paid_budget_month_usd=budgets[2], db_dsn=DSN)


class _FakeInput:
    def input_hash(self):
        return "fixed-input-hash"

    def model_dump_json(self):
        return '{"snapshot": "frozen"}'


def _result(*, ok=True, error=None, cost=0.001, request_id="req_fake"):
    from decision.llm_client import LlmCallResult
    from decision.schema import DecisionOutput
    out = DecisionOutput(direction="BUY", confidence=0.8, rationale="x") if ok else None
    return LlmCallResult(ok=ok, error=error, output=out, requested_model="claude-haiku-4-5",
                         input_hash="fixed-input-hash", request_id=request_id, input_tokens=120,
                         output_tokens=40, estimated_cost_usd=cost)


class _FakeInner:
    """Stands in for AnthropicDecisionMaker: returns a canned result, never touches the network."""
    def __init__(self, result):
        self._result = result
        self.last_result = None
        self.called = 0
        self.closed = False

    async def call(self, inp):
        self.called += 1
        self.last_result = self._result
        return self._result

    async def aclose(self):
        self.closed = True


def _gw(settings, inner, run_id):
    from decision.paid_gateway import PaidAiGateway
    return PaidAiGateway(settings, run_id=run_id, persist_dsn=DSN, context="test", inner=inner)


def _rows(run_id):
    with psycopg.connect(DSN) as c:
        return c.execute("SELECT status, actual_cost_usd, est_cost_usd FROM paid_attempts "
                         "WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()


def _cleanup(run_id):
    admin = os.environ.get("BRAIN_TEST_ADMIN_DB_DSN") or DSN
    with psycopg.connect(admin) as c:
        c.execute("DELETE FROM paid_attempts WHERE run_id=%s", (run_id,))
        c.commit()


# --- construction-time refusals (no request even attempted) ------------------------------------
def test_gateway_refuses_when_gate_off():
    from decision.paid_guard import PaidAiDisabled
    with pytest.raises(PaidAiDisabled):
        _gw(_settings(enabled=False), _FakeInner(_result()), "r-off")


def test_gateway_refuses_a_model_not_on_the_allowlist():
    with pytest.raises(SystemExit):
        _gw(_settings(model="claude-opus-4-8", allow=("claude-haiku-4-5",)),
            _FakeInner(_result()), "r-model")


def test_gateway_requires_run_id_and_persistence():
    from decision.paid_gateway import PaidAiGateway
    with pytest.raises(SystemExit):
        PaidAiGateway(_settings(), run_id="", persist_dsn=DSN, context="test",
                      inner=_FakeInner(_result()))
    with pytest.raises(SystemExit):
        PaidAiGateway(_settings(), run_id="r", persist_dsn="", context="test",
                      inner=_FakeInner(_result()))


# --- budget reservation (fail-closed) -----------------------------------------------------------
def test_zero_budget_refuses_before_any_request():
    from database.repository import BudgetExceeded
    run_id = "r-zero-" + os.urandom(3).hex()
    inner = _FakeInner(_result())
    gw = _gw(_settings(budgets=(0.0, 0.0, 0.0)), inner, run_id)
    try:
        with pytest.raises(BudgetExceeded):
            run(gw.call(_FakeInput()))
        assert inner.called == 0                       # NO request made
        assert _rows(run_id) == []                     # reservation rolled back, no 'started' orphan
    finally:
        _cleanup(run_id)


def test_tiny_budget_refuses_and_never_calls():
    from database.repository import BudgetExceeded
    run_id = "r-tiny-" + os.urandom(3).hex()
    inner = _FakeInner(_result())
    gw = _gw(_settings(budgets=(0.0001, 1000.0, 1000.0)), inner, run_id)   # est >> 0.0001
    try:
        with pytest.raises(BudgetExceeded):
            run(gw.call(_FakeInput()))
        assert inner.called == 0
    finally:
        _cleanup(run_id)


# --- pre-attempt audit + outcomes ---------------------------------------------------------------
def test_successful_call_records_a_completed_attempt_with_actual_cost():
    from database.repository import paid_spend_summary
    run_id = "r-ok-" + os.urandom(3).hex()
    inner = _FakeInner(_result(ok=True, cost=0.001))
    gw = _gw(_settings(), inner, run_id)
    try:
        out = run(gw.decide(_FakeInput()))
        assert out.direction.value == "BUY" and inner.called == 1
        rows = _rows(run_id)
        assert len(rows) == 1 and rows[0][0] == "completed"
        assert float(rows[0][1]) == pytest.approx(0.001)          # actual cost recorded
        assert paid_spend_summary(DSN, run_id)["run"] == pytest.approx(0.001)   # accounting uses actual
    finally:
        _cleanup(run_id)


def test_timeout_result_is_recorded_as_timeout():
    run_id = "r-to-" + os.urandom(3).hex()
    inner = _FakeInner(_result(ok=False, error="exhausted_retries:APITimeoutError", cost=None))
    gw = _gw(_settings(), inner, run_id)
    try:
        with pytest.raises(Exception):
            run(gw.decide(_FakeInput()))
        rows = _rows(run_id)
        assert len(rows) == 1 and rows[0][0] == "timeout"        # cost unknown -> flagged, not lost
    finally:
        _cleanup(run_id)


def test_error_result_is_recorded_and_excluded_from_spend():
    from database.repository import paid_spend_summary
    run_id = "r-err-" + os.urandom(3).hex()
    inner = _FakeInner(_result(ok=False, error="api_status:400", cost=None))
    gw = _gw(_settings(), inner, run_id)
    try:
        with pytest.raises(Exception):
            run(gw.decide(_FakeInput()))
        rows = _rows(run_id)
        assert len(rows) == 1 and rows[0][0] == "error"
        assert paid_spend_summary(DSN, run_id)["run"] == 0.0     # 'error' rows don't count as spend
    finally:
        _cleanup(run_id)


# --- operator reconciliation + orphan sweep -----------------------------------------------------
def _seed_completed(run_id, cost=0.002):
    from database.repository import finalize_paid_attempt, reserve_paid_attempt
    aid = reserve_paid_attempt(DSN, run_id=run_id, context="test", model="claude-haiku-4-5",
                               input_hash="h-" + os.urandom(2).hex(), attempt_no=0,
                               est_cost_usd=0.005, budget_run_usd=1000.0, budget_day_usd=1e9,
                               budget_month_usd=1e9)
    finalize_paid_attempt(DSN, attempt_id=aid, status="completed", request_id="req", input_tokens=10,
                          output_tokens=5, actual_cost_usd=cost)
    return aid


def test_console_reconciliation_workflow():
    from database.repository import (
        mark_paid_attempt_reconciled, paid_attempts_needing_reconciliation,
    )
    run_id = "r-rec-" + os.urandom(3).hex()
    try:
        aid = _seed_completed(run_id)
        unrec = paid_attempts_needing_reconciliation(DSN, run_id)
        assert len(unrec) == 1 and unrec[0]["id"] == aid
        assert mark_paid_attempt_reconciled(DSN, aid) == 1
        assert paid_attempts_needing_reconciliation(DSN, run_id) == []
        assert mark_paid_attempt_reconciled(DSN, aid) == 0        # already reconciled -> no-op
    finally:
        _cleanup(run_id)


def test_orphan_started_attempts_are_swept_to_unknown():
    from database.repository import stale_started_attempts, sweep_started_attempts_to_unknown
    run_id = "r-orph-" + os.urandom(3).hex()
    try:
        with psycopg.connect(DSN) as c:      # an OLD 'started' row (process died mid-request)
            c.execute("INSERT INTO paid_attempts (run_id, context, model, input_hash, attempt_no, "
                      "status, est_cost_usd, started_at) VALUES (%s,'test','claude-haiku-4-5','h',0,"
                      "'started', 0.003, now() - interval '10 minutes')", (run_id,))
            c.commit()
        assert any(r["run_id"] == run_id for r in stale_started_attempts(DSN, older_than_seconds=300))
        assert sweep_started_attempts_to_unknown(DSN, older_than_seconds=300) >= 1
        with psycopg.connect(DSN) as c:
            st = c.execute("SELECT status FROM paid_attempts WHERE run_id=%s", (run_id,)).fetchone()[0]
        assert st == "unknown"               # flagged as outcome-uncertain, not silently pending
    finally:
        _cleanup(run_id)
