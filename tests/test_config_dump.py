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


def test_warn_if_env_world_readable(tmp_path):
    import os

    from config.settings import warn_if_env_world_readable
    p = tmp_path / ".env"
    p.write_text("X=1")
    os.chmod(p, 0o600)
    assert warn_if_env_world_readable(str(p)) is None            # 0600 is fine
    os.chmod(p, 0o644)                                           # group/other readable
    w = warn_if_env_world_readable(str(p))
    assert w and "chmod 600" in w
    assert warn_if_env_world_readable(str(tmp_path / "missing")) is None   # absent -> no warning
