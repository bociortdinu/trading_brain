"""Typed config guards. market_mode is strict online|replay — a typo must be rejected
at load time, not silently coerced to a default."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_valid_modes_accepted():
    assert Settings(_env_file=None, market_mode="online").market_mode == "online"
    assert Settings(_env_file=None, market_mode="replay").market_mode == "replay"


def test_typo_mode_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, market_mode="onlien")  # typo must fail closed


def test_empty_mode_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, market_mode="")
