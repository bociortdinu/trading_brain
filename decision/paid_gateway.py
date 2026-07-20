"""The central financial gateway for PAID Anthropic calls (Codex audit §8).

Every paid path (shadow.runner --maker claude, app.decide --paid, app.llm_smoke) constructs a
`PaidAiGateway` and calls it through the normal `decide()` / `call()` surface. The gateway enforces,
in one place:

- the master kill-switch (BRAIN_PAID_AI_ENABLED) — a configured key alone never spends;
- a model ALLOWLIST (the effective model is printed before any request);
- persistence + an explicit run_id (a paid run must be auditable);
- a hard cap on HTTP attempts per logical decision (retries), set on the inner maker;
- USD budgets per run / UTC day / UTC month, DERIVED from the paid_attempts ledger, with an ATOMIC
  reservation + a pre-attempt 'started' row written BEFORE the request (so a timeout/crash still
  leaves a trace) and a finalize with the real usage afterwards.

Budgets default to 0 (nothing permitted) — you raise them only for a canary/experiment. Follow-up:
per-HTTP-attempt rows for retries>1 (today the inner maker's internal retries share one reserved
row whose estimate already covers the retry budget; with the canary default of 1 attempt the row IS
the HTTP attempt).
"""

from __future__ import annotations

import sys

from decision.llm_client import SYSTEM_RULES, LlmDecisionError, _PRICES
from decision.paid_guard import require_paid_ai_enabled
from database.repository import finalize_paid_attempt, reserve_paid_attempt


class PaidAiGateway:
    """A DecisionMaker that fronts the real Anthropic maker with the financial gateway."""

    def __init__(self, settings, *, run_id: str, persist_dsn: str, context: str,
                 model: str | None = None, inner=None) -> None:
        if not run_id or not persist_dsn:
            raise SystemExit(f"REFUSING paid AI ({context}): a paid run requires persistence + an "
                             f"explicit run_id (auditable). Pass --persist and --run-id.")
        require_paid_ai_enabled(settings, context=context)
        model = model or settings.decision_model
        allow = list(getattr(settings, "paid_ai_model_allowlist", []) or [])
        if model not in allow:
            raise SystemExit(f"REFUSING paid AI ({context}): model {model!r} is not in the paid "
                             f"allowlist {allow}. Set BRAIN_DECISION_MODEL to an allowed model or "
                             f"add it to BRAIN_PAID_AI_MODEL_ALLOWLIST.")
        self._dsn = persist_dsn
        self._run_id = run_id
        self._context = context
        self._model = model
        self._max_tokens = settings.decision_max_tokens
        self._max_http_attempts = settings.paid_max_http_attempts
        self._budget_run = settings.paid_budget_run_usd
        self._budget_day = settings.paid_budget_day_usd
        self._budget_month = settings.paid_budget_month_usd
        self.calls = 0                     # logical decide() count (the runner's cap reads this)
        self.last_result = None
        if inner is None:
            from decision.llm_client import AnthropicDecisionMaker
            inner = AnthropicDecisionMaker(settings.anthropic_api_key, model,
                                           max_tokens=self._max_tokens,
                                           max_retries=max(self._max_http_attempts - 1, 0))
        self._inner = inner
        # Transparency: the operator sees exactly what will be billed-against before any request.
        print(f"[paid gateway] context={context} model={model} run_id={run_id} "
              f"max_http_attempts={self._max_http_attempts} budgets(run/day/month)="
              f"${self._budget_run:.2f}/${self._budget_day:.2f}/${self._budget_month:.2f}",
              file=sys.stderr)

    def _estimate_worst_usd(self, inp) -> float:
        """A GENEROUS per-logical-call estimate for the reservation: real input size (system rules +
        the input JSON) as input tokens, max_tokens as output, a cache-write surcharge, times the
        HTTP-attempt budget. The AUTHORITATIVE cost is the usage recorded at finalize."""
        pin, pout = _PRICES.get(self._model, (5.0e-6, 25.0e-6))
        prompt_chars = len(SYSTEM_RULES) + len(inp.model_dump_json())
        prompt_tokens = int(prompt_chars / 4 * 1.2)          # ~4 chars/token, +20% safety
        per_attempt = prompt_tokens * pin + self._max_tokens * pout + prompt_tokens * pin * 0.25
        return round(per_attempt * self._max_http_attempts, 6)

    async def call(self, inp):
        """Reserve budget + record a 'started' attempt BEFORE the request, run the inner call, then
        finalize with the real usage. Raises BudgetExceeded (before any request) if no budget."""
        self.calls += 1
        input_hash = inp.input_hash()
        est = self._estimate_worst_usd(inp)
        attempt_id = reserve_paid_attempt(
            self._dsn, run_id=self._run_id, context=self._context, model=self._model,
            input_hash=input_hash, attempt_no=0, est_cost_usd=est,
            budget_run_usd=self._budget_run, budget_day_usd=self._budget_day,
            budget_month_usd=self._budget_month)   # raises BudgetExceeded -> no request made
        try:
            res = await self._inner.call(inp)
        except Exception:
            finalize_paid_attempt(self._dsn, attempt_id=attempt_id, status="error")
            raise
        self.last_result = res
        status = ("completed" if res.ok
                  else "timeout" if (res.error and "Timeout" in res.error) else "error")
        finalize_paid_attempt(
            self._dsn, attempt_id=attempt_id, status=status, request_id=res.request_id,
            input_tokens=res.input_tokens, output_tokens=res.output_tokens,
            cache_read_tokens=res.cache_read_input_tokens,
            cache_write_tokens=res.cache_creation_input_tokens,
            actual_cost_usd=res.estimated_cost_usd)
        return res

    async def decide(self, inp):
        res = await self.call(inp)
        if not res.ok or res.output is None:
            raise LlmDecisionError(res.error or "llm_call_failed")
        return res.output

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose:
            await aclose()
