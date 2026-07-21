"""Continuous shadow-online loop.

Each tick decides on the latest CLOSED M15 bar and, if the Risk Engine approves, opens a
shadow trade (status='open'); every tick also reconciles still-open shadow trades against the
newest bars, closing those whose SL/TP was hit. No real money, no execution — this builds a
live shadow track record. The decision-maker is injected (deterministic ConfluenceStrategy by
default; swap in the Anthropic maker for a paid measurement).

    python -m shadow.online --once      # one tick
    python -m shadow.online             # loop, waking at each M15 close
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from app.collect import (
    _eligibility_config,
    build_packet_from_windows,
    compute_eligibility,
    fetch_windows,
    m15_closes,
    observe_xtb_spread,
)
from app.jobs import TRANSIENT, next_m15
from config.settings import Settings, load_settings
from core.models import Direction
from data_collector.providers.base import Candle, only_closed, timeframe_minutes
from data_collector.providers.factory import build_provider
from data_collector.session import calendar_for
from database.feedback import build_feedback
from database.operations import OperationalTelemetry, git_metadata
from database.repository import (
    assert_run_manifest,
    complete_decision_reservation,
    insert_decision,
    insert_evaluation,
    insert_llm_call,
    insert_spread_observation,
    last_processed_bar,
    open_shadow_trades,
    quarantine_shadow_trade,
    record_downtime_gap,
    record_processed_bar,
    reserve_decision,
    upsert_shadow_trade,
    upsert_snapshot,
)
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig
from decision.schema import (
    STRATEGY_VERSION,
    FeedbackContext,
    build_decision_input,
    decision_fingerprint,
)
from features.mtf import TRIGGER_TF
from risk.engine import RiskConfig
from shadow.reconciler import count_missed_open_bars, reconcile, select_reconcile_bars_for_trade
from shadow.runner import ConfluenceStrategy
from shadow.virtual_broker import (
    ShadowConfig,
    VirtualTrade,
    cost_manifest,
    execution_hash,
    execution_manifest,
    open_virtual_trade,
    shadow_config_from_costs,
    shadow_config_from_settings,
)

log = logging.getLogger(__name__)

# How the online loop handles bars missed during downtime. Each tick decides only the LATEST closed
# bar, so missed bars are SKIPPED (not replayed) — recorded explicitly per gap so the choice is
# auditable, not implicit. A future backfill mode would record 'backfill' instead.
DOWNTIME_POLICY = "skip"

# Tick outcomes that mean the bar was HANDLED cleanly (advance the downtime reference); the rest
# ('llm_failed', 'error') are failures that must NOT advance it.
_OK_OUTCOMES = frozenset({"decided", "position_open", "held", "already_decided",
                          "ineligible", "prefiltered"})


def _record_processed_and_downtime(settings: Settings, provider_name: str, run_id: str,
                                   summary: dict) -> None:
    """After a tick is handled: measure downtime vs the last *processed* bar (P0-C3), persist a real
    gap, then record THIS bar in the processed-bars ledger. Only a cleanly-processed bar (`ok`)
    counts as a resume and advances the reference; a failed bar is recorded but does not."""
    as_of_iso = summary.get("as_of")
    if not as_of_iso:
        return   # no closed bar this tick (nothing processed)
    as_of = datetime.fromisoformat(as_of_iso)
    outcome = summary.get("outcome", "error")
    ok = outcome in _OK_OUTCOMES
    symbol = settings.symbol_query
    if ok:
        prev_proc, prev_run = last_processed_bar(settings.db_dsn, symbol, provider_name)
        missed = count_missed_open_bars(prev_proc, as_of, calendar_for(provider_name))
        if missed:
            summary["missed_bars"] = missed
            summary["downtime_policy"] = DOWNTIME_POLICY
            record_downtime_gap(settings.db_dsn, symbol=symbol, provider=provider_name,
                                prev_bar_close=prev_proc, prev_run_id=prev_run,
                                resumed_bar_close=as_of, run_id=run_id, missed_bars=missed,
                                policy=DOWNTIME_POLICY)
            log.warning("downtime gap: %d open-market bar(s) missing before %s (prev_run=%s, "
                        "run=%s), recorded (policy=%s) — track record NOT continuous over the gap",
                        missed, as_of.isoformat(), prev_run, run_id, DOWNTIME_POLICY)
    record_processed_bar(settings.db_dsn, symbol=symbol, provider=provider_name, bar_close=as_of,
                         run_id=run_id, outcome=outcome, ok=ok)


def _to_trade(row: dict) -> VirtualTrade:
    return VirtualTrade(
        direction=Direction.BUY if row["side"] == "buy" else Direction.SELL,
        entry_mid=float(row["entry_price"]), sl_price=float(row["sl_price"]),
        tp_price=float(row["tp_price"]), spread_pct=float(row["spread_pct"] or 0.0),
        spread_provenance=row["spread_provenance"] or "modeled",
        slippage_pct=float(row["slippage_pct"] or 0.0), opened_at=row["opened_at"],
    )


def _any_open_trade_wants_finer(dsn: str, symbol: str, provider_name: str) -> bool:
    """True if any OPEN trade for this symbol+provider was opened wanting a finer reconcile
    timeframe — so we fetch M1 to honour its FROZEN granularity, not just the current config's."""
    for row in open_shadow_trades(dsn, symbol=symbol):
        if row.get("data_provider") and row["data_provider"] != provider_name:
            continue
        cfg = shadow_config_from_costs(row.get("costs"), timeout_bars=row.get("timeout_bars"))
        if cfg.reconcile_timeframe != TRIGGER_TF:
            return True
    return False


def reconcile_open_trades(dsn: str, coarse_bars: list[Candle], *, symbol: str, provider_name: str,
                          now: datetime | None = None,
                          shadow_config: ShadowConfig | None = None,
                          fine_bars: list[Candle] | None = None) -> int:
    """Reconcile OPEN shadow trades for `symbol` ACROSS ALL RUNS against `coarse_bars` (the trigger
    timeframe), optionally using the finer `fine_bars` (e.g. M1) PER TRADE when they calendar-cover
    that trade's window from entry. Update (close) those hit in place; returns how many closed.

    Across-runs matters: a new run_id (after a config/release change) must still drain positions
    left open by a PREVIOUS run — each trade is closed under ITS OWN run_id, reconciled with the
    ShadowConfig it was OPENED with (rebuilt from its stored cost manifest, incl. its frozen
    reconcile granularity), NEVER the current live config.

    FAIL-CLOSED coverage: if neither timeframe calendar-covers [opened_at, now] (e.g. the trade is
    older than the fetched window), the trade is left UNMODIFIED and a warning is logged — never
    expired/closed on incomplete data where an earlier SL/TP could hide in an uncovered gap."""
    now = now or datetime.now(timezone.utc)
    calendar = calendar_for(provider_name)   # fail-closed for an unknown provider
    fallback = shadow_config or ShadowConfig()
    fine_bars = fine_bars or []
    closed = 0
    quarantined = 0
    for row in open_shadow_trades(dsn, symbol=symbol):     # ACROSS runs, not just the current one
        # FROZEN SOURCE: a trade opened under provider X must be reconciled with X's bars + calendar,
        # never the current provider's (closing a Polygon trade with XTB bars would be wrong). This
        # tick only holds the current provider's bars, so a mismatched-source trade CANNOT be
        # reconciled here. QUARANTINE it (P0-C2): move it out of 'open' so it stops blocking the
        # symbol-wide position gate forever, WITHOUT pretending it was reconciled — it stays visible
        # for a manual drain/migration on its own provider.
        if row.get("data_provider") and row["data_provider"] != provider_name:
            reason = (f"source mismatch: opened under provider={row['data_provider']}, current "
                      f"process provider={provider_name}; cannot reconcile with foreign bars")
            n = quarantine_shadow_trade(dsn, decision_id=row["decision_id"],
                                        run_id=row["run_id"], reason=reason)
            log.warning("reconcile QUARANTINED (source mismatch): trade run=%s decision=%s opened "
                        "under provider=%s, current provider=%s (rows=%d)", row.get("run_id"),
                        row.get("decision_id"), row["data_provider"], provider_name, n)
            quarantined += n
            continue
        trade = _to_trade(row)
        trade_run_id = row["run_id"]                       # close under the trade's OWN run
        cfg = shadow_config_from_costs(row.get("costs"), timeout_bars=row.get("timeout_bars"),
                                       fallback=fallback)
        # Per-trade FROZEN granularity — not the current settings' reconcile_timeframe.
        bars, tf_used, fell_back, covered = select_reconcile_bars_for_trade(
            fine_bars, coarse_bars, opened_at=trade.opened_at, now=now,
            want_tf=cfg.reconcile_timeframe, trigger_tf=TRIGGER_TF, calendar=calendar)
        if not covered:
            # The window from entry is not fully covered by open-market bars — cannot trust any
            # conclusion (a touch could hide in the gap). Leave the trade open and alert.
            log.warning("reconcile skipped (uncovered window) run=%s decision=%s opened_at=%s",
                        trade_run_id, row.get("decision_id"), trade.opened_at.isoformat())
            continue
        # Record the granularity that ACTUALLY produced this R (per trade; may be a fallback).
        cfg = cfg.model_copy(update={"reconcile_timeframe": tf_used})
        outcome = reconcile(trade, bars, cfg)
        if outcome.status != "open":
            costs = cost_manifest(trade, cfg)
            if fell_back:
                costs["reconcile_fallback"] = True   # wanted finer bars, reconciled at the trigger TF
            upsert_shadow_trade(dsn, decision_id=row["decision_id"], run_id=trade_run_id,
                                symbol=row["symbol"], trade=trade, outcome=outcome,
                                timeframe=TRIGGER_TF, timeout_bars=cfg.timeout_bars,
                                costs=costs,
                                # WHEN we observed the close (now), not the bar's close_time — so
                                # a downtime-delayed observation can't be injected retroactively.
                                observed_at=datetime.now(timezone.utc))
            closed += 1
    if quarantined:
        log.warning("reconcile quarantined %d cross-provider trade(s) for %s (now excluded from the "
                    "position gate; awaiting manual drain)", quarantined, symbol)
    return closed


async def _fetch_finer_bars(provider, provider_symbol: str, tf: str, timeout_bars: int,
                            now: datetime) -> list[Candle]:
    """Best-effort finer (e.g. M1) bars covering a full timeout window, for intrabar reconciliation.
    Returns [] on an EXPECTED provider issue (unsupported timeframe like XTB's M1, or a transient
    network error) so the caller falls back to the trigger timeframe — recorded per trade via
    `reconcile_fallback`. A programming error is NOT swallowed: it propagates to the tick's handler."""
    try:
        count = timeout_bars * timeframe_minutes(TRIGGER_TF) + 60   # cover the whole hold window
        return only_closed(await provider.get_ohlcv(provider_symbol, tf, count), now)
    except TRANSIENT:   # ProviderError + network/transient types (see app.jobs._transient_types)
        return []


async def shadow_tick(settings: Settings, provider, provider_name: str, *, decision_maker,
                      run_id: str, model_name: str = "deterministic-confluence",
                      shadow_config: ShadowConfig | None = None, live_router=None) -> dict:
    shadow_config = shadow_config or ShadowConfig()
    now = datetime.now(timezone.utc)
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)

    # VERIFY THE RUN CONFIG FIRST — before any fetch or DB mutation. A process started with a config
    # incompatible with this run_id must abort with ZERO side effects; the old order reconciled
    # (mutating open trades) and could return at the position gate without ever checking the
    # manifest. The execution manifest depends only on config/versions, so it is known here.
    exec_manifest = build_online_exec_manifest(settings, shadow_config=shadow_config,
                                               model_name=model_name, provider_name=provider_name)
    exec_hash = execution_hash(exec_manifest)
    assert_run_manifest(settings.db_dsn, run_id, exec_manifest, exec_hash)   # raises on mismatch

    windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
    closes = m15_closes(windows)
    if not closes:
        return {"status": "no_bars"}
    as_of = closes[-1]
    summary: dict = {"as_of": as_of.isoformat()}

    # DOWNTIME is detected + persisted by observed_shadow_tick AFTER this bar is fully processed
    # (against the last *processed* bar, not the last *decision* — see P0-C3). shadow_tick only
    # tags each exit's `outcome` so the wrapper can record this bar in the processed-bars ledger.
    summary["outcome"] = "error"   # overwritten below at every clean exit; 'error' if we fall out

    # 1) RECONCILE FIRST: close any open trade the new bars just hit, BEFORE considering a new
    #    entry — so we never stack a new position on one the same bars should have closed.
    #    Fetch finer (M1) bars when the current config wants them OR any open (same-provider) trade
    #    was OPENED wanting M1 — so a frozen-M1 trade is NOT artificially degraded to M15 just
    #    because the current process runs M15. The per-trade decision to use them is still made
    #    against each trade's frozen config inside reconcile_open_trades.
    fine_bars: list[Candle] = []
    want_fine = (shadow_config.reconcile_timeframe != TRIGGER_TF
                 or _any_open_trade_wants_finer(settings.db_dsn, brain_symbol, provider_name))
    if want_fine:
        fine_bars = await _fetch_finer_bars(provider, provider_symbol, "1min",
                                            shadow_config.timeout_bars, now)
    summary["reconciled_closed"] = reconcile_open_trades(
        settings.db_dsn, windows[TRIGGER_TF], symbol=brain_symbol, provider_name=provider_name,
        now=now, shadow_config=shadow_config, fine_bars=fine_bars)

    # 2) POSITION GATE: one position per SYMBOL at a time, across ALL runs. If a trade for this
    #    symbol is still open anywhere (incl. a previous run), do NOT decide or open another — this
    #    both matches single-position and blocks stacking a new run on a not-yet-drained old one.
    if open_shadow_trades(settings.db_dsn, symbol=brain_symbol):
        summary["decision"] = "skipped:position_open"
        summary["outcome"] = "position_open"     # PROCESSED (gate respected) — NOT downtime
        return summary

    packet = build_packet_from_windows(
        windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
        provider_symbol=provider_symbol, ingested_at=now,
    )
    spread_pct, basis = await observe_xtb_spread(settings, packet.price, packet.bar_close)
    quote_time = observed_at = None
    if basis is not None:
        packet = packet.model_copy(update={"spread_pct": spread_pct, "basis_observed": basis})
        if basis.get("quote_time"):
            quote_time = datetime.fromisoformat(basis["quote_time"])
        if basis.get("observed_at"):
            observed_at = datetime.fromisoformat(basis["observed_at"])
    eval_now = datetime.now(timezone.utc)
    result = compute_eligibility(windows, as_of, settings, mode="online", now=eval_now,
                                 quote_time=quote_time, provider_name=provider_name)

    # FEEDBACK (Faza 5): the as_of-safe shadow track record, part of the decision input so the
    # model can learn from its own closed trades. Only trades closed before `as_of` are included.
    _fb = build_feedback(settings.db_dsn, run_id=run_id, before=as_of)
    feedback = FeedbackContext(regime_performance=_fb["regime_performance"],
                               recent_trades=_fb["recent_trades"])

    # The fingerprint includes feedback AND the execution config (a decision made with a different
    # track record or a different config is a different decision). The run manifest (exec_hash) was
    # already verified at the top, before any mutation.
    fingerprint = decision_fingerprint(
        input_hash=build_decision_input(packet, mode="online", feedback=feedback).input_hash(),
        model=model_name, provider=provider_name, risk_config_version=RiskConfig().version,
        execution_hash=exec_hash)

    # RESERVE BEFORE THE (paid) LLM. Two processes on the same run must not both call and pay: the
    # loser gets 'held' and skips WITHOUT paying (the old find_decision->call->insert let both pay
    # and the loser's paid call vanished). 'done' means the FULL chain already exists (decision +
    # trade are written atomically), so it's safe to skip.
    worker = f"{socket.gethostname()}:{os.getpid()}"
    claim, token = reserve_decision(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                    worker=worker)
    if claim == "held":
        summary["decision"] = "skipped:held_by_other"
        summary["outcome"] = "held"
        return summary
    if claim == "done":
        summary["decision"] = "skipped:already_decided"
        summary["outcome"] = "already_decided"
        return summary

    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    summary["snapshot"] = f"{status}:{snap_id}"
    if snap_id is None or status == "conflict":
        complete_decision_reservation(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                      status="failed", claim_token=token)
        summary["outcome"] = "error"     # snapshot conflict — not cleanly processed
        return summary
    # Spread + eligibility are separate append-only facts about the snapshot.
    spread_obs_id = None
    if basis is not None and packet.spread_pct is not None:
        spread_obs_id = insert_spread_observation(
            settings.db_dsn, snapshot_id=snap_id, spread_pct=packet.spread_pct,
            provenance="observed_xtb", observed_at=observed_at or eval_now,
            quote_time=quote_time, basis=basis,
        )
    eval_id = insert_evaluation(settings.db_dsn, snap_id, result)
    record = await run_decision(packet, result, decision_maker, mode="online",
                                prefilter_config=_prefilter_config(settings),
                                risk_config=RiskConfig(),
                                calendar=calendar_for(provider_name), feedback=feedback)
    last = getattr(decision_maker, "last_result", None)
    if record.stage == "llm_failed":
        if last is not None:   # a failed call yielded no decision -> audit it unlinked
            insert_llm_call(settings.db_dsn, last, snapshot_id=snap_id)
        complete_decision_reservation(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                      status="failed", claim_token=token)
        summary["decision"] = f"llm_failed:{record.llm_error}"
        summary["outcome"] = "llm_failed"
        return summary

    inp = build_decision_input(packet, mode="online", feedback=feedback)
    # Build the trade (if any) BEFORE persisting, so decision + audit + open trade go in ONE
    # transaction — a crash can't leave a committed decision without its trade (online had no
    # recovery for that window, unlike the backtest).
    open_trade = None
    if record.risk_approved:
        observed_mid = None
        if basis is not None and basis.get("xtb_bid") and basis.get("xtb_ask"):
            observed_mid = (basis["xtb_bid"] + basis["xtb_ask"]) / 2
        trade = open_virtual_trade(
            record.decision.direction, observed_mid or packet.price,
            record.risk.sl_pct, record.risk.tp_pct,
            spread_pct=packet.spread_pct or settings.replay_spread_pct,
            spread_provenance="observed_xtb" if packet.spread_pct else "modeled",
            slippage_pct=settings.slippage_pct, opened_at=observed_at or eval_now,
        )
        open_trade = {"symbol": brain_symbol, "trade": trade,
                      "outcome": reconcile(trade, [], shadow_config), "timeframe": TRIGGER_TF,
                      "timeout_bars": shadow_config.timeout_bars,
                      "costs": cost_manifest(trade, shadow_config), "observed_at": None}
    dec_id, inserted = insert_decision(
        settings.db_dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name,
        record=record, ai_input=inp.model_dump(mode="json"),
        ai_output=record.decision.model_dump(mode="json") if record.decision else None,
        mode="shadow", data_provider=provider_name, run_id=run_id,
        input_fingerprint=fingerprint, spread_observation_id=spread_obs_id, llm_result=last,
        open_trade=open_trade,
    )
    # 'done' only after the full chain is on disk. If the decision already existed (a crash
    # between the atomic commit and this completion, reclaimed on a later tick), the chain is
    # already complete — just finalise.
    complete_decision_reservation(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                  status="done", claim_token=token, decision_id=dec_id)
    summary["decision"] = f"{record.stage}:{record.decision.direction.value if record.decision else '-'}"
    summary["outcome"] = "decided"
    if open_trade is not None:
        summary["opened_trade"] = "opened" if inserted else "exists"

    # LIVE ORDER — last, and only after the decision + shadow chain are already on disk. Ordering
    # matters: routing first would risk an order with no record of why it was sent. This way the
    # worst case is an order placed but not yet linked, and the router's position gate catches
    # that on the next tick because it reads the ACCOUNT rather than our own state.
    if live_router is not None and record.risk_approved and record.decision is not None:
        route = await live_router.route(
            symbol=brain_symbol, direction=record.decision.direction,
            approved=record.risk_approved, sl_pct=record.risk.sl_pct,
            tp_pct=record.risk.tp_pct, confidence=record.decision.confidence,
            as_of=packet.bar_close)
        summary["live"] = (f"placed:{route.external_id}" if route.placed
                           else f"skipped:{route.reason}")
        log.info("live route: %s", summary["live"])

    return summary


async def observed_shadow_tick(settings: Settings, provider, provider_name: str, *,
                               decision_maker, run_id: str, model_name: str,
                               shadow_config: ShadowConfig,
                               telemetry: OperationalTelemetry, live_router=None) -> dict:
    """Run and audit one online shadow tick without changing the decision semantics."""
    operation_id = telemetry.start_run(
        "shadow_tick", symbol=settings.symbol_query, experiment_id=run_id)
    telemetry.heartbeat("healthy", details={"phase": "deciding", "run_id": run_id,
                                              "operation_id": operation_id})
    try:
        summary = await shadow_tick(
            settings, provider, provider_name, decision_maker=decision_maker,
            run_id=run_id, model_name=model_name, shadow_config=shadow_config,
            live_router=live_router)
    except asyncio.CancelledError as exc:
        telemetry.finish_run(operation_id, "cancelled", error=exc)
        raise
    except TRANSIENT as exc:
        telemetry.finish_run(operation_id, "transient_error", error=exc)
        telemetry.heartbeat("degraded", error=exc,
                            details={"run_id": run_id, "operation_id": operation_id})
        raise
    except Exception as exc:
        telemetry.finish_run(operation_id, "failed", error=exc)
        telemetry.heartbeat("error", error=exc,
                            details={"run_id": run_id, "operation_id": operation_id})
        raise
    # P0-C3: downtime + processed-bar ledger, AFTER the bar was handled (not before it finalises),
    # measured against the last *processed* bar (not the last decision) — so a bar the position gate
    # skipped, being processed, does NOT register as downtime.
    _record_processed_and_downtime(settings, provider_name, run_id, summary)
    telemetry.finish_run(operation_id, "success", bars_processed=int("as_of" in summary),
                         result=summary)
    telemetry.heartbeat("healthy", success=True,
                        details={"run_id": run_id, "operation_id": operation_id,
                                 "result": summary})
    return summary


async def shadow_tick_with_retries(settings: Settings, provider, provider_name: str, *,
                                   decision_maker, run_id: str, model_name: str,
                                   shadow_config: ShadowConfig,
                                   telemetry: OperationalTelemetry,
                                   attempts: int = 3, base_delay_seconds: float = 5.0,
                                   live_router=None) -> dict:
    """Retry only declared transient failures, soon enough not to lose the M15 decision bar."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return await observed_shadow_tick(
                settings, provider, provider_name, decision_maker=decision_maker,
                run_id=run_id, model_name=model_name, shadow_config=shadow_config,
                telemetry=telemetry, live_router=live_router)
        except TRANSIENT:
            if attempt == attempts:
                raise
            delay = base_delay_seconds * attempt
            log.warning("shadow tick transient failure; retry %d/%d in %.1fs",
                        attempt + 1, attempts, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _prefilter_config(settings: Settings) -> PrefilterConfig:
    """Prefilter with the run's blocked-regime list, so which regimes we refuse is a measurable
    choice rather than a hardcoded one."""
    return PrefilterConfig(blocked_regimes=list(settings.prefilter_blocked_regimes))


def _shadow_config(settings: Settings) -> ShadowConfig:
    return shadow_config_from_settings(settings)


class CallCapReached(RuntimeError):
    """The online loop hit its logical paid-call cap and must stop."""


def _build_maker(settings: Settings, kind: str, *, run_id: str | None = None,
                 persist_dsn: str | None = None):
    """Select the shadow decision maker. Returns (maker, model_name) so the persisted decision
    records which maker produced it.

    `claude` was refused outright here until this loop grew the same guards the backtest runner
    has, because an unbounded paid loop is not something to leave one flag away. It now requires
    ALL of them and still fails closed if any is missing:
      - a hard `--max-llm-calls` cap, enforced by _CountingMaker and checked BEFORE each tick;
      - persistence + run_id, so every call lands in the audit ledger (the gateway demands it);
      - the central financial gateway (master switch, model allowlist, USD budgets, per-attempt
        audit) — the same one the runner uses;
      - an explicit cost confirmation at startup;
      - the Anthropic client closed in a finally (see _close_maker).
    """
    if kind == "deterministic":
        return ConfluenceStrategy(), "deterministic-confluence", False
    if kind == "claude":
        if not (run_id and persist_dsn):
            raise SystemExit(
                "--maker claude needs a run_id and a database: every paid call must land in the "
                "audit ledger. Configure BRAIN_DB_DSN and pass --run-id.")
        from decision.paid_gateway import PaidAiGateway
        from shadow.runner import _CountingMaker

        gateway = PaidAiGateway(settings, run_id=run_id, persist_dsn=persist_dsn,
                                context="shadow.online")
        return _CountingMaker(gateway), settings.decision_model, True
    raise SystemExit(f"unknown --maker {kind!r} (expected deterministic|claude)")


async def _close_maker(maker) -> None:
    """Close the Anthropic client a paid maker holds. The loop is long-lived, so leaking the
    client here is not a tidy-up detail — it leaks a connection pool for the process lifetime."""
    inner = getattr(maker, "inner", maker)
    aclose = getattr(inner, "aclose", None)
    if aclose is not None:
        await aclose()


def _calls_made(maker) -> int:
    return getattr(maker, "calls", 0)


async def _once(settings: Settings, run_id: str, maker, model_name: str,
                live_router=None) -> None:
    provider = build_provider(settings)
    telemetry = OperationalTelemetry(settings.db_dsn, "shadow_online_once")
    telemetry.heartbeat("starting", details={"run_id": run_id, "maker": model_name})
    try:
        summary = await observed_shadow_tick(
            settings, provider, settings.market_data_provider, decision_maker=maker,
            run_id=run_id, model_name=model_name, shadow_config=_shadow_config(settings),
            telemetry=telemetry, live_router=live_router)
    finally:
        try:
            telemetry.stop()
        finally:
            try:
                aclose = getattr(provider, "aclose", None)
                if aclose:
                    await aclose()
            finally:
                await _close_maker(maker)
    print(f"[shadow-online] {summary}")


async def _loop(settings: Settings, run_id: str, maker, model_name: str,
                offset_seconds: float = 5.0, max_llm_calls: int | None = None,
                deadline=None, live_router=None) -> None:
    """Continuous shadow-online. `max_llm_calls` and `deadline` bound a PAID run: the cap is
    checked before each tick (never mid-decision, so a decision is never half-paid-for), and the
    deadline ends a timed session. Both stop the loop cleanly through the same finally that
    closes the provider and the maker."""
    provider = build_provider(settings)
    telemetry = OperationalTelemetry(settings.db_dsn, "shadow_online")
    telemetry.heartbeat("starting", details={"run_id": run_id, "maker": model_name,
                                              "provider": settings.market_data_provider})
    try:
        while True:
            # Budget/time checks BEFORE the tick: stopping mid-decision would leave a paid call
            # without its persisted chain.
            if max_llm_calls is not None and _calls_made(maker) >= max_llm_calls:
                log.info("call cap reached (%d) — stopping", max_llm_calls)
                return
            if deadline is not None and datetime.now(timezone.utc) >= deadline:
                log.info("deadline reached — stopping after %d call(s)", _calls_made(maker))
                return
            tick_error = None
            try:
                summary = await shadow_tick_with_retries(
                    settings, provider, settings.market_data_provider,
                    decision_maker=maker, run_id=run_id, model_name=model_name,
                    shadow_config=_shadow_config(settings), telemetry=telemetry,
                    live_router=live_router)
                log.info("shadow tick: %s", summary)
            except Exception as exc:  # noqa: BLE001 — a tick error must not kill the loop
                tick_error = exc
                log.exception("shadow tick failed (continuing)")
            wake = next_m15(datetime.now(timezone.utc)) + timedelta(seconds=offset_seconds)
            telemetry.heartbeat("healthy" if tick_error is None else "error",
                                next_wake_at=wake, error=tick_error,
                                details={"phase": "sleeping", "run_id": run_id,
                                         "maker": model_name})
            # Never sleep past the deadline — a timed session must end on time, not at the next
            # M15 close after it.
            sleep_for = max(1.0, (wake - datetime.now(timezone.utc)).total_seconds())
            if deadline is not None:
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    return
                sleep_for = min(sleep_for, remaining)
            await asyncio.sleep(sleep_for)
    finally:
        try:
            telemetry.stop()
        finally:
            try:
                aclose = getattr(provider, "aclose", None)
                if aclose:
                    await aclose()
            finally:
                await _close_maker(maker)


def build_online_exec_manifest(settings: Settings, *, shadow_config: ShadowConfig,
                               model_name: str, provider_name: str) -> dict:
    """The FULL executable manifest that identifies (and pins) a shadow-online run: financing/cost
    config, risk + prefilter + eligibility policy, the market calendar, the SYMBOL and its provider
    mapping, the timeframes, the maker/provider, and the git provenance. resolve_run_id() hashes
    THIS so the default run_id changes with ANY of them — a new build/config never lands in an
    incompatible old run."""
    brain_symbol = settings.symbol_query
    manifest = execution_manifest(
        modeled_spread_pct=settings.replay_spread_pct, slippage_pct=settings.slippage_pct,
        config=shadow_config, single_position=True, cooldown_bars=0,
        risk_config=RiskConfig().model_dump(mode="json"),
        prefilter_config=_prefilter_config(settings).model_dump(mode="json"),
        eligibility_policy=_eligibility_config(settings).as_policy(),
        calendar_version=calendar_for(provider_name).version)
    manifest.update({
        "run_kind": "shadow_online",
        "maker": model_name,
        "provider": provider_name,
        "symbol": brain_symbol,
        "provider_symbol": settings.provider_symbol(brain_symbol),   # provider_symbol_map identity
        "timeframes": list(settings.timeframes),
        "strategy_version": STRATEGY_VERSION,
        "feedback": True,
        **git_metadata(),                                            # commit/branch/dirty -> release id
    })
    return manifest


def resolve_run_id(cli_run_id: str | None, settings: Settings, *, model_name: str,
                   provider_name: str) -> str:
    """Explicit --run-id wins; else BRAIN_RUN_ID (settings.run_id); else a default derived from the
    FULL executable manifest hash (config + symbol + provider mapping + timeframes + model + git),
    so any material change — including a new build — starts a NEW run instead of colliding with an
    incompatible old one (RunConfigMismatch)."""
    if cli_run_id:
        return cli_run_id
    if settings.run_id:
        return settings.run_id
    cfg = shadow_config_from_settings(settings)
    digest = execution_hash(build_online_exec_manifest(
        settings, shadow_config=cfg, model_name=model_name, provider_name=provider_name))
    log.warning("no --run-id / BRAIN_RUN_ID set; using the manifest-derived default "
                "shadow-online-%s-%s. Set BRAIN_RUN_ID to a stable release id to keep one "
                "experiment identity across builds.", STRATEGY_VERSION, digest)
    return f"shadow-online-{STRATEGY_VERSION}-{digest}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Continuous shadow-online (no execution).")
    parser.add_argument("--once", action="store_true", help="run one tick then exit")
    parser.add_argument("--run-id", default=None,
                        help="experiment id (else BRAIN_RUN_ID, else a strategy-versioned default)")
    parser.add_argument("--maker", choices=["deterministic", "claude"], default="deterministic",
                        help="decision maker: deterministic (free) or claude (paid API)")
    parser.add_argument("--max-llm-calls", type=int, default=None,
                        help="hard cap on paid decisions; REQUIRED with --maker claude")
    parser.add_argument("--minutes", type=float, default=None,
                        help="stop after this many minutes (timed session)")
    parser.add_argument("--yes", action="store_true", help="skip the paid-run confirmation prompt")
    parser.add_argument("--live", action="store_true",
                        help="ROUTE REAL ORDERS for approved verdicts (demo account by default). "
                             "trading_hands must also have TRADING_ENABLED=true — this flag "
                             "cannot override its refusal.")
    args = parser.parse_args()
    settings = load_settings()            # Settings validators run here (bad config -> hard exit)
    _shadow_config(settings)              # build+validate the ShadowConfig ONCE at startup, not per tick

    # A paid ONLINE loop must be bounded before it starts. Without a cap this runs forever, so
    # the cap is required rather than defaulted — a default would be a number nobody chose.
    if args.maker == "claude" and args.max_llm_calls is None:
        raise SystemExit("--maker claude requires --max-llm-calls N (this loop would otherwise "
                         "run unbounded)")
    if args.max_llm_calls is not None and args.max_llm_calls < 1:
        raise SystemExit("--max-llm-calls must be >= 1")

    persist_dsn = settings.db_dsn
    provisional_run_id = resolve_run_id(args.run_id, settings, model_name=settings.decision_model,
                                        provider_name=settings.market_data_provider)
    maker, model_name, is_paid = _build_maker(settings, args.maker, run_id=provisional_run_id,
                                              persist_dsn=persist_dsn)
    run_id = resolve_run_id(args.run_id, settings, model_name=model_name,
                            provider_name=settings.market_data_provider)
    if is_paid:
        from shadow.runner import _confirm_paid_run
        _confirm_paid_run(model_name, args.max_llm_calls, settings.decision_max_tokens, args.yes,
                          http_attempts_per_call=settings.paid_max_http_attempts)
    live_router = None
    if args.live:
        from brokers_bridge.trading_hands import TradingHandsClient
        from execution.live_router import LiveRouter, live_config_from_settings

        live_cfg = live_config_from_settings(settings).model_copy(update={"enabled": True})
        live_client = TradingHandsClient(settings.trading_hands_url,
                                         settings.http_timeout_seconds)
        live_router = LiveRouter(live_client, live_cfg)
        log.warning("LIVE ORDER ROUTING ENABLED: volume=%s max_positions=%d cooldown=%.0fm "
                    "demo_only=%s", live_cfg.volume, live_cfg.max_open_positions,
                    live_cfg.cooldown_minutes, live_cfg.require_demo)

    deadline = (datetime.now(timezone.utc) + timedelta(minutes=args.minutes)
                if args.minutes else None)
    log.info("shadow online run_id=%s maker=%s cap=%s deadline=%s",
             run_id, model_name, args.max_llm_calls, deadline)
    try:
        if args.once:
            asyncio.run(_once(settings, run_id, maker, model_name, live_router=live_router))
        else:
            asyncio.run(_loop(settings, run_id, maker, model_name,
                              max_llm_calls=args.max_llm_calls, deadline=deadline,
                              live_router=live_router))
    except KeyboardInterrupt:
        log.info("shadow online stopped by operator")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
