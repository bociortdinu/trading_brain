"""Redacted dump of the effective settings — to verify what config a (containerized) process
actually resolved.

    python -m config.dump

Secrets (API keys, DSN passwords) are masked. Prints deterministic key-sorted JSON.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from config.settings import load_settings

_SECRET_TOKENS = ("key", "secret", "token", "password")


def _redact(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name.endswith("dsn") and isinstance(value, str):
        # postgresql://user:PASSWORD@host/db -> mask the password only.
        return re.sub(r"(://[^:@/]+:)[^@]+(@)", r"\1***\2", value)
    if any(tok in name for tok in _SECRET_TOKENS) and isinstance(value, str) and value:
        return "***"
    return value


def redacted_settings() -> dict[str, Any]:
    s = load_settings()
    data = s.model_dump(mode="json")
    return {k: _redact(k.lower(), v) for k, v in sorted(data.items())}


def main() -> int:
    json.dump(redacted_settings(), sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
