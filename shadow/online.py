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
from database.repository import (
    insert_decision,
    insert_evaluation,
    insert_llm_call,
    open_shadow_trades,
    upsert_shadow_trade,
    upsert_snapshot,
)
from decision.pipeline import run_decision
from decision.prefilter import PrefilterConfig
from decision.schema import build_decision_input
from features.mtf import TRIGGER_TF
from risk.engine import RiskConfig
from shadow.reconciler import reconcile
from shadow.runner import ConfluenceStrategy
from shadow.virtual_broker import ShadowConfig, VirtualTrade, open_virtual_trade

log = logging.getLogger(__name__)
DEFAULT_RUN_ID = "shadow-online-confluence"


def _to_trade(row: dict) -> VirtualTrade:
    return VirtualTrade(
        direction=Direction.BUY if row["side"] == "buy" else Direction.SELL,
        entry_mid=float(row["entry_price"]), sl_price=float(row["sl_price"]),
        tp_price=float(row["tp_price"]), spread_pct=float(row["spread_pct"] or 0.0),
        spread_provenance=row["spread_provenance"] or "modeled", opened_at=row["opened_at"],
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
                                timeframe=TRIGGER_TF, timeout_bars=shadow_config.timeout_bars)
            closed += 1
    return closed


async def shadow_tick(settings: Settings, provider, provider_name: str, *, decision_maker,
                      run_id: str, model_name: str = "deterministic-confluence") -> dict:
    now = datetime.now(timezone.utc)
    brain_symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(brain_symbol)
    windows = await fetch_windows(provider, provider_symbol, settings.timeframes, now)
    closes = m15_closes(windows)
    if not closes:
        return {"status": "no_bars"}
    as_of = closes[-1]
    packet = build_packet_from_windows(
        windows, as_of, brain_symbol=brain_symbol, provider_name=provider_name,
        provider_symbol=provider_symbol, ingested_at=now,
    )
    spread_pct, basis = await observe_xtb_spread(settings, packet.price, packet.bar_close)
    quote_time = None
    if basis is not None:
        packet = packet.model_copy(update={"spread_pct": spread_pct, "basis_observed": basis})
        if basis.get("quote_time"):
            quote_time = datetime.fromisoformat(basis["quote_time"])
    eval_now = datetime.now(timezone.utc)
    result = compute_eligibility(windows, as_of, settings, mode="online", now=eval_now,
                                 quote_time=quote_time, provider_name=provider_name)

    summary: dict = {"as_of": as_of.isoformat()}
    status, snap_id = upsert_snapshot(settings.db_dsn, packet)
    summary["snapshot"] = f"{status}:{snap_id}"
    if snap_id is not None and status != "conflict":
        eval_id = insert_evaluation(settings.db_dsn, snap_id, result)
        record = await run_decision(packet, result, decision_maker, mode="online",
                                    prefilter_config=PrefilterConfig(), risk_config=RiskConfig())
        last = getattr(decision_maker, "last_result", None)
        if last is not None:
            insert_llm_call(settings.db_dsn, last, snapshot_id=snap_id)
        if record.stage == "llm_failed":
            summary["decision"] = f"llm_failed:{record.llm_error}"
        else:
            inp = build_decision_input(packet, mode="online")
            dec_id = insert_decision(
                settings.db_dsn, snapshot_id=snap_id, evaluation_id=eval_id, model=model_name,
                record=record, ai_input=inp.model_dump(mode="json"),
                ai_output=record.decision.model_dump(mode="json") if record.decision else None,
                mode="shadow", data_provider=provider_name,
            )
            summary["decision"] = f"{record.stage}:{record.decision.direction.value if record.decision else '-'}"
            if record.risk_approved:
                trade = open_virtual_trade(
                    record.decision.direction, packet.price, record.risk.sl_pct, record.risk.tp_pct,
                    spread_pct=packet.spread_pct or settings.replay_spread_pct,
                    spread_provenance="observed_xtb" if packet.spread_pct else "modeled", opened_at=as_of,
                )
                tid, _ = upsert_shadow_trade(
                    settings.db_dsn, decision_id=dec_id, run_id=run_id, symbol=brain_symbol,
                    trade=trade, outcome=reconcile(trade, []), timeframe=TRIGGER_TF,
                    timeout_bars=ShadowConfig().timeout_bars,
                )
                summary["opened_trade"] = tid

    summary["reconciled_closed"] = reconcile_open_trades(settings.db_dsn, windows[TRIGGER_TF], run_id=run_id)
    return summary


async def _once(settings: Settings, run_id: str) -> None:
    provider = build_provider(settings)
    try:
        summary = await shadow_tick(settings, provider, settings.market_data_provider,
                                    decision_maker=ConfluenceStrategy(), run_id=run_id)
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose:
            await aclose()
    print(f"[shadow-online] {summary}")


async def _loop(settings: Settings, run_id: str, offset_seconds: float = 5.0) -> None:
    provider = build_provider(settings)
    maker = ConfluenceStrategy()
    try:
        while True:
            try:
                summary = await shadow_tick(settings, provider, settings.market_data_provider,
                                            decision_maker=maker, run_id=run_id)
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
    args = parser.parse_args()
    settings = load_settings()
    if args.once:
        asyncio.run(_once(settings, args.run_id))
    else:
        asyncio.run(_loop(settings, args.run_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
