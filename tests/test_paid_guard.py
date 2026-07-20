"""Fail-closed gate for PAID Anthropic calls. Proves EVERY money-spending entry point refuses the
call when BRAIN_PAID_AI_ENABLED is off — a configured API key alone must never be enough to spend.
No real API call is ever made (the refusal happens before any maker/client is used)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from decision.paid_guard import (
    PaidAiDisabled,
    confirm_paid_call,
    require_paid_ai_enabled,
)


def _settings(*, enabled: bool, key: str | None = "sk-test"):
    return SimpleNamespace(paid_ai_enabled=enabled, anthropic_api_key=key,
                           decision_model="claude-haiku-4-5")


# --- the gate itself ---------------------------------------------------------------------------
def test_require_refuses_when_disabled_even_with_a_key():
    with pytest.raises(PaidAiDisabled):
        require_paid_ai_enabled(_settings(enabled=False, key="sk-live"), context="test")


def test_require_refuses_when_key_missing_even_if_enabled():
    with pytest.raises(PaidAiDisabled):
        require_paid_ai_enabled(_settings(enabled=True, key=None), context="test")


def test_require_passes_only_when_enabled_and_key_present():
    require_paid_ai_enabled(_settings(enabled=True, key="sk-live"), context="test")  # no raise


def test_confirm_aborts_on_no_answer_and_proceeds_on_yes(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "")           # empty -> abort
    with pytest.raises(SystemExit):
        confirm_paid_call(context="test", model="m", assume_yes=False)
    confirm_paid_call(context="test", model="m", assume_yes=True)  # --yes -> proceeds, no prompt


# --- each entry point refuses when the gate is OFF (no real call) -------------------------------
def test_runner_build_maker_refuses_claude_when_gate_off():
    from shadow.runner import _build_maker
    with pytest.raises(SystemExit):
        _build_maker(_settings(enabled=False), "claude")
    # the free maker never touches the gate
    maker, name, is_paid = _build_maker(_settings(enabled=False), "deterministic")
    assert is_paid is False


def test_llm_smoke_refuses_when_gate_off(monkeypatch):
    import app.llm_smoke as smoke
    monkeypatch.setattr(smoke, "load_settings", lambda: _settings(enabled=False, key="sk-live"))

    def _boom(*a, **k):
        raise AssertionError("_run must NOT be reached when the gate is off")

    monkeypatch.setattr(smoke, "_run", _boom)
    monkeypatch.setattr("sys.argv", ["app.llm_smoke"])
    with pytest.raises(SystemExit):
        smoke.main()


def test_decide_paid_path_refuses_before_any_data_fetch(monkeypatch):
    import app.decide as decide
    from tests.helpers import run

    def _boom(*a, **k):
        raise AssertionError("build_provider must NOT be reached when the gate is off")

    monkeypatch.setattr(decide, "build_provider", _boom)
    with pytest.raises(SystemExit):
        run(decide._run(_settings(enabled=False, key="sk-live"), mode="replay", use_paid=True))
