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


# --- finer-bar (M1) reconciliation fetch: best-effort, degrades to the trigger TF --------------
def test_resolve_run_id_prefers_cli_then_env_then_config_digest_default():
    """R2-5/P0-7: CLI wins, else BRAIN_RUN_ID, else a default that embeds the strategy version AND
    a CONFIG DIGEST — so a cost/RiskConfig/eligibility/calendar change forces a new run."""
    from config.settings import Settings
    from decision.schema import STRATEGY_VERSION
    from shadow.online import resolve_run_id
    assert resolve_run_id("cli-x", Settings(run_id="env-y")) == "cli-x"
    assert resolve_run_id(None, Settings(run_id="env-y")) == "env-y"
    default = resolve_run_id(None, Settings(run_id=None))
    assert default.startswith(f"shadow-online-{STRATEGY_VERSION}-")
    # a cost change must change the default (was static before)
    assert resolve_run_id(None, Settings(run_id=None)) != resolve_run_id(None, Settings(commission_pct=0.05))


def test_fetch_finer_bars_falls_back_to_empty_on_provider_error():
    """A provider that cannot serve M1 must not crash the tick — the caller then reconciles at the
    trigger timeframe and flags the fallback."""
    from datetime import datetime, timezone
    from shadow.online import _fetch_finer_bars

    class BadProvider:
        async def get_ohlcv(self, symbol, tf, count):
            raise ProviderError("provider has no M1")

    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert run(_fetch_finer_bars(BadProvider(), "GOLD", "1min", 96, now)) == []


def test_fetch_finer_bars_returns_only_closed_bars():
    from datetime import datetime, timedelta, timezone
    from data_collector.providers.base import Candle
    from shadow.online import _fetch_finer_bars

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    closed = Candle(open_time=now - timedelta(minutes=2), close_time=now - timedelta(minutes=1),
                    open=1, high=1, low=1, close=1, volume=1.0)
    forming = Candle(open_time=now, close_time=now + timedelta(minutes=1),
                     open=1, high=1, low=1, close=1, volume=1.0)   # not closed yet

    class GoodProvider:
        async def get_ohlcv(self, symbol, tf, count):
            assert tf == "1min"
            return [closed, forming]

    bars = run(_fetch_finer_bars(GoodProvider(), "GOLD", "1min", 10, now))
    assert closed in bars and forming not in bars     # the still-forming bar is dropped
