"""Continuous shadow loop resilience without network or database."""

from __future__ import annotations

import pytest

from data_collector.providers.polygon import ProviderError
from shadow.online import shadow_tick_with_retries
from shadow.virtual_broker import ShadowConfig
from tests.helpers import run


def _call(telemetry=None):
    return shadow_tick_with_retries(
        object(), object(), "xtb", decision_maker=object(), run_id="r",
        model_name="deterministic", shadow_config=ShadowConfig(),
        telemetry=telemetry or object(), attempts=3, base_delay_seconds=0,
    )


def test_shadow_tick_retries_transient_and_returns_success(monkeypatch):
    calls = 0

    async def fake_tick(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderError("reconnecting")
        return {"status": "ok"}

    monkeypatch.setattr("shadow.online.observed_shadow_tick", fake_tick)
    assert run(_call()) == {"status": "ok"}
    assert calls == 3


def test_shadow_tick_does_not_retry_programming_error(monkeypatch):
    calls = 0

    async def fake_tick(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise KeyError("bug")

    monkeypatch.setattr("shadow.online.observed_shadow_tick", fake_tick)
    with pytest.raises(KeyError):
        run(_call())
    assert calls == 1
