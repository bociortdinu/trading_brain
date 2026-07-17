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
from datetime import datetime, timedelta, timezone

from app.collect import (
    build_packet_from_windows,
    compute_eligibility,
    fetch_windows,
    m15_closes,
    observe_xtb_spread,
)
from app.jobs import next_m15
from config.settings import Settings, load_settings
from core.models import Direction
from data_collector.providers.base import Candle
from data_collector.providers.factory import build_provider
from data_collector.session import calendar_for
from database.repository import (
    find_decision_by_fingerprint,
    insert_decision,
    insert_evaluation,
    insert_llm_call,
    insert_spread_observation,
    open_shadow_trades,
    upsert_shadow_trade,
    upsert_snapshot,
)
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig
from decision.schema import build_decision_input, decision_fingerprint
from features.mtf import TRIGGER_TF
from risk.engine import RiskConfig
from shadow.reconciler import reconcile
from shadow.runner import ConfluenceStrategy
from shadow.virtual_broker import ShadowConfig, VirtualTrade, cost_manifest, open_virtual_trade

log = logging.getLogger(__name__)
DEFAULT_RUN_ID = "shadow-online-confluence"


def _to_trade(row: dict) -> VirtualTrade:
    return VirtualTrade(
        direction=Direction.BUY if row["side"] == "buy" else Direction.SELL,
        entry_mid=float(row["entry_price"]), sl_price=float(row["sl_price"]),
        tp_price=float(row["tp_price"]), spread_pct=float(row["spread_pct"] or 0.0),
        spread_provenance=row["spread_provenance"] or "modeled",
        slippage_pct=float(row["slippage_pct"] or 0.0), opened_at=row["opened_at"],
    )


def reconcile_open_trades(dsn: str, m15_bars: list[Candle], *, run_id: str,
                          shadow_config: ShadowConfig | None = None) -> int:
    """Reconcile OPEN shadow trades against the latest M15 bars; update (close) those hit in
    place. Returns how many transitioned out of 'open'."""
    shadow_config = shadow_config or ShadowConfig()
    closed = 0
    for row in open_shadow_trades(dsn, run_id):
        trade = _to_trade(row)
        outcome = reconcile(trade, m15_bars, shadow_config)
        if outcome.status != "open":
            upsert_shadow_trade(dsn, decision_id=row["decision_id"], run_id=run_id,
                                symbol=row["symbol"], trade=trade, outcome=outcome,
                                timeframe=TRIGGER_TF, timeout_bars=shadow_config.timeout_bars,
                                costs=cost_manifest(trade, shadow_config))
            closed += 1
    return closed


async def shadow_tick(settings: Settings, provider, provider_name: str, *, decision_maker,
                      run_id: str, model_name: str = "deterministic-confluence",
                      shadow_config: ShadowConfig | None = None) -> dict:
    shadow_config = shadow_config or ShadowConfig()
    now = datetime.now(timezone.utc)
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)
    windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
    closes = m15_closes(windows)
    if not closes:
        return {"status": "no_bars"}
    as_of = closes[-1]
    summary: dict = {"as_of": as_of.isoformat()}

    # 1) RECONCILE FIRST: close any open trade the new bars just hit, BEFORE considering a new
    #    entry — so we never stack a new position on one the same bars should have closed.
    summary["reconciled_closed"] = reconcile_open_trades(
        settings.db_dsn, windows[TRIGGER_TF], run_id=run_id, shadow_config=shadow_config)

    # 2) POSITION GATE (matches the backtest): one position per run at a time. If a trade is
    #    still open after reconciliation, do NOT decide or open another (also saves an LLM call).
    if open_shadow_trades(settings.db_dsn, run_id):
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

    # DEDUPE BEFORE THE (paid) LLM: if this exact input was already decided in this run (same M15
    # bar reprocessed after a restart), skip — don't re-call the model or duplicate the audit.
    fingerprint = decision_fingerprint(
        input_hash=build_decision_input(packet, mode="online").input_hash(),
        model=model_name, provider=provider_name, risk_config_version=RiskConfig().version)
    if find_decision_by_fingerprint(settings.db_dsn, input_fingerprint=fingerprint, run_id=run_id) is not None:
        summary["decision"] = "skipped:already_decided"
        return summary

    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    summary["snapshot"] = f"{status}:{snap_id}"
    if snap_id is not None and status != "conflict":
        # The observed spread is a SEPARATE append-only fact about the snapshot; the decision
        # below records exactly which observation it consumed.
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
                                    calendar=calendar_for(provider_name))
        last = getattr(decision_maker, "last_result", None)
        if last is not None:
            insert_llm_call(settings.db_dsn, last, snapshot_id=snap_id)
        if record.stage == "llm_failed":
            summary["decision"] = f"llm_failed:{record.llm_error}"
        else:
            inp = build_decision_input(packet, mode="online")
            # ATOMIC + idempotent on (input_fingerprint, run_id): a concurrent/duplicate insert
            # returns the existing decision id instead of creating a second row.
            dec_id = insert_decision(
                settings.db_dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name,
                record=record, ai_input=inp.model_dump(mode="json"),
                ai_output=record.decision.model_dump(mode="json") if record.decision else None,
                mode="shadow", data_provider=provider_name, run_id=run_id,
                input_fingerprint=fingerprint, spread_observation_id=spread_obs_id,
            )
            summary["decision"] = f"{record.stage}:{record.decision.direction.value if record.decision else '-'}"
            if record.risk_approved:
                # Online: fill at the OBSERVED quote mid (captures real latency), not the bar close.
                observed_mid = None
                if basis is not None and basis.get("xtb_bid") and basis.get("xtb_ask"):
                    observed_mid = (basis["xtb_bid"] + basis["xtb_ask"]) / 2
                # opened_at is the LOCAL OBSERVATION time (basis.observed_at — when the brain saw
                # the quote), not the broker tick time nor the bar close. The reconciler must not
                # count M15 movement that happened before the real entry.
                trade = open_virtual_trade(
                    record.decision.direction, observed_mid or packet.price,
                    record.risk.sl_pct, record.risk.tp_pct,
                    spread_pct=packet.spread_pct or settings.replay_spread_pct,
                    spread_provenance="observed_xtb" if packet.spread_pct else "modeled",
                    slippage_pct=settings.slippage_pct, opened_at=observed_at or eval_now,
                )
                tid, _ = upsert_shadow_trade(
                    settings.db_dsn, decision_id=dec_id, run_id=run_id, symbol=brain_symbol,
                    trade=trade, outcome=reconcile(trade, [], shadow_config), timeframe=TRIGGER_TF,
                    timeout_bars=shadow_config.timeout_bars, costs=cost_manifest(trade, shadow_config),
                )
                summary["opened_trade"] = tid

    return summary


def _shadow_config(settings: Settings) -> ShadowConfig:
    return ShadowConfig(commission_pct=settings.commission_pct,
                        swap_pct_per_night=settings.swap_pct_per_night)


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
    try:
        summary = await shadow_tick(settings, provider, settings.market_data_provider,
                                    decision_maker=maker, run_id=run_id, model_name=model_name,
                                    shadow_config=_shadow_config(settings))
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()
    print(f"[shadow-online] {summary}")


async def _loop(settings: Settings, run_id: str, maker, model_name: str,
                offset_seconds: float = 5.0) -> None:
    provider = build_provider(settings)
    try:
        while True:
            try:
                summary = await shadow_tick(settings, provider, settings.market_data_provider,
                                            decision_maker=maker, run_id=run_id,
                                            model_name=model_name,
                                            shadow_config=_shadow_config(settings))
                log.info("shadow tick: %s", summary)
            except Exception:  # noqa: BLE001 — a tick error must not kill the loop
                log.exception("shadow tick failed (continuing)")
            wake = next_m15(datetime.now(timezone.utc)) + timedelta(seconds=offset_seconds)
            await asyncio.sleep(max(1.0, (wake - datetime.now(timezone.utc)).total_seconds()))
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Continuous shadow-online (no execution).")
    parser.add_argument("--once", action="store_true", help="run one tick then exit")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="experiment id for shadow trades")
    parser.add_argument("--maker", choices=["deterministic", "claude"], default="deterministic",
                        help="decision maker: deterministic (free) or claude (paid API)")
    args = parser.parse_args()
    settings = load_settings()
    maker, model_name = _build_maker(settings, args.maker)
    if args.once:
        asyncio.run(_once(settings, args.run_id, maker, model_name))
    else:
        asyncio.run(_loop(settings, args.run_id, maker, model_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
