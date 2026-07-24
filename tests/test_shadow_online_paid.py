"""Paid guards on the shadow-online loop.

This loop is long-lived, so an unguarded paid maker means unbounded spend. These tests pin the
guards that make `--maker claude` safe to enable at all: the cap actually stops the loop, the
deadline actually ends it, and the Anthropic client is closed on every exit path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shadow.online import CallCapReached, _calls_made, _close_maker, _build_maker
from tests.helpers import run


class _Client:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _Maker:
    """Stands in for the gateway-backed paid maker: holds a client and counts calls."""

    def __init__(self):
        self.inner = _Client()
        self.calls = 0


def test_close_maker_closes_the_wrapped_client():
    """The maker is wrapped in _CountingMaker, so the client to close is the INNER one."""
    m = _Maker()
    run(_close_maker(m))
    assert m.inner.closed is True


def test_close_maker_tolerates_a_free_maker():
    class _Free:
        pass

    run(_close_maker(_Free()))   # must not raise — the deterministic maker holds no client


def test_calls_made_defaults_to_zero_for_a_free_maker():
    class _Free:
        pass

    assert _calls_made(_Free()) == 0


def test_deterministic_maker_is_free_and_needs_no_run_id():
    maker, name, is_paid = _build_maker(_settings(), "deterministic")
    assert is_paid is False and name == "deterministic-confluence"


def _settings(**kw):
    from config.settings import Settings

    base = dict(db_dsn="postgresql://u:p@127.0.0.1:5432/x", decision_model="claude-sonnet-5")
    base.update(kw)
    return Settings(**base)


def test_paid_maker_refuses_without_persistence():
    """Every paid call must land in the audit ledger, so no run_id/DSN means no paid maker."""
    with pytest.raises(SystemExit, match="audit ledger"):
        _build_maker(_settings(), "claude", run_id=None, persist_dsn=None)


def test_unknown_maker_is_rejected():
    with pytest.raises(SystemExit, match="unknown --maker"):
        _build_maker(_settings(), "gpt")


# ---- loop bounds ---- #
class _StopTick(BaseException):
    """Deliberately a BaseException. The loop catches `Exception` on purpose so a tick failure
    does not kill a long-running session — a RuntimeError here would be swallowed and the test
    would spin forever instead of exercising the error path."""


def _loop_with(monkeypatch, maker, *, max_llm_calls=None, deadline=None, ticks_before_fail=99):
    """Drive _loop with the tick and sleep stubbed out, so the bound logic is what is measured."""
    import shadow.online as online

    state = {"ticks": 0}

    async def _tick(*a, **kw):
        state["ticks"] += 1
        if state["ticks"] > ticks_before_fail:
            raise _StopTick("ran too long")
        maker.calls += 1
        return {"as_of": "x"}

    async def _sleep(_):
        return None

    monkeypatch.setattr(online, "shadow_tick_with_retries", _tick)
    monkeypatch.setattr(online.asyncio, "sleep", _sleep)
    monkeypatch.setattr(online, "build_provider", lambda s: _Client())
    monkeypatch.setattr(online, "OperationalTelemetry",
                        lambda *a, **kw: type("T", (), {"heartbeat": lambda *a, **k: None,
                                                        "stop": lambda *a: None})())
    monkeypatch.setattr(online, "_record_processed_and_downtime", lambda *a, **kw: None)
    run(online._loop(_settings(), "run-1", maker, "claude-sonnet-5",
                     offset_seconds=0.0, max_llm_calls=max_llm_calls, deadline=deadline))
    return state["ticks"]


def test_call_cap_stops_the_loop(monkeypatch):
    """The money guarantee: the loop must stop at the cap, not merely report it."""
    maker = _Maker()
    ticks = _loop_with(monkeypatch, maker, max_llm_calls=3, ticks_before_fail=10)
    assert maker.calls == 3 and ticks == 3


def test_cap_is_checked_before_the_tick_not_mid_decision(monkeypatch):
    """Stopping mid-decision would leave a paid call without its persisted chain."""
    maker = _Maker()
    maker.calls = 5                       # already at the cap before the first tick
    ticks = _loop_with(monkeypatch, maker, max_llm_calls=5, ticks_before_fail=10)
    assert ticks == 0                     # never entered a tick


def test_deadline_stops_the_loop(monkeypatch):
    maker = _Maker()
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    ticks = _loop_with(monkeypatch, maker, deadline=past, ticks_before_fail=10)
    assert ticks == 0


def test_client_is_closed_when_the_cap_ends_the_loop(monkeypatch):
    maker = _Maker()
    _loop_with(monkeypatch, maker, max_llm_calls=2, ticks_before_fail=10)
    assert maker.inner.closed is True


def test_client_is_closed_even_when_the_loop_raises(monkeypatch):
    """A leaked client on the error path is the case that actually bites in a long-lived loop."""
    maker = _Maker()
    with pytest.raises(_StopTick):
        _loop_with(monkeypatch, maker, max_llm_calls=99, ticks_before_fail=0)
    assert maker.inner.closed is True


def test_a_tick_error_does_not_kill_the_loop(monkeypatch):
    """The other half of the contract: an ordinary tick failure is logged and the session
    continues — a transient provider blip must not end a paid window early."""
    import shadow.online as online

    maker = _Maker()
    state = {"ticks": 0}

    async def _tick(*a, **kw):
        state["ticks"] += 1
        maker.calls += 1
        if state["ticks"] == 1:
            raise RuntimeError("transient")
        return {"as_of": "x"}

    async def _sleep(_):
        return None

    monkeypatch.setattr(online, "shadow_tick_with_retries", _tick)
    monkeypatch.setattr(online.asyncio, "sleep", _sleep)
    monkeypatch.setattr(online, "build_provider", lambda s: _Client())
    monkeypatch.setattr(online, "OperationalTelemetry",
                        lambda *a, **kw: type("T", (), {"heartbeat": lambda *a, **k: None,
                                                        "stop": lambda *a: None})())
    monkeypatch.setattr(online, "_record_processed_and_downtime", lambda *a, **kw: None)
    run(online._loop(_settings(), "run-1", maker, "claude-sonnet-5",
                     offset_seconds=0.0, max_llm_calls=3))
    assert state["ticks"] == 3            # survived the failure and kept going to the cap
