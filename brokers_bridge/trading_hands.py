"""Async, typed HTTP client for the trading_hands execution API.

The brain talks to XTB ONLY through these 7 endpoints (see trading_hands/AI_manual.md).
Contracts encoded here and validated against the Go source:

- Volume is fixed server-side (TRADING_VOLUME); `allocation` does not size the order.
- `preds_proba` must be >= 0.5 or the order is rejected.
- `take_profit` SIGN encodes direction: >= 0 for Buy, < 0 for Sell; magnitude is a percent.
- `stop_loss` is a positive percent; SL/TP absolute prices are computed server-side.
- TradeResult carries NO close price / profit; outcome is reconstructed elsewhere.

All network/parse failures raise a typed TradingHandsError subclass — never a raw
httpx exception or an uncaught traceback.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Sane bound for SL/TP percentages sent to the broker.
MAX_PCT = 100.0


# --------------------------------------------------------------------------- #
# Error hierarchy
# --------------------------------------------------------------------------- #
class TradingHandsError(RuntimeError):
    """Base class for every failure talking to trading_hands."""


class TradingHandsUnreachable(TradingHandsError):
    """Transport/connection failure — the service is likely not running."""


class TradingHandsTimeout(TradingHandsError):
    """The request exceeded the configured timeout."""


class TradingHandsBadResponse(TradingHandsError):
    """2xx response whose body was not valid JSON / did not match the schema."""


class TradingHandsStatusError(TradingHandsError):
    """Non-2xx response. `status_code` is the primary signal."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"trading_hands {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class AmbiguousSymbolError(TradingHandsError):
    """A symbol query resolved to more than one plausible instrument."""

    def __init__(self, query: str, candidates: list[str]) -> None:
        super().__init__(f"ambiguous symbol query {query!r}; candidates: {candidates}")
        self.query = query
        self.candidates = candidates


# --------------------------------------------------------------------------- #
# Response models (shapes from AI_manual.md, verified against xstation/types.go)
# --------------------------------------------------------------------------- #
class Status(BaseModel):
    connected: bool
    account: str
    environment: str
    # Explicit safety fact exposed by trading_hands. Optional only for compatibility while an
    # older service binary is still running; the dashboard warns when it is absent.
    trading_enabled: bool | None = None


class Balance(BaseModel):
    balance: float
    equity: float
    free_margin: float
    currency: str
    account: str


class Position(BaseModel):
    symbol: str
    instrument_id: int
    volume: float
    open_price: float
    stop_loss: float = 0.0
    take_profit: float = 0.0
    side: str  # "buy" | "sell"
    external_id: str


class Instrument(BaseModel):
    symbol: str
    description: str = ""
    symbol_key: str = ""
    instrument_id: int = 0
    quote_id: int = 0
    asset_class: str = ""
    group_id: str = ""
    precision: int = 0
    tradeable: bool = False
    session_type: int = 0
    min_volume: float = 0.0
    volume_step: float = 0.0


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    time: int = 0

    @property
    def spread_pct(self) -> float:
        """Spread as a percent of ask (real XTB spread at this instant)."""
        return (self.ask - self.bid) / self.ask * 100 if self.ask else 0.0


class TradeResult(BaseModel):
    accepted: bool
    external_id: str = ""
    symbol: str = ""
    side: str = ""
    volume: float = 0.0


# --------------------------------------------------------------------------- #
# Purchase request ("prediction"). Field names are EXACT — trading_hands rejects
# unknown fields with 400. Strict validation is enforced at CONSTRUCTION time:
# a malformed prediction raises pydantic.ValidationError before any network call.
# --------------------------------------------------------------------------- #
class ModelSetup(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    forecast_horizon: int = Field(ge=0)
    target_change: int
    model_type: Literal["Buy", "Sell", "NoAction"]  # external contract, case-sensitive


class PredictionRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    symbol: str = Field(min_length=1)
    prediction_date: str = Field(min_length=1)
    allocation: float = Field(ge=0)
    preds_proba: float = Field(ge=0.5, le=1.0)          # broker rejects < 0.5
    stop_loss: float = Field(gt=0, le=MAX_PCT)          # positive percent
    take_profit: float = Field(ge=-MAX_PCT, le=MAX_PCT)  # sign encodes direction
    model_setup: ModelSetup
    runtimestamp: str = Field(min_length=1)
    sl_tp_logic: str = "percent"
    interval: str = Field(min_length=1)
    id_model_properties: str = Field(min_length=1)

    @model_validator(mode="after")
    def _tp_sign_matches_direction(self) -> "PredictionRequest":
        mt = self.model_setup.model_type
        if mt == "Buy" and self.take_profit < 0:
            raise ValueError("Buy requires take_profit >= 0 (sign encodes direction)")
        if mt == "Sell" and self.take_profit >= 0:
            raise ValueError("Sell requires take_profit < 0 (sign encodes direction)")
        return self


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class TradingHandsClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # `transport` lets tests inject httpx.MockTransport (no real network).
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds, transport=transport
        )

    async def __aenter__(self) -> "TradingHandsClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- internals --------------------------------------------------------- #
    async def _request(self, method: str, path: str, json: Any | None = None) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.TimeoutException as exc:
            raise TradingHandsTimeout(f"{method} {path} timed out") from exc
        except httpx.RequestError as exc:
            raise TradingHandsUnreachable(f"{method} {path}: {exc}") from exc
        if resp.status_code // 100 == 2:
            return resp
        raise TradingHandsStatusError(resp.status_code, self._error_message(resp))

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            return str(resp.json().get("error", resp.text))
        except (json.JSONDecodeError, ValueError, AttributeError):
            return resp.text

    @staticmethod
    def _model(resp: httpx.Response, model: type[BaseModel]) -> Any:
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TradingHandsBadResponse(f"invalid JSON from {resp.url}: {exc}") from exc
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise TradingHandsBadResponse(f"unexpected {model.__name__} shape: {exc}") from exc

    @staticmethod
    def _model_list(resp: httpx.Response, model: type[BaseModel]) -> list[Any]:
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TradingHandsBadResponse(f"invalid JSON from {resp.url}: {exc}") from exc
        if not isinstance(payload, list):
            raise TradingHandsBadResponse(f"expected a JSON array from {resp.url}")
        try:
            return [model.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise TradingHandsBadResponse(f"unexpected {model.__name__} shape: {exc}") from exc

    # ---- read endpoints (always available) --------------------------------- #
    async def status(self) -> Status:
        return self._model(await self._request("GET", "/status"), Status)

    async def balance(self) -> Balance:
        return self._model(await self._request("GET", "/balance"), Balance)

    async def positions(self) -> list[Position]:
        return self._model_list(await self._request("GET", "/positions"), Position)

    async def quote(self, symbol: str) -> Quote:
        """Live bid/ask. Raises TradingHandsStatusError(503) if the market is
        closed or the symbol is unknown."""
        return self._model(await self._request("GET", f"/quote/{symbol}"), Quote)

    async def instruments(self, query: str) -> list[Instrument]:
        return self._model_list(await self._request("GET", f"/instruments/{query}"), Instrument)

    async def resolve_symbol(self, query: str) -> Instrument:
        """Resolve a query (e.g. 'gold') to a SINGLE instrument, deterministically.

        - exactly one exact symbol match -> use it;
        - otherwise, if there is exactly one candidate overall -> use it;
        - anything else is ambiguous -> raise (never pick the first arbitrarily).
        """
        matches = await self.instruments(query)
        if not matches:
            raise TradingHandsStatusError(404, f"no instruments match {query!r}")
        exact = [i for i in matches if i.symbol.upper() == query.upper()]
        if len(exact) == 1:
            return exact[0]
        if not exact and len(matches) == 1:
            return matches[0]
        raise AmbiguousSymbolError(query, [i.symbol for i in matches])

    # ---- action endpoints (require TRADING_ENABLED=true) ------------------- #
    async def purchase(self, prediction: PredictionRequest) -> TradeResult | None:
        """Open a position. Returns None when the server responds 204 (NoAction)."""
        resp = await self._request("POST", "/purchase", json=prediction.model_dump())
        if resp.status_code == 204:
            return None
        return self._model(resp, TradeResult)

    async def close(self, external_id: str) -> TradeResult:
        return self._model(await self._request("POST", f"/close/{external_id}"), TradeResult)
