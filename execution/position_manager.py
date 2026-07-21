"""Live position manager — the exit half of the trade lifecycle.

The router opens positions. Without this, nothing ever closed them: SL/TP sit at the broker so
they do fire, but a position that touches neither would stay open indefinitely, accruing swap,
while the shadow side recorded a `timeout` exit at 24h and computed an R for a trade that was
still running. The track record and the account would simply diverge.

Broker truth, not simulation. A live position's outcome is read back from the account rather
than reconciled against bars — that is the whole point of trading on demo instead of modelling.
Three things can happen to an open live trade, and each tick resolves exactly one:

  1. still held, inside the horizon      -> leave it alone
  2. still held, past the horizon        -> close it and record the exit we caused
  3. no longer held                      -> the broker closed it (SL or TP fired); record that

Case 3 is where the data is thin, and the honesty matters. The xStation CoreAPI does not publish
a closed position's fill price, close time, or which level was hit (see
trading_hands/docs/IPAX_CLOSED_POSITIONS.md — that lives on a separate gRPC-Web API which is not
yet decoded). The tempting move is to record the exit AT the SL or TP level, since one of them
must have fired. That would be wrong often enough to matter: slippage and gaps mean the fill is
frequently not that level, and every R derived from it would inherit the fiction while looking
like a measurement. So such an exit is recorded with no price and no R, and says so.

Closing the ipax gap is what turns those into real outcomes. Until then a live track record has
two populations — trades we closed (priced, R known) and trades the broker closed (unpriced) —
and they must not be averaged together.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from brokers_bridge.trading_hands import TradingHandsClient
from data_collector.providers.base import timeframe_minutes

MANAGER_VERSION = "position-manager-2026.1"


class ManagedExit(BaseModel):
    """What happened to one live position this tick."""
    trade_id: int
    external_id: str
    action: str                       # "held" | "closed_timeout" | "closed_by_broker" | "close_failed"
    exit_reason: str | None = None
    realized_pnl: float | None = None
    r_multiple: float | None = None
    detail: str | None = None


class ManagerReport(BaseModel):
    checked: int = 0
    exits: list[ManagedExit] = Field(default_factory=list)
    orphans: list[str] = Field(default_factory=list)   # broker positions we have no record of


def horizon_for(trade: dict, default_timeout_bars: int = 96) -> timedelta:
    """The day-trading hold horizon for this trade, from the values FROZEN on it at open.

    Read off the row rather than the live config so a config change cannot retroactively move
    the deadline of a position that is already running."""
    bars = trade.get("timeout_bars") or default_timeout_bars
    tf = trade.get("timeframe") or "15min"
    return timedelta(minutes=bars * timeframe_minutes(tf))


def r_from_prices(trade: dict, exit_price: float) -> float | None:
    """R from PRICES, which is the only place we can get it honestly.

    1R is the entry-to-stop distance, so R = (exit - entry) / (entry - stop), signed by
    direction. This needs an exit price — available when WE close the position, not when the
    broker does (see the module docstring). No price, no R; a P&L figure in account currency
    cannot substitute, because the currency-converted risk unit is not known here."""
    entry, stop = float(trade["entry_price"]), float(trade["sl_price"])
    risk = abs(entry - stop)
    if risk == 0:
        return None
    move = exit_price - entry if trade["side"] == "buy" else entry - exit_price
    return round(move / risk, 4)


class LivePositionManager:
    """Resolves open live positions against the account, every tick."""

    def __init__(self, client: TradingHandsClient, dsn: str, *, now_fn=None) -> None:
        self._client = client
        self._dsn = dsn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _realized_pnl(trade, balance_now, vanished, open_trades) -> tuple[float | None, str]:
        """Realized P&L for a position the broker closed, from the account balance delta.

        Returns (pnl, why) — `why` explains a None so a missing figure is never mistaken for a
        zero result. Attribution is refused unless this is the only live position we are
        tracking: with several in flight the delta covers every realization between the two
        instants and cannot honestly be split.
        """
        baseline = trade.get("balance_at_open")
        if baseline is None:
            return None, "no opening balance recorded (trade predates the column)"
        if balance_now is None:
            return None, "balance unavailable"
        if len(open_trades) > 1:
            return None, f"{len(open_trades)} live positions tracked — delta not attributable"
        return round(float(balance_now) - float(baseline), 2), "single position in flight"

    async def sweep(self, *, symbol: str | None = None) -> ManagerReport:
        from database.repository import close_live_trade, open_live_trades

        report = ManagerReport()
        open_trades = open_live_trades(self._dsn, symbol=symbol)
        positions = await self._client.positions()
        held = {p.external_id: p for p in positions}
        report.checked = len(open_trades)

        # Balance is read ONCE per sweep, and only when something has actually disappeared —
        # there is nothing to price otherwise. Balance moves on REALIZATION only (an open
        # position marks equity, not balance), so the delta from a trade's opening balance is
        # its realized result.
        vanished = [t for t in open_trades if t["external_id"] not in held]
        balance_now = (await self._client.balance()).balance if vanished else None

        # A position at the broker that no trade row claims. It cannot be managed — we do not
        # know its horizon or intent — so it is reported, never silently closed.
        known = {t["external_id"] for t in open_trades}
        report.orphans = [eid for eid in held if eid not in known]

        for trade in open_trades:
            eid = trade["external_id"]
            now = self._now()

            if eid not in held:
                # The broker closed it: SL or TP fired. The CoreAPI publishes neither the fill
                # price nor which level was hit, so both are recorded as unknown. Inventing an
                # exit at the SL or TP level would be the tempting move and the wrong one —
                # slippage and gaps mean the fill is frequently NOT that level, and every R
                # derived from it would inherit the fiction.
                pnl, why = self._realized_pnl(trade, balance_now, vanished, open_trades)
                close_live_trade(
                    self._dsn, trade_id=trade["id"], exit_price=None,
                    exit_reason="manual", closed_at=now,
                    realized_pnl=pnl, r_multiple=None, observed_at=now)
                report.exits.append(ManagedExit(
                    trade_id=trade["id"], external_id=eid, action="closed_by_broker",
                    exit_reason="broker_sl_or_tp", realized_pnl=pnl,
                    detail=f"P&L from balance delta ({why}); fill price and level unknown — the "
                           f"CoreAPI does not publish a closed position's outcome, and it is not "
                           f"invented here (see IPAX_CLOSED_POSITIONS.md)"))
                continue

            deadline = trade["opened_at"] + horizon_for(trade)
            if now < deadline:
                report.exits.append(ManagedExit(trade_id=trade["id"], external_id=eid,
                                                action="held"))
                continue

            # Past the horizon: this is a day-trading system, so we close it ourselves. Here we
            # DO know the exit — we caused it — so the fill is recorded.
            result = await self._client.close(eid)
            if not result.accepted:
                report.exits.append(ManagedExit(
                    trade_id=trade["id"], external_id=eid, action="close_failed",
                    detail="broker rejected the close; will retry next tick"))
                continue
            quote = await self._client.quote(trade["symbol"])
            fill = quote.bid if trade["side"] == "buy" else quote.ask
            r = r_from_prices(trade, fill)
            close_live_trade(self._dsn, trade_id=trade["id"], exit_price=fill,
                             exit_reason="timeout", closed_at=now, realized_pnl=None,
                             r_multiple=r, observed_at=now)
            report.exits.append(ManagedExit(
                trade_id=trade["id"], external_id=eid, action="closed_timeout",
                exit_reason="timeout", r_multiple=r,
                detail=f"held past {horizon_for(trade)}"))

        return report
