"""R2-4/R2-13: docker-compose credential scoping — the admin DSN and API keys reach ONLY the
services that need them, and no admin password is blanket-injected. Verified against the RESOLVED
`docker compose config` (client-side; skips when docker/compose is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _resolved(tmp_path, profile="data"):
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    yaml = pytest.importorskip("yaml")
    env = tmp_path / "env.compose"
    env.write_text("POSTGRES_PASSWORD=adminpw\nBRAIN_APP_PASSWORD=apppw\n"
                   "BRAIN_POLYGON_API_KEY=polykey\nBRAIN_ANTHROPIC_API_KEY=anthkey\n")
    cmd = ["docker", "compose", "--env-file", str(env), "--profile", profile, "config"]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"docker compose config unavailable: {r.stderr[:160]}")
    return yaml.safe_load(r.stdout)


def _env(cfg, svc):
    return cfg["services"][svc].get("environment") or {}


def test_admin_dsn_reaches_only_migrate(tmp_path):
    cfg = _resolved(tmp_path)
    assert "BRAIN_ADMIN_DB_DSN" in _env(cfg, "migrate")
    for svc in ("dashboard", "collector", "online"):
        assert "BRAIN_ADMIN_DB_DSN" not in _env(cfg, svc), f"{svc} must not receive the admin DSN"


def test_api_keys_are_scoped_to_their_service(tmp_path):
    cfg = _resolved(tmp_path)
    assert "BRAIN_ANTHROPIC_API_KEY" in _env(cfg, "online")
    for svc in ("dashboard", "collector", "migrate"):
        assert "BRAIN_ANTHROPIC_API_KEY" not in _env(cfg, svc)
    assert "BRAIN_POLYGON_API_KEY" not in _env(cfg, "dashboard")   # dashboard needs no market key


def test_no_admin_password_is_blanket_injected(tmp_path):
    cfg = _resolved(tmp_path)
    for svc in ("dashboard", "collector", "online", "migrate"):
        assert "POSTGRES_PASSWORD" not in _env(cfg, svc), f"{svc} must not receive POSTGRES_PASSWORD"


def test_every_app_service_gets_the_app_dsn(tmp_path):
    cfg = _resolved(tmp_path)
    for svc in ("dashboard", "collector", "online", "migrate"):
        assert "BRAIN_DB_DSN" in _env(cfg, svc)
