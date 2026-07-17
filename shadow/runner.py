"""Shadow backtest runner: replay historical bars, produce reconciled shadow trades + edge
metrics — WITHOUT real money and (with the deterministic strategy) WITHOUT paid LLM calls.

For each M15 close that has enough history on every timeframe, it builds the as-of packet
with a MODELED spread (never the live quote — anti look-ahead), runs the SAME decision
pipeline (prefilter -> decide -> Risk Engine), and if the verdict is approved opens a virtual
trade and reconciles it against the bars that FOLLOW the entry. The result is a distribution
of R-multiples (net of the round-trip spread) with the pessimistic/optimistic ambiguity band.

`ConfluenceStrategy` is a deterministic stand-in for the LLM so the mechanism can be
validated and metrics produced at zero cost; swap in the Anthropic maker for a real (paid)
measurement of the model's edge on identical inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
from datetime import datetime, timedelta, timezone

from core.models import Direction
from data_collector.providers.base import Candle
from data_collector.providers.factory import build_provider
from data_collector.session import calendar_for
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig, prefilter
from decision.schema import (
    DecisionOutput,
    build_decision_input,
    decision_fingerprint,
)
from features.eligibility import EligibilityConfig, evaluate_eligibility
from features.engineering import MIN_BARS
from features.mtf import TRIGGER_TF, build_feature_packet
from risk.engine import RiskConfig
from shadow.metrics import summarize
from shadow.reconciler import reconcile
from shadow.virtual_broker import ShadowConfig, open_virtual_trade


class ConfluenceStrategy:
    """Deterministic LLM stand-in for cost-free backtests: BUY on aligned_bull, SELL on
    aligned_bear, else NO_TRADE. NOT a real edge — it exercises the backtest + reconciler."""

    async def decide(self, inp) -> DecisionOutput:
        if inp.confluence == "aligned_bull":
            return DecisionOutput(direction=Direction.BUY, confidence=0.7, rationale="aligned_bull")
        if inp.confluence == "aligned_bear":
            return DecisionOutput(direction=Direction.SELL, confidence=0.7, rationale="aligned_bear")
        return DecisionOutput(direction=Direction.NO_TRADE, confidence=0.5, rationale="not aligned")


class _CountingMaker:
    """Wraps a maker to COUNT real decide() calls — the guardrail the runner uses to enforce a
    hard `--max-llm-calls` budget on paid runs (fail-closed: the loop stops before overspending).
    Delegates last_result so llm_calls are still persisted."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    async def decide(self, inp):
        self.calls += 1
        return await self.inner.decide(inp)

    @property
    def last_result(self):
        return getattr(self.inner, "last_result", None)


def _slice(windows: dict[str, list[Candle]], as_of: datetime) -> dict[str, list[Candle]]:
    return {tf: [c for c in cs if c.close_time <= as_of] for tf, cs in windows.items()}


def _bar_seconds(bars: list[Candle]) -> float:
    """Median M15 bar length in seconds (robust to gaps), for the post-close cooldown clock."""
    deltas = sorted((bars[i + 1].open_time - bars[i].open_time).total_seconds()
                    for i in range(len(bars) - 1))
    return deltas[len(deltas) // 2] if deltas else 900.0


async def backtest_over_windows(windows: dict[str, list[Candle]], **kw) -> list[dict]:
    """Run a backtest, holding an EXCLUSIVE lock on the run_id for its whole duration.

    The lock is not optional bookkeeping: this backtest is STATEFUL (`busy_until` carries the open
    position from bar to bar). Per-bar reservations stop two workers paying twice for the same
    bar, but they do not make the run parallelisable — two workers just split the bars and each
    tracks its own open position, so "one position at a time" quietly stops being true (measured:
    2 workers -> 19 trades, 11 overlapping pairs). Serialise the run, or the headline result is
    not the strategy you think you measured.
    """
    persist_dsn, run_id = kw.get("persist_dsn"), kw.get("run_id")
    if not (persist_dsn and run_id):
        return await _backtest_over_windows(windows, **kw)   # nothing persisted -> nothing to guard
    from database.repository import run_lock
    with run_lock(persist_dsn, run_id):
        return await _backtest_over_windows(windows, **kw)


async def _backtest_over_windows(
    windows: dict[str, list[Candle]],
    *,
    symbol: str,
    provider_name: str,
    decision_maker,
    modeled_spread_pct: float,
    slippage_pct: float = 0.0,
    prefilter_config: PrefilterConfig | None = None,
    risk_config: RiskConfig | None = None,
    eligibility_config: EligibilityConfig | None = None,
    shadow_config: ShadowConfig | None = None,
    min_bars: int = MIN_BARS,
    single_position: bool = True,   # one position at a time (executable); False = event-study
    cooldown_bars: int = 0,         # extra bars to wait AFTER a trade closes before re-entry
    max_llm_calls: int | None = None,  # hard cap on decide() calls (paid runs); None = unlimited
    persist_dsn: str | None = None,
    run_id: str | None = None,
    model_name: str = "deterministic-confluence",
    worker_id: str | None = None,      # identifies this worker's reservations (default: host:pid)
) -> list[dict]:
    """Backtest over provided windows. Returns per-bar dicts:
    {as_of, stage, direction, approved, outcome(dict|None)}. When `persist_dsn`+`run_id` are
    given, the full chain (snapshot -> evaluation -> decision -> trade) is written for each
    APPROVED trade so the run leaves auditable shadow trades in the DB (idempotent on
    (decision_id, run_id))."""
    calendar = calendar_for(provider_name)
    prefilter_config = prefilter_config or PrefilterConfig()
    risk_config = risk_config or RiskConfig()
    eligibility_config = eligibility_config or EligibilityConfig()
    shadow_config = shadow_config or ShadowConfig()

    m15 = windows[TRIGGER_TF]
    out: list[dict] = []
    # Position policy: a single account holds ONE position at a time. Without this gate the
    # backtest opens a new trade on every approved bar (the reviewer saw 23 concurrent
    # positions), which is NOT an executable strategy — the summed R is meaningless. We block
    # new entries while a trade is open, plus an optional cooldown after it closes.
    busy_until = None  # datetime: no new entry strictly before this (open trade + cooldown)
    bar_seconds = _bar_seconds(m15)
    counting = isinstance(decision_maker, _CountingMaker)
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    for as_of in (c.close_time for c in m15):
        sliced = _slice(windows, as_of)
        if any(len(sliced.get(tf, [])) < min_bars for tf in windows):
            continue  # not enough history on some timeframe yet
        packet = build_feature_packet(
            symbol, sliced, as_of=as_of, provider=provider_name, provider_symbol=symbol,
            ingested_at=as_of, spread_pct=modeled_spread_pct, calendar=calendar,
        )
        elig = evaluate_eligibility(sliced, TRIGGER_TF, as_of, mode="replay", now=as_of,
                                    config=eligibility_config, calendar=calendar)

        # HARD LLM BUDGET first — BEFORE any claim. Reserving and then breaking on the cap
        # stranded an 'in_progress' reservation with no decision behind it, blocking that bar
        # until its lease expired (measured with cap=0: 1 reservation, 0 decisions).
        if counting and max_llm_calls is not None and decision_maker.calls >= max_llm_calls:
            out.append({"as_of": as_of, "stage": "llm_cap_reached", "direction": "NO_TRADE",
                        "approved": False, "blocked": None, "outcome": None})
            break

        # RESERVE BEFORE THE (paid) LLM. A plain SELECT here would not stop two concurrent
        # workers from both missing it and both paying — uniqueness only discards the loser's
        # ROW, never the CHARGE. The claim below is atomic: exactly one worker may call the model.
        #
        # Only bars that will actually REACH the model are reserved: the prefilter is pure and
        # free, so a bar it rejects costs nothing, is recomputed identically on a resume, and must
        # not leave an unfinished reservation behind.
        fingerprint = claim_token = None
        if persist_dsn and run_id and prefilter(packet, elig, prefilter_config).passed:
            from database.repository import reserve_decision
            fingerprint = decision_fingerprint(
                input_hash=build_decision_input(packet, mode="replay").input_hash(),
                model=model_name, provider=provider_name, risk_config_version=risk_config.version)
            claim, claim_token = reserve_decision(
                persist_dsn, input_fingerprint=fingerprint, run_id=run_id, worker=worker_id)
            if claim == "held":
                # Impossible under the run lock we hold — another worker would have to be inside
                # the same run. Fail loudly rather than skip the bar: skipping is what let two
                # workers split a stateful run and produce overlapping positions.
                raise RuntimeError(
                    f"reservation for {as_of.isoformat()} is held by another worker despite the "
                    f"exclusive lock on run {run_id!r} — refusing to continue")
            if claim == "done":
                row = _resume_row(persist_dsn, as_of, run_id, fingerprint, claim)
                busy_until = _resume_busy_until(row, busy_until, m15, bar_seconds, cooldown_bars)
                out.append(row)
                continue

        rec = await run_decision(packet, elig, decision_maker, mode="replay",
                                 prefilter_config=prefilter_config, risk_config=risk_config,
                                 calendar=calendar)
        llm_result = getattr(decision_maker, "last_result", None)
        # Decide the BLOCKED disposition before persisting, so the decision row records it and a
        # resume can tell "approved and traded" from "approved but a position was already open".
        blocked = ("position_open" if (rec.risk_approved and single_position
                                       and busy_until is not None and as_of < busy_until)
                   else None)
        row = {"as_of": as_of, "stage": rec.stage,
               "direction": rec.decision.direction.value if rec.decision else "NO_TRADE",
               "approved": rec.risk_approved, "blocked": blocked, "outcome": None}

        # Persist EVERY decided bar (not only approved ones) so a paid re-run resumes past
        # NO_TRADE/rejected bars too — the LLM was called for them, so they must be deduped.
        dec_id = None
        if persist_dsn and run_id and rec.stage == "decided":
            dec_id = _persist_decision(persist_dsn, run_id, model_name, symbol, provider_name,
                                       packet, elig, rec, fingerprint=fingerprint,
                                       llm_result=llm_result, blocked_reason=blocked)
        # Close out the claim. 'failed' (the LLM errored, OR the snapshot was unusable so nothing
        # was persisted) stays retryable; 'done' is terminal and must only be claimed when a
        # decision actually landed — marking 'done' with decision_id NULL would tell every later
        # run "already decided" about a decision that does not exist.
        if fingerprint is not None:
            _release(persist_dsn, fingerprint, run_id,
                     "done" if dec_id is not None else "failed", dec_id, claim_token)
        if rec.risk_approved:
            if blocked:
                out.append(row)   # approved but not tradeable -> not a trade
                continue
            future = [c for c in m15 if c.open_time >= as_of]
            entry_ref = future[0].open if future else packet.price  # fill at next bar's open (latency)
            trade = open_virtual_trade(
                rec.decision.direction, entry_ref, rec.risk.sl_pct, rec.risk.tp_pct,
                spread_pct=modeled_spread_pct, spread_provenance="modeled",
                slippage_pct=slippage_pct, opened_at=as_of,
            )
            o = reconcile(trade, future, shadow_config)
            row["outcome"] = o.model_dump()
            if single_position:
                closed_at = o.closed_at or (future[-1].close_time if future else as_of)
                busy_until = closed_at + timedelta(seconds=bar_seconds * cooldown_bars)
            if dec_id is not None:
                _persist_trade(persist_dsn, run_id, symbol, dec_id, trade, o, shadow_config)
        out.append(row)
    return out


def _release(dsn, fingerprint, run_id, status, decision_id, claim_token) -> None:
    from database.repository import complete_decision_reservation

    complete_decision_reservation(dsn, input_fingerprint=fingerprint, run_id=run_id,
                                  status=status, decision_id=decision_id, claim_token=claim_token)


def _resume_busy_until(row, current, m15, bar_seconds, cooldown_bars):
    """Rebuild the open-position clock from a resumed bar. An outcome with NO closed_at is still
    OPEN at the end of the data and must block for the rest of the run — mirroring what the live
    path does with `future[-1]`. Forgetting this let a resume open a second position on top."""
    outcome = row.get("outcome")
    if not outcome:
        return current   # nothing traded on this bar (NO_TRADE / rejected / blocked)
    closed_at = outcome.get("closed_at") or (m15[-1].close_time if m15 else None)
    if closed_at is None:
        return current
    return closed_at + timedelta(seconds=bar_seconds * cooldown_bars)


def _resume_row(dsn, as_of, run_id, fingerprint, claim) -> dict:
    """Rebuild the report row for a bar an earlier run already handled, from what was persisted —
    so a resumed run reproduces the original instead of reporting blanks."""
    from database.repository import load_decided_outcome

    prior = load_decided_outcome(dsn, input_fingerprint=fingerprint, run_id=run_id)
    row = {"as_of": as_of, "stage": "resumed", "direction": "NO_TRADE",
           "approved": False, "blocked": None, "outcome": None}
    if prior is None:
        return row
    row["direction"] = prior["direction"]
    row["approved"] = prior["risk_verdict"] == "approved"
    row["blocked"] = prior["blocked_reason"]
    if prior["status"] is not None:   # a trade was opened for this bar
        row["outcome"] = {
            "status": prior["status"], "exit_reason": prior["exit_reason"],
            "exit_price": float(prior["exit_price"]) if prior["exit_price"] is not None else None,
            "closed_at": prior["closed_at"],
            "r_multiple": float(prior["r_multiple"]) if prior["r_multiple"] is not None else None,
            "r_pessimistic": float(prior["r_pessimistic"]) if prior["r_pessimistic"] is not None else None,
            "r_optimistic": float(prior["r_optimistic"]) if prior["r_optimistic"] is not None else None,
            "ambiguous": prior["ambiguous"],
        }
    return row


def _persist_decision(dsn, run_id, model_name, symbol, provider_name, packet, elig, rec, *,
                      fingerprint, llm_result, blocked_reason=None) -> int | None:
    """Write snapshot -> evaluation -> decision (+ the LLM call, if any) for one decided bar.
    ATOMIC/idempotent on (input_fingerprint, run_id) via insert_decision's ON CONFLICT. Returns
    the decision id, or None if the snapshot wasn't usable."""
    from database.repository import (
        insert_decision, insert_evaluation, insert_llm_call, upsert_snapshot,
    )

    status, snap_id = upsert_snapshot(dsn, packet)
    if snap_id is None or status == "conflict":
        return None
    if llm_result is not None:   # persist the paid API call (success or failure) for audit
        insert_llm_call(dsn, llm_result, snapshot_id=snap_id)
    eval_id = insert_evaluation(dsn, snap_id, elig)
    inp = build_decision_input(packet, mode="replay")
    return insert_decision(
        dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name, record=rec,
        ai_input=inp.model_dump(mode="json"),
        ai_output=rec.decision.model_dump(mode="json") if rec.decision else None,
        mode="shadow", data_provider=provider_name, run_id=run_id, input_fingerprint=fingerprint,
        blocked_reason=blocked_reason,
    )


def _persist_trade(dsn, run_id, symbol, dec_id, trade, outcome, shadow_config) -> None:
    """Upsert the shadow trade for an approved, decided bar (idempotent on (decision_id, run_id),
    monotone: a closed trade is never reopened)."""
    from database.repository import upsert_shadow_trade
    from shadow.virtual_broker import cost_manifest

    upsert_shadow_trade(
        dsn, decision_id=dec_id, run_id=run_id, symbol=symbol, trade=trade, outcome=outcome,
        timeframe=TRIGGER_TF, timeout_bars=shadow_config.timeout_bars,
        costs=cost_manifest(trade, shadow_config),
    )


def report(rows: list[dict]) -> dict:
    """Backtest summary: bars evaluated, approvals, position-gate blocks, and edge metrics over
    the trades ACTUALLY opened. `approved` counts every risk-approved signal; `blocked` are
    approvals suppressed because a position was already open; `trades_opened` is what the
    metrics are computed over. With single_position, approved == blocked + trades_opened."""
    outcomes = [r["outcome"] for r in rows if r["outcome"] is not None]
    blocked = sum(1 for r in rows if r.get("blocked"))
    return {
        "bars_evaluated": len(rows),
        "prefiltered_out": sum(1 for r in rows if r["stage"] == "prefiltered_out"),
        "approved": sum(1 for r in rows if r["approved"]),
        "blocked_position_open": blocked,
        "trades_opened": len(outcomes),
        "metrics": summarize(outcomes),
    }


def _build_maker(settings, kind: str):
    """deterministic = free ConfluenceStrategy; claude = the real paid AnthropicDecisionMaker
    (wrapped in _CountingMaker so the runner can enforce a hard call cap). Returns
    (maker, model_name, is_paid)."""
    if kind == "deterministic":
        return ConfluenceStrategy(), "deterministic-confluence", False
    if kind == "claude":
        if not settings.anthropic_api_key:
            raise SystemExit("--maker claude needs BRAIN_ANTHROPIC_API_KEY (real, paid API calls)")
        from decision.llm_client import AnthropicDecisionMaker
        inner = AnthropicDecisionMaker(settings.anthropic_api_key, settings.decision_model,
                                       max_tokens=settings.decision_max_tokens)
        return _CountingMaker(inner), settings.decision_model, True
    raise SystemExit(f"unknown maker {kind!r} (expected deterministic|claude)")


def _confirm_paid_run(model: str, max_calls: int, max_tokens: int, assume_yes: bool) -> None:
    """Fail-closed cost gate for a paid backtest: print a WORST-CASE cost estimate and require an
    explicit confirmation (interactive y/N, or --yes). Never spends money silently."""
    from decision.llm_client import _PRICES

    pin, pout = _PRICES.get(model, (5.0e-6, 25.0e-6))   # default to Opus-tier (conservative)
    # worst case: every call sends a full prompt (~max_tokens in) and fills max_tokens out.
    est = max_calls * (max_tokens * pin + max_tokens * pout)
    print(f"[paid run] maker=claude model={model} max_llm_calls={max_calls} "
          f"-> worst-case ~${est:.2f} (dedupe/resume + prefilter usually make it far less).")
    if assume_yes:
        print("[paid run] --yes given; proceeding.")
        return
    try:
        reply = input("Proceed with PAID API calls? [y/N] ").strip().lower()
    except EOFError:
        reply = ""
    if reply not in ("y", "yes"):
        raise SystemExit("aborted (no confirmation).")


async def _run(settings, *, count: int, run_id: str | None, maker_kind: str = "deterministic",
               max_llm_calls: int = 50, assume_yes: bool = False) -> None:
    maker, model_name, is_paid = _build_maker(settings, maker_kind)
    if is_paid:
        _confirm_paid_run(model_name, max_llm_calls, settings.decision_max_tokens, assume_yes)

    provider = build_provider(settings)
    symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(symbol)
    tfs = settings.timeframes
    try:
        windows = {tf: await provider.get_ohlcv(provider_symbol, tf, count) for tf in tfs}
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()
    try:
        rows = await backtest_over_windows(
            windows, symbol=symbol, provider_name=settings.market_data_provider,
            decision_maker=maker, modeled_spread_pct=settings.replay_spread_pct,
            slippage_pct=settings.slippage_pct, model_name=model_name,
            max_llm_calls=max_llm_calls if is_paid else None,
            shadow_config=ShadowConfig(commission_pct=settings.commission_pct,
                                       swap_pct_per_night=settings.swap_pct_per_night),
            persist_dsn=settings.db_dsn if run_id else None, run_id=run_id,
        )
    finally:
        # ALWAYS close the Anthropic client (even on error/cap) so we don't leak the connection.
        maker_aclose = getattr(getattr(maker, "inner", None), "aclose", None)
        if maker_aclose:
            await maker_aclose()
    rep = report(rows)
    calls = getattr(maker, "calls", 0)
    print(f"[backtest] provider={settings.market_data_provider} symbol={symbol} "
          f"bars_evaluated={rep['bars_evaluated']} prefiltered_out={rep['prefiltered_out']} "
          f"approved={rep['approved']} blocked_position_open={rep['blocked_position_open']} "
          f"trades_opened={rep['trades_opened']} llm_calls={calls}"
          + (f" persisted run_id={run_id}" if run_id else ""))
    print(f"[metrics] {rep['metrics']}")


def main() -> int:
    from datetime import datetime, timezone

    from config.settings import load_settings

    parser = argparse.ArgumentParser(description="Shadow backtest over historical bars (no execution).")
    parser.add_argument("--count", type=int, default=1500, help="bars per timeframe to fetch")
    parser.add_argument("--persist", action="store_true", help="write shadow trades to the DB")
    parser.add_argument("--run-id", help="experiment id for persisted trades (default: timestamped)")
    parser.add_argument("--maker", choices=["deterministic", "claude"], default="deterministic",
                        help="decision maker: deterministic (free) or claude (paid API batch)")
    parser.add_argument("--max-llm-calls", type=int, default=50,
                        help="hard cap on paid decide() calls (--maker claude); stops cleanly at it")
    parser.add_argument("--yes", action="store_true", help="skip the paid-run confirmation prompt")
    args = parser.parse_args()
    run_id = None
    if args.persist:
        run_id = args.run_id or f"backtest-{args.maker}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    asyncio.run(_run(load_settings(), count=args.count, run_id=run_id, maker_kind=args.maker,
                     max_llm_calls=args.max_llm_calls, assume_yes=args.yes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
