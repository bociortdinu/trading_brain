"""Fail-closed Anthropic decision client. No real API calls: the SDK client is replaced by
a fake whose messages.parse returns crafted responses or raises the REAL anthropic error
types, so retry/fail-closed paths are exercised deterministically."""

from __future__ import annotations

import types
from datetime import datetime, timezone

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from decision.llm_client import AnthropicDecisionMaker, LlmDecisionError  # noqa: E402
from decision.schema import DecisionInput, DecisionOutput  # noqa: E402
from tests.helpers import run  # noqa: E402

UTC = timezone.utc
_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _inp():
    return DecisionInput(symbol="GOLD", as_of=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
                         mode="replay", price=4100.0, regime_h1="bull_trend", macro_bias="up",
                         major_trend="bull_trend", confluence="aligned_bull",
                         feature_pipeline_version="1.2.0")


def _resp(*, stop_reason="end_turn", parsed=None, model="claude-sonnet-5"):
    usage = types.SimpleNamespace(input_tokens=1200, output_tokens=40,
                                  cache_read_input_tokens=1000, cache_creation_input_tokens=0)
    return types.SimpleNamespace(parsed_output=parsed, model=model, _request_id="req_123",
                                 stop_reason=stop_reason, usage=usage)


class _FakeMessages:
    def __init__(self, behavior):
        self._b, self.calls = behavior, 0

    async def parse(self, **kw):
        self.calls += 1
        return self._b(self.calls)


class _FakeClient:
    def __init__(self, behavior):
        self.messages = _FakeMessages(behavior)

    async def close(self):
        pass


def _maker(behavior, **kw):
    m = AnthropicDecisionMaker("test-key", sleep_fn=_noop_sleep, **kw)
    m._client = _FakeClient(behavior)
    return m


async def _noop_sleep(_s):
    return None


# --------------------------------------------------------------------------- #
def test_success_returns_validated_output_and_manifest():
    good = DecisionOutput(direction="BUY", confidence=0.7, rationale="mtf aligned")
    m = _maker(lambda n: _resp(parsed=good))
    res = run(m.call(_inp()))
    assert res.ok and res.output.direction.value == "BUY"
    assert res.effective_model == "claude-sonnet-5" and res.request_id == "req_123"
    assert res.input_tokens == 1200 and res.cache_read_input_tokens == 1000
    assert res.estimated_cost_usd is not None and res.input_hash


def test_refusal_fails_closed():
    m = _maker(lambda n: _resp(stop_reason="refusal", parsed=None))
    res = run(m.call(_inp()))
    assert not res.ok and res.error == "refusal"


def test_max_tokens_fails_closed():
    good = DecisionOutput(direction="BUY", confidence=0.7, rationale="x")
    m = _maker(lambda n: _resp(stop_reason="max_tokens", parsed=good))
    res = run(m.call(_inp()))
    assert not res.ok and res.error == "max_tokens_truncated"


def test_missing_parsed_output_fails_closed():
    m = _maker(lambda n: _resp(parsed=None))
    res = run(m.call(_inp()))
    assert not res.ok and res.error == "no_parsed_output"


def test_rate_limit_retried_then_success():
    good = DecisionOutput(direction="SELL", confidence=0.8, rationale="x")

    def behavior(n):
        if n == 1:
            raise anthropic.RateLimitError("429", response=httpx.Response(429, request=_REQ), body=None)
        return _resp(parsed=good)

    m = _maker(behavior, max_retries=2)
    res = run(m.call(_inp()))
    assert res.ok and res.output.direction.value == "SELL"
    assert res.retry_count == 1   # one 429 retry before success -> recorded for the cost audit


def test_client_4xx_not_retried():
    calls = {"n": 0}

    def behavior(n):
        calls["n"] = n
        raise anthropic.APIStatusError("bad", response=httpx.Response(400, request=_REQ), body=None)

    m = _maker(behavior, max_retries=3)
    res = run(m.call(_inp()))
    assert not res.ok and res.error == "api_status:400" and calls["n"] == 1  # no retry


def test_server_5xx_retried_then_exhausted():
    def behavior(n):
        raise anthropic.APIStatusError("boom", response=httpx.Response(503, request=_REQ), body=None)

    m = _maker(behavior, max_retries=2)
    res = run(m.call(_inp()))
    assert not res.ok and "api_status:503" in res.error
    assert res.retry_count == 2   # exhausted all retries -> recorded


def test_timeout_retried_then_exhausted():
    def behavior(n):
        raise anthropic.APITimeoutError(request=_REQ)

    m = _maker(behavior, max_retries=1)
    res = run(m.call(_inp()))
    assert not res.ok and res.error.startswith("exhausted_retries")


def test_decide_raises_on_failure():
    m = _maker(lambda n: _resp(stop_reason="refusal", parsed=None))
    with pytest.raises(LlmDecisionError):
        run(m.decide(_inp()))
