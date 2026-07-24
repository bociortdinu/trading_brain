"""Operator view + reconciliation for PAID AI spend (no API calls, DB only).

The central gateway records every paid attempt in `paid_attempts`. This tool lets the operator:
- see spend per run / UTC day / UTC month (the same accounting the budget enforces);
- list completed attempts NOT yet checked against the Anthropic console, and mark them reconciled
  (the GO-criterion: reconcile local cost vs the provider before scaling up);
- find + sweep ORPHANS (attempts stuck at 'started' — a request was sent but never finalized).

    python -m app.paid_report                         # summary
    python -m app.paid_report --run-id my-exp         # scope spend to one run
    python -m app.paid_report --list-unreconciled     # completed, not yet reconciled
    python -m app.paid_report --reconcile 123         # mark attempt 123 reconciled
    python -m app.paid_report --orphans               # stuck 'started' rows
    python -m app.paid_report --sweep-orphans --older 300   # flag stuck 'started' -> 'unknown'
"""

from __future__ import annotations

import argparse

from config.settings import load_settings
from database.repository import (
    mark_paid_attempt_reconciled,
    paid_attempts_needing_reconciliation,
    paid_spend_summary,
    stale_started_attempts,
    sweep_started_attempts_to_unknown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Paid AI spend report + console reconciliation (no API calls).")
    parser.add_argument("--run-id", help="scope the spend summary + unreconciled list to one run")
    parser.add_argument("--list-unreconciled", action="store_true",
                        help="list completed attempts not yet reconciled with the console")
    parser.add_argument("--reconcile", type=int, metavar="ID",
                        help="mark attempt ID as reconciled with the provider console")
    parser.add_argument("--orphans", action="store_true", help="list attempts stuck at 'started'")
    parser.add_argument("--sweep-orphans", action="store_true",
                        help="flag stuck 'started' attempts as 'unknown' (outcome-uncertain)")
    parser.add_argument("--older", type=int, default=300,
                        help="orphan age threshold in seconds (default 300)")
    args = parser.parse_args()
    dsn = load_settings().db_dsn

    if args.reconcile is not None:
        n = mark_paid_attempt_reconciled(dsn, args.reconcile)
        print(f"reconciled {n} attempt(s) (id={args.reconcile})"
              if n else f"no completed, un-reconciled attempt with id={args.reconcile}")
        return 0 if n else 1

    if args.sweep_orphans:
        n = sweep_started_attempts_to_unknown(dsn, args.older)
        print(f"swept {n} orphan 'started' attempt(s) older than {args.older}s -> 'unknown' "
              f"(reconcile their cost with the console)")
        return 0

    spend = paid_spend_summary(dsn, args.run_id)
    print("=== paid AI spend (USD; actual where known, else reserved estimate) ===")
    if args.run_id:
        print(f"run {args.run_id!r} : ${spend['run']:.4f}")
    print(f"today (UTC)   : ${spend['day']:.4f}")
    print(f"month (UTC)   : ${spend['month']:.4f}")

    unrec = paid_attempts_needing_reconciliation(dsn, args.run_id)
    orphans = stale_started_attempts(dsn, args.older)
    print(f"unreconciled completed attempts: {len(unrec)}   |   orphans (>{args.older}s 'started'): {len(orphans)}")

    if args.list_unreconciled:
        print("\n-- unreconciled (compare each with the Anthropic console, then --reconcile ID) --")
        for r in unrec:
            print(f"  id={r['id']} run={r['run_id']} ctx={r['context']} model={r['model']} "
                  f"req={r['request_id']} in/out={r['input_tokens']}/{r['output_tokens']} "
                  f"cost=${(r['actual_cost_usd'] or 0):.6f} at={r['finished_at']}")
    if args.orphans:
        print("\n-- orphans stuck at 'started' (a request may have been billed) --")
        for r in orphans:
            print(f"  id={r['id']} run={r['run_id']} ctx={r['context']} model={r['model']} "
                  f"est=${(r['est_cost_usd'] or 0):.6f} started_at={r['started_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
