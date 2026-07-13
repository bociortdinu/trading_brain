"""Packaging guards.

Regression: `decision`/`risk` were importable from the project cwd but MISSING from
[tool.setuptools.packages.find], so `pip install -e .` in a clean venv omitted them and
`import decision` raised ModuleNotFoundError from any other cwd. This test fails if ANY
top-level importable package is not covered by an include pattern, and a subprocess
import check proves the declared packages import with the repo root on the path (the
clean-install scenario), from a cwd that is NOT the repo root.
"""

from __future__ import annotations

import fnmatch
import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_NON_PACKAGE = {"tests", "docs", "scratchpad", "build", "dist"}


def _declared_patterns() -> list[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return cfg["tool"]["setuptools"]["packages"]["find"]["include"]


def _top_level_packages() -> list[str]:
    out = []
    for child in ROOT.iterdir():
        if child.is_dir() and (child / "__init__.py").exists() and child.name not in _NON_PACKAGE:
            if not child.name.startswith(".") and not child.name.endswith(".egg-info"):
                out.append(child.name)
    return sorted(out)


def test_every_top_level_package_is_declared():
    patterns = _declared_patterns()
    missing = [p for p in _top_level_packages()
               if not any(fnmatch.fnmatch(p, pat) for pat in patterns)]
    assert not missing, f"packages missing from setuptools packages.find include: {missing}"


def test_decision_and_risk_declared():
    patterns = _declared_patterns()
    for pkg in ("decision", "risk"):
        assert any(fnmatch.fnmatch(pkg, pat) for pat in patterns), f"{pkg} not declared"


def test_declared_packages_import_from_foreign_cwd():
    # Simulate an installed package: repo root on sys.path, cwd elsewhere (/).
    code = "import decision, risk, decision.pipeline, decision.schema, risk.engine; print('ok')"
    res = subprocess.run(
        [sys.executable, "-c", code], cwd="/", env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr


def test_anthropic_sdk_supports_parse():
    # `anthropic>=0.40` is NOT proof of Structured Outputs; assert the ACTUAL installed SDK
    # exposes messages.parse + the error types the client depends on, and meets the floor.
    import pytest

    anthropic = pytest.importorskip("anthropic")
    floor = tuple(int(x) for x in "0.116".split("."))
    have = tuple(int(x) for x in anthropic.__version__.split(".")[:2])
    assert have >= floor, f"anthropic {anthropic.__version__} < pinned floor 0.116"
    client = anthropic.Anthropic(api_key="test-not-used")
    assert hasattr(client.messages, "parse"), "SDK lacks messages.parse (Structured Outputs)"
    for name in ("RateLimitError", "APIStatusError", "APIConnectionError", "APITimeoutError"):
        assert hasattr(anthropic, name), f"SDK lacks anthropic.{name}"
