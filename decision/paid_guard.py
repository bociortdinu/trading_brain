"""Fail-closed gate for PAID Anthropic calls — the single choke point every money-spending entry
point must pass through BEFORE constructing or using a real maker.

This is the seed of the central financial gateway (Codex audit §8): today it enforces the master
kill-switch (`BRAIN_PAID_AI_ENABLED`, default OFF) and a human confirmation; USD/HTTP-attempt
budgets and pre-attempt auditing land on top of the same choke point later. The invariant is:
**a configured API key alone must never be enough to spend money.**
"""

from __future__ import annotations

import sys


class PaidAiDisabled(SystemExit):
    """Raised (as a SystemExit, so a CLI aborts with a non-zero code) when a paid call is attempted
    while the master gate is OFF."""


def require_paid_ai_enabled(settings, *, context: str) -> None:
    """Refuse UNLESS the operator has explicitly turned paid AI on. `context` names the call site
    (e.g. 'app.decide', 'shadow.runner', 'app.llm_smoke') so the refusal is actionable. Also
    requires the api key to be present (a paid call cannot succeed without it)."""
    if not getattr(settings, "paid_ai_enabled", False):
        raise PaidAiDisabled(
            f"REFUSING paid AI call ({context}): BRAIN_PAID_AI_ENABLED is off (default). "
            f"A configured API key is NOT sufficient to spend money. Set BRAIN_PAID_AI_ENABLED=true "
            f"only when you intend to pay."
        )
    if not getattr(settings, "anthropic_api_key", None):
        raise PaidAiDisabled(f"REFUSING paid AI call ({context}): BRAIN_ANTHROPIC_API_KEY is missing.")


def confirm_paid_call(*, context: str, model: str, assume_yes: bool,
                      extra: str = "") -> None:
    """Interactive fail-closed confirmation for a paid call: prints WHAT will be spent-on and
    requires an explicit `y` (or a caller-supplied assume_yes, e.g. from --yes). Never proceeds on
    an empty/EOF answer. Complements require_paid_ai_enabled (the master gate); this is the
    per-invocation human check."""
    print(f"[PAID {context}] model={model} {extra}".rstrip())
    if assume_yes:
        print(f"[PAID {context}] --yes given; proceeding.")
        return
    try:
        reply = input("Proceed with a PAID API call? [y/N] ").strip().lower()
    except EOFError:
        reply = ""
    if reply not in ("y", "yes"):
        raise SystemExit(f"aborted ({context}): no confirmation.")


def key_present_note(settings) -> None:
    """Advisory for the free paths: if a key is configured while paid AI is OFF, remind that the
    key is dormant (harmless) — printed to stderr so it never pollutes machine-readable stdout."""
    if getattr(settings, "anthropic_api_key", None) and not getattr(settings, "paid_ai_enabled", False):
        print("note: BRAIN_ANTHROPIC_API_KEY is set but BRAIN_PAID_AI_ENABLED is off — no paid "
              "calls will be made.", file=sys.stderr)
