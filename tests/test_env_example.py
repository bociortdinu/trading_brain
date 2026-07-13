"""Guard: .env.example must carry ONLY sanctioned EXACT placeholders — never a real secret.

Stricter than fuzzy substring matching: every secret-bearing value (API keys, tokens,
DSN passwords) must be EXACTLY one of the approved placeholder strings below. This catches
both a real secret copied into the template AND placeholder drift (a new secret field added
without a sanctioned placeholder).
"""

from __future__ import annotations

import pathlib
import re

_ENV_EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / ".env.example"

# The ONLY strings allowed in a secret-bearing position in the template.
_EXACT_PLACEHOLDERS = {
    "your-polygon-key-here",
    "your-anthropic-key-here",
    "APP_PASSWORD",
    "ADMIN_PASSWORD",
}
_SECRET_KEY_TOKENS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


def _secret_values() -> list[tuple[str, str]]:
    """(env_key, secret_value) pairs for every secret-bearing line in the template."""
    out: list[tuple[str, str]] = []
    for raw in _ENV_EXAMPLE.read_text().splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = (x.strip() for x in s.split("=", 1))
        ku = key.upper()
        if ku.endswith("DSN"):
            m = re.search(r"://[^:@/]+:([^@]+)@", val)  # user:PASSWORD@host
            if m:
                out.append((key, m.group(1)))
        elif any(t in ku for t in _SECRET_KEY_TOKENS):
            # A numeric value is a budget/count (e.g. MAX_TOKENS=1024), never a secret.
            if not val.isdigit():
                out.append((key, val))
    return out


def test_env_example_uses_only_exact_placeholders():
    offenders = [(k, v) for (k, v) in _secret_values() if v not in _EXACT_PLACEHOLDERS]
    assert not offenders, (
        "secret-bearing fields in .env.example must be exact sanctioned placeholders "
        f"{sorted(_EXACT_PLACEHOLDERS)}; offending fields: {offenders}"
    )


def test_scan_actually_finds_the_secret_fields():
    # Sanity: the scanner must be seeing the key/DSN fields (guards against a no-op pass).
    keys = {k for (k, _) in _secret_values()}
    assert {"BRAIN_ANTHROPIC_API_KEY", "BRAIN_POLYGON_API_KEY", "BRAIN_DB_DSN"} <= keys
