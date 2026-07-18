"""P0-5: git provenance is build-injected in containers, and UNKNOWN is never read as clean."""

from __future__ import annotations


def test_git_metadata_uses_build_injected_env(monkeypatch):
    from database.operations import git_metadata
    monkeypatch.setenv("BRAIN_GIT_COMMIT", "abc123")
    monkeypatch.setenv("BRAIN_GIT_BRANCH", "main")
    monkeypatch.setenv("BRAIN_GIT_DIRTY", "false")
    m = git_metadata()
    assert m["git_commit"] == "abc123" and m["git_branch"] == "main"
    assert m["git_dirty"] is False and m["git_provenance"] == "build"


def test_git_metadata_dirty_unknown_is_none_not_false(monkeypatch):
    from database.operations import git_metadata
    monkeypatch.setenv("BRAIN_GIT_COMMIT", "abc123")
    monkeypatch.setenv("BRAIN_GIT_DIRTY", "unknown")
    assert git_metadata()["git_dirty"] is None      # unknown, NOT clean/false


def test_dashboard_git_state_is_unknown_without_git_or_build_env(monkeypatch):
    from dashboard.service import DashboardService
    monkeypatch.delenv("BRAIN_GIT_COMMIT", raising=False)
    svc = DashboardService.__new__(DashboardService)
    svc.repo_root = "/nonexistent-no-git-xyz"
    st = svc._git_state()
    assert st["state"] == "unknown" and not st.get("ok")    # never silently 'clean'


def test_dashboard_git_state_uses_build_env_when_git_absent(monkeypatch):
    from dashboard.service import DashboardService
    monkeypatch.setenv("BRAIN_GIT_COMMIT", "deadbee")
    monkeypatch.setenv("BRAIN_GIT_DIRTY", "false")
    svc = DashboardService.__new__(DashboardService)
    svc.repo_root = "/nonexistent-no-git-xyz"
    st = svc._git_state()
    assert st["state"] == "clean" and st["commit"] == "deadbee" and st.get("provenance") == "build"
