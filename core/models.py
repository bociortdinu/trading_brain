"""Internal decision vocabulary, kept SEPARATE from the external trading_hands contract.

- Internal (the brain speaks this): Direction = BUY | SELL | NO_TRADE.
- External (trading_hands expects this, case-sensitive): "Buy" | "Sell" | "NoAction".

Mapping happens in exactly one place (`to_external_model_type`); nothing else should
hardcode the external strings.
"""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


_EXTERNAL_MODEL_TYPE: dict[Direction, str] = {
    Direction.BUY: "Buy",
    Direction.SELL: "Sell",
    Direction.NO_TRADE: "NoAction",
}


def to_external_model_type(direction: Direction) -> str:
    """Map an internal Direction to the trading_hands `model_type` string."""
    return _EXTERNAL_MODEL_TYPE[direction]
