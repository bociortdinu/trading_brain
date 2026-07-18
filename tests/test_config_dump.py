"""P0-6: a set BRAIN_* setting is reflected in the effective config, and the dump redacts secrets."""

from __future__ import annotations


def test_config_dump_reflects_env_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("BRAIN_RECONCILE_TIMEFRAME", "1min")
    monkeypatch.setenv("BRAIN_ANTHROPIC_API_KEY", "sk-secret-should-not-leak")
    monkeypatch.setenv("BRAIN_DB_DSN", "postgresql://u:hunter2@h:5433/trading_brain")
    from config.dump import redacted_settings
    d = redacted_settings()
    assert d["reconcile_timeframe"] == "1min"                 # a set BRAIN_* reaches the config
    assert d["anthropic_api_key"] == "***"                    # secret masked
    assert "hunter2" not in d["db_dsn"] and "***" in d["db_dsn"]   # DSN password masked, host kept
    assert d["db_dsn"].endswith("/trading_brain")
