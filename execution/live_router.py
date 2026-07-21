"""Live execution router — the ONLY place in trading_brain that sends a real order.

Everything upstream is pure: the risk engine says a verdict is *shadow-eligible*, which means
"the decision passed the deterministic risk rules". That is necessary but not sufficient to
trade, because the remaining gates depend on state the risk engine cannot see — what the broker
account actually holds right now, and how recently we last fired. Those gates live here, next to
the client that can answer them.

Fail-closed everywhere. Every refusal returns a named reason rather than raising, so a skipped
order is an auditable fact and never a silent no-op:

  `router_disabled`            — off unless explicitly enabled (a default may never trade)
  `not_approved` / `missing_sl_tp` — the risk verdict did not approve, or carried no SL/TP
  `volume_out_of_bounds`       — our own requested size is outside the configured cap
  `cooldown`                   — fired too recently
  `trading_disabled`           — trading_hands reports trading_enabled=false; never overridden
  `not_demo`                   — the account is not the demo environment (see `require_demo`)
  `broker_volume_unknown` / `broker_volume_too_large` — see below
  `position_exists` / `max_positions` — the ACCOUNT already holds this, or enough, positions
  `broker_rejected` / `broker_no_action` — the broker declined

Two of these carry the weight:

`position_exists` reads `/positions` immediately before every order. The shadow side tracks its
own `busy_until`, but that is simulated state: after a restart it is empty while the account
still holds the position, and a router trusting it would open a duplicate.

`broker_volume_too_large` exists because trading_hands **ignores the request's `allocation`** and
sizes every order from its own TRADING_VOLUME (verified live 2026-07-21: asked 0.01, got 0.02).
We therefore cannot set position size from here — the only honest control is to refuse when the
broker is configured to trade more than we accept. An unpublished volume (older binary) refuses
too: an unknown position size is not a safe one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from brokers_bridge.trading_hands import (
    ModelSetup,
    PredictionRequest,
    TradeResult,
    TradingHandsClient,
)
from core.models import Direction

ROUTER_VERSION = "live-router-2026.1"


class LiveExecutionConfig(BaseModel):
    """Bounds for live order routing. Every default is the SAFE one: disabled, demo-only, one
    position, a small volume. Turning this on has to be a deliberate act, not a config drift."""

    enabled: bool = False
    require_demo: bool = True          # refuse to route against a non-demo account
    # `volume` is what we ASK for, but trading_hands ignores the request's allocation and sizes
    # every order from its own TRADING_VOLUME (verified live 2026-07-21: asked 0.01, got 0.02).
    # So this cannot set the size — `max_volume` is the gate that matters: if the broker is
    # configured to trade more than we accept, we refuse rather than send an order whose size we
    # do not control. A cap that cannot cap would be worse than none.
    volume: float = Field(0.01, gt=0)
    max_volume: float = Field(0.10, gt=0)
    cooldown_minutes: float = Field(15.0, ge=0)
    max_open_positions: int = Field(1, ge=1)
    version: str = ROUTER_VERSION


class RouteResult(BaseModel):
    """Outcome of one routing attempt. `placed=False` with a `reason` is the normal, expected
    path — most bars are refused by a gate, and each refusal is recorded, not swallowed."""

    placed: bool
    reason: str | None = None
    external_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    volume: float | None = None
    gates_checked: list[str] = Field(default_factory=list)


def build_prediction(*, symbol: str, direction: Direction, sl_pct: float, tp_pct: float,
                     confidence: float, volume: float, as_of: datetime,
                     interval: str = "15min") -> PredictionRequest:
    """Map an approved verdict onto the broker's request shape.

    Two contract details that are easy to get wrong and that the request model enforces:
    `take_profit` carries the DIRECTION in its sign (negative = Sell), and `preds_proba` must be
    >= 0.5 or the broker rejects it. Our confidence is ORDINAL, not a probability, so it is
    clamped rather than rescaled — pretending an ordinal 0.55 is a 55% probability would be
    inventing a claim the model never made.
    """
    is_buy = direction is Direction.BUY
    return PredictionRequest(
        symbol=symbol,
        prediction_date=as_of.date().isoformat(),
        allocation=volume,
        preds_proba=min(1.0, max(0.5, confidence)),
        stop_loss=abs(sl_pct),
        take_profit=abs(tp_pct) if is_buy else -abs(tp_pct),
        model_setup=ModelSetup(forecast_horizon=0, target_change=0,
                               model_type="Buy" if is_buy else "Sell"),
        runtimestamp=as_of.isoformat(),
        sl_tp_logic="percent",
        interval=interval,
        id_model_properties=ROUTER_VERSION,
    )


class LiveRouter:
    """Routes approved verdicts to real orders, subject to the gates above.

    `last_fired_at` is held in memory only. That is deliberate for the cooldown (a restart
    forgetting it costs at most one early entry), but it is exactly why the position gate must
    NOT rely on memory — a restart forgetting an open position costs a duplicate.
    """

    def __init__(self, client: TradingHandsClient, config: LiveExecutionConfig | None = None,
                 *, now_fn=None) -> None:
        self._client = client
        self._config = config or LiveExecutionConfig()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_fired_at: datetime | None = None

    @property
    def config(self) -> LiveExecutionConfig:
        return self._config

    async def route(self, *, symbol: str, direction: Direction, approved: bool,
                    sl_pct: float | None, tp_pct: float | None, confidence: float,
                    as_of: datetime) -> RouteResult:
        gates: list[str] = []

        def refuse(reason: str) -> RouteResult:
            return RouteResult(placed=False, reason=reason, gates_checked=gates)

        gates.append("router_enabled")
        if not self._config.enabled:
            return refuse("router_disabled")

        gates.append("risk_approved")
        if not approved or direction is Direction.NO_TRADE:
            return refuse("not_approved")
        if sl_pct is None or tp_pct is None:
            return refuse("missing_sl_tp")

        gates.append("volume_bounds")
        volume = self._config.volume
        if not (0 < volume <= self._config.max_volume):
            return refuse("volume_out_of_bounds")

        gates.append("cooldown")
        if self.last_fired_at is not None:
            elapsed = (self._now() - self.last_fired_at).total_seconds() / 60.0
            if elapsed < self._config.cooldown_minutes:
                return refuse(f"cooldown:{elapsed:.1f}m<{self._config.cooldown_minutes}m")

        # Broker truth from here down — never cached, never inferred from shadow state.
        gates.append("trading_enabled")
        status = await self._client.status()
        # `trading_enabled` is None on an older trading_hands binary that does not report it.
        # `not None` is True, so an unknown answer refuses — an unreported safety flag must never
        # be read as permission.
        if not getattr(status, "trading_enabled", False):
            return refuse("trading_disabled")

        gates.append("demo_account")
        if self._config.require_demo and getattr(status, "environment", "") != "demo":
            return refuse(f"not_demo:{getattr(status, 'environment', '?')}")

        # The size we will actually get, not the one we asked for. An older binary reports None;
        # refuse then too, because an unknown position size is not a safe one.
        gates.append("broker_volume")
        broker_volume = getattr(status, "trading_volume", None)
        if broker_volume is None:
            return refuse("broker_volume_unknown")
        if broker_volume > self._config.max_volume:
            return refuse(f"broker_volume_too_large:{broker_volume}>{self._config.max_volume}")

        gates.append("existing_positions")
        positions = await self._client.positions()
        same_symbol = [p for p in positions if p.symbol == symbol]
        if same_symbol:
            return refuse(f"position_exists:{same_symbol[0].external_id}")
        if len(positions) >= self._config.max_open_positions:
            return refuse(f"max_positions:{len(positions)}>={self._config.max_open_positions}")

        prediction = build_prediction(symbol=symbol, direction=direction, sl_pct=sl_pct,
                                      tp_pct=tp_pct, confidence=confidence, volume=volume,
                                      as_of=as_of)
        result: TradeResult | None = await self._client.purchase(prediction)
        if result is None:
            return refuse("broker_no_action")      # 204: broker declined without an error
        if not result.accepted:
            return refuse("broker_rejected")

        # Only a genuinely accepted order starts the cooldown — a refusal must not lock us out.
        self.last_fired_at = self._now()
        return RouteResult(placed=True, external_id=result.external_id or None,
                           symbol=result.symbol or symbol,
                           side=result.side or ("buy" if direction is Direction.BUY else "sell"),
                           # The broker's echoed volume is authoritative — it is what actually
                           # got filled, which may differ from what we requested.
                           volume=result.volume or broker_volume, gates_checked=gates)


def live_config_from_settings(settings) -> LiveExecutionConfig:
    return LiveExecutionConfig(
        enabled=getattr(settings, "live_execution_enabled", False),
        require_demo=getattr(settings, "live_require_demo", True),
        volume=getattr(settings, "live_volume", 0.01),
        max_volume=getattr(settings, "live_max_volume", 0.10),
        cooldown_minutes=getattr(settings, "live_cooldown_minutes", 15.0),
        max_open_positions=getattr(settings, "live_max_open_positions", 1),
    )
