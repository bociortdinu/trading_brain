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
    last_decision_as_of,
    open_shadow_trades,
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


def _to_trade(row: dict) -> VirtualTrade:
    return VirtualTrade(
        direction=Direction.BUY if row["side"] == "buy" else Direction.SELL,
        entry_mid=float(row["entry_price"]), sl_price=float(row["sl_price"]),
        tp_price=float(row["tp_price"]), spread_pct=float(row["spread_pct"] or 0.0),
        spread_provenance=row["spread_provenance"] or "modeled",
        slippage_pct=float(row["slippage_pct"] or 0.0), opened_at=row["opened_at"],
    )


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
    for row in open_shadow_trades(dsn, symbol=symbol):     # ACROSS runs, not just the current one
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
                      shadow_config: ShadowConfig | None = None) -> dict:
    shadow_config = shadow_config or ShadowConfig()
    now = datetime.now(timezone.utc)
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)

    # VERIFY THE RUN CONFIG FIRST — before any fetch or DB mutation. A process started with a config
    # incompatible with this run_id must abort with ZERO side effects; the old order reconciled
    # (mutating open trades) and could return at the position gate without ever checking the
    # manifest. The execution manifest depends only on config/versions, so it is known here.
    exec_manifest = execution_manifest(
        modeled_spread_pct=settings.replay_spread_pct, slippage_pct=settings.slippage_pct,
        config=shadow_config, single_position=True, cooldown_bars=0,
        risk_config=RiskConfig().model_dump(mode="json"),
        prefilter_config=PrefilterConfig().model_dump(mode="json"),
        eligibility_policy=_eligibility_config(settings).as_policy(),
        calendar_version=calendar_for(provider_name).version)
    exec_manifest.update({
        "run_kind": "shadow_online",
        "maker": model_name,
        "provider": provider_name,
        "symbol": brain_symbol,
        "feedback": True,
        **git_metadata(),
    })
    exec_hash = execution_hash(exec_manifest)
    assert_run_manifest(settings.db_dsn, run_id, exec_manifest, exec_hash)   # raises on mismatch

    windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
    closes = m15_closes(windows)
    if not closes:
        return {"status": "no_bars"}
    as_of = closes[-1]
    summary: dict = {"as_of": as_of.isoformat()}

    # DOWNTIME CATCH-UP (visibility): each tick decides only the LATEST bar, so after downtime the
    # bars between the last decision and now are skipped. Count and record the gap (calendar-aware,
    # so a weekend is not a gap) — the track record is only "continuous" if this stays 0.
    prev = last_decision_as_of(settings.db_dsn, run_id, brain_symbol, provider_name)
    missed = count_missed_open_bars(prev, as_of, calendar_for(provider_name))
    if missed:
        summary["missed_bars"] = missed
        log.warning("downtime gap: %d open-market bar(s) skipped between %s and %s (run=%s) — the "
                    "track record is not continuous over this gap", missed,
                    prev.isoformat() if prev else "?", as_of.isoformat(), run_id)

    # 1) RECONCILE FIRST: close any open trade the new bars just hit, BEFORE considering a new
    #    entry — so we never stack a new position on one the same bars should have closed. Prefer
    #    finer (M1) bars for intrabar SL/TP ordering when configured and available; else fall back
    #    to the trigger timeframe and record that the R was measured coarsely.
    # Best-effort provisioning of finer bars (current settings decide whether to fetch M1); the
    # PER-TRADE decision to actually use them is made against each trade's frozen config inside
    # reconcile_open_trades. A frozen-M1 trade under a now-M15 config simply falls back (flagged).
    fine_bars: list[Candle] = []
    if shadow_config.reconcile_timeframe != TRIGGER_TF:
        fine_bars = await _fetch_finer_bars(provider, provider_symbol,
                                            shadow_config.reconcile_timeframe,
                                            shadow_config.timeout_bars, now)
    summary["reconciled_closed"] = reconcile_open_trades(
        settings.db_dsn, windows[TRIGGER_TF], symbol=brain_symbol, provider_name=provider_name,
        now=now, shadow_config=shadow_config, fine_bars=fine_bars)

    # 2) POSITION GATE: one position per SYMBOL at a time, across ALL runs. If a trade for this
    #    symbol is still open anywhere (incl. a previous run), do NOT decide or open another — this
    #    both matches single-position and blocks stacking a new run on a not-yet-drained old one.
    if open_shadow_trades(settings.db_dsn, symbol=brain_symbol):
        summary["decision"] = "skipped:position_open"
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
        return summary
    if claim == "done":
        summary["decision"] = "skipped:already_decided"
        return summary

    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    summary["snapshot"] = f"{status}:{snap_id}"
    if snap_id is None or status == "conflict":
        complete_decision_reservation(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                      status="failed", claim_token=token)
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
                                prefilter_config=PrefilterConfig(), risk_config=RiskConfig(),
                                calendar=calendar_for(provider_name), feedback=feedback)
    last = getattr(decision_maker, "last_result", None)
    if record.stage == "llm_failed":
        if last is not None:   # a failed call yielded no decision -> audit it unlinked
            insert_llm_call(settings.db_dsn, last, snapshot_id=snap_id)
        complete_decision_reservation(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id,
                                      status="failed", claim_token=token)
        summary["decision"] = f"llm_failed:{record.llm_error}"
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
    if open_trade is not None:
        summary["opened_trade"] = "opened" if inserted else "exists"

    return summary


async def observed_shadow_tick(settings: Settings, provider, provider_name: str, *,
                               decision_maker, run_id: str, model_name: str,
                               shadow_config: ShadowConfig,
                               telemetry: OperationalTelemetry) -> dict:
    """Run and audit one online shadow tick without changing the decision semantics."""
    operation_id = telemetry.start_run(
        "shadow_tick", symbol=settings.symbol_query, experiment_id=run_id)
    telemetry.heartbeat("healthy", details={"phase": "deciding", "run_id": run_id,
                                              "operation_id": operation_id})
    try:
        summary = await shadow_tick(
            settings, provider, provider_name, decision_maker=decision_maker,
            run_id=run_id, model_name=model_name, shadow_config=shadow_config)
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
                                   attempts: int = 3, base_delay_seconds: float = 5.0) -> dict:
    """Retry only declared transient failures, soon enough not to lose the M15 decision bar."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    for attempt in range(1, attempts + 1):
        try:
            return await observed_shadow_tick(
                settings, provider, provider_name, decision_maker=decision_maker,
                run_id=run_id, model_name=model_name, shadow_config=shadow_config,
                telemetry=telemetry)
        except TRANSIENT:
            if attempt == attempts:
                raise
            delay = base_delay_seconds * attempt
            log.warning("shadow tick transient failure; retry %d/%d in %.1fs",
                        attempt + 1, attempts, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _shadow_config(settings: Settings) -> ShadowConfig:
    return shadow_config_from_settings(settings)


def _build_maker(settings: Settings, kind: str):
    """Select the shadow decision maker. Returns (maker, model_name) so the persisted decision
    records which maker produced it.

    `claude` is DELIBERATELY REFUSED here. The backtest runner guards a paid run (a hard
    --max-llm-calls cap, a worst-case cost estimate, an explicit confirmation, and closing the
    Anthropic client in a finally); this loop has NONE of that and runs unbounded, so enabling it
    would mean an open-ended spend with no ceiling and a leaked client. Fail closed until those
    guards exist here too — an unbounded paid loop is not something to leave one flag away.
    """
    if kind == "deterministic":
        return ConfluenceStrategy(), "deterministic-confluence"
    if kind == "claude":
        raise SystemExit(
            "--maker claude is disabled for shadow-online: this loop has no call cap, no cost "
            "estimate/confirmation and does not close the Anthropic client, so it would spend "
            "without a ceiling. Use `python -m shadow.runner --maker claude --max-llm-calls N` "
            "(guarded) to measure the model, or run online with --maker deterministic."
        )
    raise SystemExit(f"unknown --maker {kind!r} (expected deterministic|claude)")


async def _once(settings: Settings, run_id: str, maker, model_name: str) -> None:
    provider = build_provider(settings)
    telemetry = OperationalTelemetry(settings.db_dsn, "shadow_online_once")
    telemetry.heartbeat("starting", details={"run_id": run_id, "maker": model_name})
    try:
        summary = await observed_shadow_tick(
            settings, provider, settings.market_data_provider, decision_maker=maker,
            run_id=run_id, model_name=model_name, shadow_config=_shadow_config(settings),
            telemetry=telemetry)
    finally:
        try:
            telemetry.stop()
        finally:
            aclose = getattr(provider, "aclose", None)
            if aclose:
                await aclose()
    print(f"[shadow-online] {summary}")


async def _loop(settings: Settings, run_id: str, maker, model_name: str,
                offset_seconds: float = 5.0) -> None:
    provider = build_provider(settings)
    telemetry = OperationalTelemetry(settings.db_dsn, "shadow_online")
    telemetry.heartbeat("starting", details={"run_id": run_id, "maker": model_name,
                                              "provider": settings.market_data_provider})
    try:
        while True:
            tick_error = None
            try:
                summary = await shadow_tick_with_retries(
                    settings, provider, settings.market_data_provider,
                    decision_maker=maker, run_id=run_id, model_name=model_name,
                    shadow_config=_shadow_config(settings), telemetry=telemetry)
                log.info("shadow tick: %s", summary)
            except Exception as exc:  # noqa: BLE001 — a tick error must not kill the loop
                tick_error = exc
                log.exception("shadow tick failed (continuing)")
            wake = next_m15(datetime.now(timezone.utc)) + timedelta(seconds=offset_seconds)
            telemetry.heartbeat("healthy" if tick_error is None else "error",
                                next_wake_at=wake, error=tick_error,
                                details={"phase": "sleeping", "run_id": run_id,
                                         "maker": model_name})
            await asyncio.sleep(max(1.0, (wake - datetime.now(timezone.utc)).total_seconds()))
    finally:
        try:
            telemetry.stop()
        finally:
            aclose = getattr(provider, "aclose", None)
            if aclose:
                await aclose()


def config_digest(settings: Settings) -> str:
    """Short, stable hash of the config that determines a shadow run's OUTCOMES — cost/financing
    config, eligibility policy, risk/prefilter config, calendar (provider) and strategy version.
    A change to ANY of these yields a new default run_id, so an incompatible config never silently
    lands in an old run (RunConfigMismatch)."""
    import hashlib
    import json

    from app.collect import _eligibility_config
    from decision.prefilter import PrefilterConfig
    from risk.engine import RiskConfig
    blob = {
        "shadow": shadow_config_from_settings(settings).model_dump(mode="json"),
        "risk": RiskConfig().model_dump(mode="json"),
        "prefilter": PrefilterConfig().model_dump(mode="json"),
        "eligibility": _eligibility_config(settings).as_policy(),
        "provider": settings.market_data_provider,   # picks the market calendar
        "strategy": STRATEGY_VERSION,
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:10]


def resolve_run_id(cli_run_id: str | None, settings: Settings) -> str:
    """Explicit --run-id wins; else BRAIN_RUN_ID (settings.run_id); else a default that embeds the
    strategy version AND a config digest — so changing costs/RiskConfig/eligibility/calendar forces
    a NEW run rather than a silent RunConfigMismatch against an incompatible old run."""
    if cli_run_id:
        return cli_run_id
    if settings.run_id:
        return settings.run_id
    digest = config_digest(settings)
    log.warning("no --run-id / BRAIN_RUN_ID set; using the config-derived default "
                "shadow-online-%s-%s. Set BRAIN_RUN_ID to a release/build id for a stable "
                "experiment identity across code changes.", STRATEGY_VERSION, digest)
    return f"shadow-online-{STRATEGY_VERSION}-{digest}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Continuous shadow-online (no execution).")
    parser.add_argument("--once", action="store_true", help="run one tick then exit")
    parser.add_argument("--run-id", default=None,
                        help="experiment id (else BRAIN_RUN_ID, else a strategy-versioned default)")
    parser.add_argument("--maker", choices=["deterministic", "claude"], default="deterministic",
                        help="decision maker: deterministic (free) or claude (paid API)")
    args = parser.parse_args()
    settings = load_settings()            # Settings validators run here (bad config -> hard exit)
    _shadow_config(settings)              # build+validate the ShadowConfig ONCE at startup, not per tick
    run_id = resolve_run_id(args.run_id, settings)
    log.info("shadow online run_id=%s", run_id)
    maker, model_name = _build_maker(settings, args.maker)
    try:
        if args.once:
            asyncio.run(_once(settings, run_id, maker, model_name))
        else:
            asyncio.run(_loop(settings, run_id, maker, model_name))
    except KeyboardInterrupt:
        log.info("shadow online stopped by operator")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
