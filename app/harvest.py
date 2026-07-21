"""Harvest closed OHLCV bars from the live provider into a PERSISTENT on-disk archive.

Why this exists
---------------
`shadow.runner` fetches its bars from `build_provider(settings)` on every run, and the XTB
CoreAPI serves a ROLLING window from a session-bound connection. Two consequences:

  1. A backtest needs `trading_hands` authenticated and the market reachable — you cannot
     replay on a weekend, and two runs days apart do not see the same bars.
  2. `freeze_dataset` content-hashes the bars it just fetched but never STORES them, so a
     `dataset_id` proves *that* data changed, not what it was. Once XTB's window rolls past,
     the exact bytes a run used are unrecoverable.

This command closes that gap. It writes `{SYMBOL}_{timeframe}.csv` in exactly the format
`CsvMarketDataProvider` reads, and MERGES with what is already on disk — so repeated
harvests ACCUMULATE history far beyond the provider's rolling window. After one harvest the
whole backtest path runs offline, free, and reproducible:

    python -m app.harvest --out data/bars --count 10000        # while trading_hands is up
    BRAIN_MARKET_DATA_PROVIDER=csv BRAIN_CSV_DIR=data/bars \
        python -m shadow.runner --count 2500 --persist --run-id det-smoke

NO orders, NO paid AI calls, NO database writes — it only reads bars and writes CSV files.

Conflicts are fail-closed by design. If the provider returns a bar whose OHLCV differs from
the one already archived for the same `open_time`, the archive is not silently rewritten:
the run aborts and names the offending bars. `--on-conflict keep|replace` is the deliberate
escape hatch (a source may legitimately revise a bar's volume after the fact).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import pathlib
import tempfile
from datetime import datetime

from config.settings import Settings, load_settings
from data_collector.providers.base import Candle
from features.engineering import MIN_BARS
from features.mtf import TRIGGER_TF

CSV_FIELDS = ["open_time", "close_time", "open", "high", "low", "close", "volume"]

# A bar's identity is its open_time; these are the values that must not silently change.
_OHLCV = ("open", "high", "low", "close", "volume")


class HarvestConflict(RuntimeError):
    """An archived bar and a freshly fetched bar disagree for the same open_time."""


def _row(candle: Candle) -> dict[str, str]:
    return {
        "open_time": candle.open_time.isoformat(),
        "close_time": candle.close_time.isoformat(),
        "open": repr(candle.open),
        "high": repr(candle.high),
        "low": repr(candle.low),
        "close": repr(candle.close),
        "volume": repr(candle.volume),
    }


def read_archive(path: pathlib.Path) -> dict[datetime, dict[str, str]]:
    """Existing archive as {open_time: row}. Missing file = empty archive."""
    if not path.exists():
        return {}
    with path.open(newline="") as fh:
        return {datetime.fromisoformat(r["open_time"]): r for r in csv.DictReader(fh)}


def _differs(old: dict[str, str], new: dict[str, str]) -> bool:
    # Compare numerically: 1.0 and 1.00 are the same bar, not a conflict.
    return any(float(old[f]) != float(new[f]) for f in _OHLCV)


def merge_bars(existing: dict[datetime, dict[str, str]], fetched: list[Candle],
               on_conflict: str = "fail") -> tuple[list[dict[str, str]], int, list[datetime]]:
    """Union the archive with freshly fetched bars, oldest-first.

    Returns (rows, added_count, conflicts). With ``on_conflict='fail'`` a disagreement raises
    rather than corrupting the archive; 'keep' preserves the archived bar, 'replace' takes the
    freshly fetched one. Conflicting open_times are always reported.
    """
    merged = dict(existing)
    conflicts: list[datetime] = []
    added = 0
    for candle in fetched:
        new = _row(candle)
        old = merged.get(candle.open_time)
        if old is None:
            merged[candle.open_time] = new
            added += 1
            continue
        if _differs(old, new):
            conflicts.append(candle.open_time)
            if on_conflict == "replace":
                merged[candle.open_time] = new
    if conflicts and on_conflict == "fail":
        shown = ", ".join(t.isoformat() for t in sorted(conflicts)[:5])
        raise HarvestConflict(
            f"{len(conflicts)} archived bar(s) disagree with the provider (e.g. {shown}). "
            f"The archive was NOT modified. Re-run with --on-conflict keep (trust the archive) "
            f"or --on-conflict replace (trust the provider) once you know which is right.")
    rows = [merged[t] for t in sorted(merged)]
    return rows, added, conflicts


def write_archive(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    """Atomically replace the archive: a crash mid-write must never truncate history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def evaluable_bars(archives: dict[str, list[dict[str, str]]], trigger_tf: str = TRIGGER_TF,
                   min_bars: int = MIN_BARS) -> tuple[int, str | None]:
    """How many trigger-timeframe bars this archive can actually be backtested on.

    `timeframe_features` needs >= MIN_BARS closed bars in EVERY timeframe at each as_of, so the
    binding constraint is whichever timeframe's warmup ends LAST — usually the daily. Without
    this check an archive that looks full (thousands of M15 bars) silently yields
    `bars_evaluated=0`, which is indistinguishable from a broken pipeline.

    Returns (evaluable_count, binding_timeframe).
    """
    warmup_end: datetime | None = None
    binding: str | None = None
    for tf, rows in archives.items():
        if len(rows) < min_bars:
            return 0, tf
        ends = datetime.fromisoformat(rows[min_bars - 1]["open_time"])
        if warmup_end is None or ends > warmup_end:
            warmup_end, binding = ends, tf
    trigger = archives.get(trigger_tf, [])
    if warmup_end is None:
        return len(trigger), None
    usable = sum(1 for r in trigger if datetime.fromisoformat(r["open_time"]) >= warmup_end)
    return usable, binding


async def harvest(settings: Settings, out_dir: str, count: int, on_conflict: str,
                  provider=None) -> int:
    from data_collector.providers.factory import build_provider

    owns_provider = provider is None
    provider = provider or build_provider(settings)
    symbol = settings.symbol_query
    provider_symbol = settings.provider_symbol(symbol)
    out = pathlib.Path(out_dir)

    print(f"harvesting {symbol} (provider={settings.market_data_provider} "
          f"symbol={provider_symbol}) -> {out}/")
    total_added = 0
    archives: dict[str, list[dict[str, str]]] = {}
    try:
        for tf in settings.timeframes:
            path = out / f"{symbol}_{tf}.csv"
            existing = read_archive(path)
            fetched = await provider.get_ohlcv(provider_symbol, tf, count)
            rows, added, conflicts = merge_bars(existing, fetched, on_conflict)
            write_archive(path, rows)
            archives[tf] = rows
            total_added += added
            span = f"{rows[0]['open_time']} .. {rows[-1]['open_time']}" if rows else "empty"
            note = f"  [{len(conflicts)} CONFLICT(S) -> {on_conflict}]" if conflicts else ""
            print(f"  {tf:6s} fetched={len(fetched):5d} new={added:5d} "
                  f"archived={len(rows):6d}  {span}{note}")
    finally:
        if owns_provider:
            aclose = getattr(provider, "aclose", None)
            if aclose:
                await aclose()

    print(f"\n{total_added} new bar(s) added. Archive: {out}/")

    usable, binding = evaluable_bars(archives)
    if usable:
        print(f"Backtest-ready: ~{usable} evaluable {TRIGGER_TF} bars "
              f"(warmup bound by {binding}).")
        print("Replay offline (free, reproducible, no live session):")
        print(f"  BRAIN_MARKET_DATA_PROVIDER=csv BRAIN_CSV_DIR={out} "
              f"python -m shadow.runner --count {usable}")
        return 0
    print(f"NOT backtest-ready: 0 evaluable {TRIGGER_TF} bars — every timeframe needs "
          f"{MIN_BARS} bars of warmup before the first bar you want to evaluate, and "
          f"{binding!r} is short. Harvest {binding!r} deeper (raise --count) and re-run; "
          f"a backtest on this archive would report bars_evaluated=0.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest closed OHLCV bars into a persistent on-disk archive (no orders, "
                    "no paid AI, no DB writes).")
    parser.add_argument("--out", default="data/bars", help="archive directory (default: data/bars)")
    parser.add_argument("--count", type=int, default=2500,
                        help="bars to request per timeframe (default: 2500)")
    parser.add_argument("--on-conflict", choices=["fail", "keep", "replace"], default="fail",
                        help="when an archived bar disagrees with the provider (default: fail)")
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be positive")
    settings = load_settings()
    try:
        return asyncio.run(harvest(settings, args.out, args.count, args.on_conflict))
    except HarvestConflict as exc:
        print(f"\nHARVEST ABORTED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
