"""Anthropic-backed DecisionMaker (the real "first LLM call"), fail-closed.

Structured Outputs: `client.messages.parse(..., output_format=DecisionOutput)` ->
`response.parsed_output` is a validated DecisionOutput. The schema is enforced by pydantic,
so a malformed / out-of-vocabulary reply cannot pass.

Fail-closed on EVERY failure mode:
- timeout / transport / 429 / 5xx  -> BOUNDED retry (transient only), then error;
- 4xx (except 429)                 -> immediate error (no retry);
- stop_reason 'refusal'            -> error (never a fabricated decision);
- stop_reason 'max_tokens'         -> error (truncated output is untrustworthy);
- parsed_output missing / invalid  -> error.
Never raises a raw SDK exception to the caller; `decide()` raises the typed LlmDecisionError.

Observability WITHOUT leaking secrets: we log/persist request-id, requested + effective
model, stop_reason, token usage (incl. cache), latency, prompt/schema versions and the
input hash. We NEVER log the API key or the prompt/inputs (which may embed sensitive data).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from decision.schema import (
    DECISION_PROMPT_VERSION,
    DECISION_SCHEMA_VERSION,
    DecisionInput,
    DecisionOutput,
)

log = logging.getLogger(__name__)

DEFAULT_DECISION_MODEL = "claude-sonnet-5"      # single configurable baseline
BENCHMARK_MODEL = "claude-opus-4-8"             # run on the SAME frozen inputs for comparison

# Static, cacheable decision rules (kept SEPARATE from the dynamic per-bar input so it can be
# prompt-cached). BUY/SELL/NO_TRADE only; SL/TP are NOT the model's job.
SYSTEM_RULES = (
    "You are a disciplined intraday gold (XAUUSD) trading analyst. You receive a compact "
    "multi-timeframe snapshot (D1 macro, H4 major trend, H1 structure, M15 trigger) with derived "
    "indicators only — never raw candles. Decide exactly one action: BUY, SELL, or NO_TRADE. "
    "`confidence` is an ORDINAL 0..1 conviction, not a probability. Prefer NO_TRADE when the "
    "multi-timeframe picture is mixed or the trigger contradicts the higher-timeframe bias. "
    "Give a short rationale and up to a few key_factors.\n\n"
    "NEWS: `news.status` is 'unavailable' when we could not retrieve the feed — that means "
    "UNKNOWN, not 'no news'. Never treat 'unavailable' as evidence that no catalyst exists, and "
    "do not justify a trade by the absence of news you could not see. Only `status: \"ok\"` with "
    "an empty list means genuinely nothing scheduled.\n\n"
    "STOP AND TARGET: propose `proposed_sl_pct` and `proposed_tp_pct` as POSITIVE distances from "
    "the current price, in percent. Place them against market STRUCTURE — put the stop beyond the "
    "level that would invalidate your idea (see nearest_support_pct / nearest_resistance_pct and "
    "the ATR of each timeframe), not at a round number. Explain the placement in `sl_tp_rationale`. "
    "A wide stop paired with a near target is not acceptable: the target must be at least twice "
    "the stop distance, and a proposal that is not will be discarded in favour of a default. Do "
    "NOT output position size — that is fixed downstream."
)

# Rough USD/token prices for a cost ESTIMATE only (not billing truth). $/token.
_PRICES = {
    "claude-sonnet-5": (3.0e-6, 15.0e-6),
    "claude-opus-4-8": (5.0e-6, 25.0e-6),
    "claude-haiku-4-5": (1.0e-6, 5.0e-6),
}


class LlmDecisionError(RuntimeError):
    """Any fail-closed failure obtaining a valid decision from the model."""


class LlmCallResult(BaseModel):
    ok: bool
    error: str | None = None
    output: DecisionOutput | None = None
    requested_model: str
    effective_model: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    latency_ms: int | None = None
    estimated_cost_usd: float | None = None
    retry_count: int = 0     # transient retries performed before this result (0 = first attempt)
    prompt_version: str = DECISION_PROMPT_VERSION
    schema_version: str = DECISION_SCHEMA_VERSION
    input_hash: str


def _estimate_cost(model: str | None, usage: Any) -> float | None:
    prices = _PRICES.get(model or "")
    if not prices and model:
        # the effective model may carry a date suffix (e.g. claude-haiku-4-5-20251001);
        # match the pricing by base-model prefix.
        for base, p in _PRICES.items():
            if model.startswith(base):
                prices = p
                break
    if not prices or usage is None:
        return None
    pin, pout = prices
    return round((getattr(usage, "input_tokens", 0) or 0) * pin
                 + (getattr(usage, "output_tokens", 0) or 0) * pout, 6)


class AnthropicDecisionMaker:
    """Implements the DecisionMaker Protocol. `call()` returns the full manifest;
    `decide()` fits the Protocol and raises LlmDecisionError on any failure."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DECISION_MODEL,
        *,
        max_tokens: int = 1024,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        sleep_fn=None,
    ) -> None:
        import anthropic  # imported lazily so the base package has no hard anthropic dep

        self._anthropic = anthropic
        # max_retries=0: we own a bounded, transient-only retry loop below.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        self._model = model
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep_fn  # if None, use anyio via the SDK; injected in tests
        self.last_result: LlmCallResult | None = None  # manifest of the most recent call

    async def aclose(self) -> None:
        await self._client.close()

    def _system(self) -> list[dict]:
        # cache_control -> the static rules block is prompt-cached (cache_read on repeats).
        return [{"type": "text", "text": SYSTEM_RULES, "cache_control": {"type": "ephemeral"}}]

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self._backoff_base * (2 ** attempt) + random.uniform(0, 0.25), self._backoff_cap)
        if self._sleep is not None:
            await self._sleep(delay)
        else:
            import anyio
            await anyio.sleep(delay)

    async def call(self, inp: DecisionInput) -> LlmCallResult:
        a = self._anthropic
        input_hash = inp.input_hash()
        user = inp.model_dump_json()
        base = dict(requested_model=self._model, input_hash=input_hash)
        t0 = time.monotonic()
        last_err = "unknown"

        for attempt in range(self._max_retries + 1):
            base["retry_count"] = attempt   # carried into every return path via **base
            try:
                resp = await self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system(),
                    messages=[{"role": "user", "content": user}],
                    output_format=DecisionOutput,
                )
            except (a.APITimeoutError, a.APIConnectionError, a.RateLimitError) as exc:
                last_err = f"{type(exc).__name__}"          # transient -> retry (bounded)
            except a.APIStatusError as exc:
                if exc.status_code >= 500 and attempt < self._max_retries:
                    last_err = f"HTTP {exc.status_code}"     # 5xx transient
                else:
                    return LlmCallResult(ok=False, error=f"api_status:{exc.status_code}",
                                         latency_ms=int((time.monotonic() - t0) * 1000), **base)
            except Exception as exc:  # noqa: BLE001 — never leak a raw SDK error to the caller
                return LlmCallResult(ok=False, error=f"unexpected:{type(exc).__name__}",
                                     latency_ms=int((time.monotonic() - t0) * 1000), **base)
            else:
                return self._finish(resp, t0, base)

            if attempt < self._max_retries:
                await self._sleep_backoff(attempt)

        return LlmCallResult(ok=False, error=f"exhausted_retries:{last_err}",
                             latency_ms=int((time.monotonic() - t0) * 1000), **base)

    def _finish(self, resp: Any, t0: float, base: dict) -> LlmCallResult:
        usage = getattr(resp, "usage", None)
        common = dict(
            effective_model=getattr(resp, "model", None),
            request_id=getattr(resp, "_request_id", None),
            stop_reason=getattr(resp, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
            latency_ms=int((time.monotonic() - t0) * 1000),
            estimated_cost_usd=_estimate_cost(getattr(resp, "model", None), usage),
            **base,
        )
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            return LlmCallResult(ok=False, error="refusal", **common)
        if stop == "max_tokens":
            return LlmCallResult(ok=False, error="max_tokens_truncated", **common)
        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            return LlmCallResult(ok=False, error="no_parsed_output", **common)
        try:
            output = parsed if isinstance(parsed, DecisionOutput) else DecisionOutput.model_validate(parsed)
        except ValidationError as exc:
            return LlmCallResult(ok=False, error=f"validation:{exc.error_count()}errs", **common)
        return LlmCallResult(ok=True, output=output, **common)

    async def decide(self, inp: DecisionInput) -> DecisionOutput:
        res = await self.call(inp)
        self.last_result = res
        if not res.ok or res.output is None:
            raise LlmDecisionError(res.error or "llm_call_failed")
        return res.output
