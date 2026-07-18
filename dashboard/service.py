"""Read-only data service used by the local operator dashboard.

The dashboard deliberately has no mutation methods. Every PostgreSQL connection is put in a
READ ONLY transaction and the HTTP side talks only to trading_hands GET endpoints. This module
turns the normalized audit tables into one operator-oriented snapshot without exposing DSNs,
credentials, API keys, or raw broker tickets.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from config.settings import Settings
from data_collector.session import calendar_for

UTC = timezone.utc


def json_safe(value: Any) -> Any:
    """Recursively convert DB/Python values to JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Broker timestamps are milliseconds since epoch.
        return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _dt(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


class DashboardService:
    """Compose live broker health and persisted brain audit data into one response."""

    def __init__(self, settings: Settings, *, repo_root: Path | None = None) -> None:
        self.settings = settings
        self.repo_root = repo_root or Path(__file__).resolve().parents[1]
        self._hands_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._hands_cache_lock = threading.Lock()
        migrations = sorted((self.repo_root / "database" / "migrations").glob("*.sql"))
        self.expected_schema_version = migrations[-1].stem if migrations else None

    def state(self, *, symbol: str | None = None, run_id: str | None = None,
              limit: int = 50) -> dict[str, Any]:
        now = datetime.now(UTC)
        symbol = (symbol or self.settings.symbol_query).strip().upper()
        run_id = run_id.strip() if run_id and run_id.strip() else None
        limit = min(200, max(5, int(limit)))

        git = self._git_state()
        db = self._db_state(symbol=symbol, run_id=run_id, limit=limit)
        hands = self._hands_state(symbol=symbol)
        self._enrich_open_trades(db.get("open_trades") or [], hands.get("quote"), now)
        market_open = self._market_open(now)
        alerts = self._alerts(now=now, symbol=symbol, git=git, db=db, hands=hands,
                              market_open=market_open)

        return json_safe({
            "generated_at": now,
            "selection": {"symbol": symbol, "run_id": run_id, "limit": limit},
            "runtime": {
                "provider": self.settings.market_data_provider,
                "market_mode": self.settings.market_mode,
                "market_open": market_open,
                "expected_schema_version": self.expected_schema_version,
                "git": git,
            },
            "health": {"database": db["health"], "trading_hands": hands},
            "alerts": alerts,
            "summary": db["summary"],
            "latest": db["latest"],
            "timeline": db["timeline"],
            "open_trades": db["open_trades"],
            "runs": db["runs"],
            "llm_calls": db["llm_calls"],
            "reservations": db["reservations"],
            "services": db["services"],
            "pipeline_runs": db["pipeline_runs"],
        })

    def _market_open(self, now: datetime) -> bool | None:
        try:
            return bool(calendar_for(self.settings.market_data_provider).is_open(now))
        except Exception:  # dashboard health must survive an unknown provider/calendar
            return None

    def _git_state(self) -> dict[str, Any]:
        def run(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=self.repo_root, check=True, capture_output=True,
                text=True, timeout=2,
            ).stdout.strip()

        try:
            status = run("status", "--porcelain")
            commit = run("rev-parse", "--short", "HEAD")
            branch = run("branch", "--show-current")
            return {
                "ok": True, "commit": commit, "branch": branch,
                "dirty": bool(status), "changed_files": len(status.splitlines()) if status else 0,
            }
        except Exception as exc:  # noqa: BLE001 - displayed as a health fact
            return {"ok": False, "error": type(exc).__name__}

    def _db_state(self, *, symbol: str, run_id: str | None, limit: int) -> dict[str, Any]:
        started = time.perf_counter()
        empty = {
            "health": {"ok": False}, "summary": {}, "latest": {}, "timeline": [],
            "open_trades": [], "runs": [], "llm_calls": [], "reservations": [],
            "services": [], "pipeline_runs": [],
        }
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.settings.db_dsn, row_factory=dict_row,
                                 connect_timeout=3) as conn:
                # Defense in depth: even if a future query is accidentally changed, PostgreSQL
                # rejects INSERT/UPDATE/DELETE/DDL on this dashboard connection.
                conn.execute("SET TRANSACTION READ ONLY")
                schema_version = conn.execute(
                    "SELECT version, applied_at FROM schema_migrations "
                    "ORDER BY applied_at DESC, version DESC LIMIT 1"
                ).fetchone()
                tables = {r["table_name"] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"
                ).fetchall()}

                timeline = conn.execute(
                    """
                    SELECT s.id AS snapshot_id, s.bar_close, s.ts AS snapshot_created_at,
                           s.provider, s.provider_symbol, s.pipeline_version, s.regime,
                           s.adx_h1, s.atr_pct_m15,
                           NULLIF(s.features->>'price','')::numeric AS price,
                           s.features->>'confluence' AS confluence,
                           s.features->>'major_trend' AS major_trend,
                           s.features->>'macro_bias' AS macro_bias,
                           e.id AS evaluation_id, e.mode AS evaluation_mode,
                           e.eligible, e.reasons AS eligibility_reasons,
                           e.feed_lag_seconds, e.quote_lag_seconds, e.quote_present,
                           so.spread_pct, so.provenance AS spread_provenance,
                           so.quote_time, so.observed_at AS spread_observed_at,
                           d.id AS decision_id, d.ts AS decision_created_at, d.model,
                           d.direction, d.confidence, d.risk_verdict, d.risk_reason,
                           d.blocked_reason, COALESCE(d.run_id,t.run_id) AS run_id,
                           d.ai_output->>'rationale' AS rationale,
                           t.id AS trade_id, t.side, t.status AS trade_status,
                           t.entry_price, t.sl_price, t.tp_price, t.opened_at,
                           t.exit_price, t.exit_reason, t.closed_at, t.r_multiple,
                           t.r_pessimistic, t.r_optimistic, t.ambiguous
                    FROM market_snapshots s
                    LEFT JOIN LATERAL (
                        SELECT * FROM snapshot_evaluations x
                        WHERE x.snapshot_id=s.id ORDER BY x.evaluated_at DESC, x.id DESC LIMIT 1
                    ) e ON true
                    LEFT JOIN LATERAL (
                        SELECT * FROM spread_observations x
                        WHERE x.snapshot_id=s.id ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
                    ) so ON true
                    LEFT JOIN LATERAL (
                        SELECT * FROM decisions x
                        WHERE x.snapshot_id=s.id
                          AND (CAST(%s AS text) IS NULL OR x.run_id=%s OR EXISTS (
                              SELECT 1 FROM trades rt WHERE rt.decision_id=x.id AND rt.run_id=%s
                          ))
                        ORDER BY x.ts DESC, x.id DESC LIMIT 1
                    ) d ON true
                    LEFT JOIN LATERAL (
                        SELECT * FROM trades x
                        WHERE x.decision_id=d.id ORDER BY x.id DESC LIMIT 1
                    ) t ON true
                    WHERE s.symbol=%s
                      AND (CAST(%s AS text) IS NULL OR d.id IS NOT NULL)
                    ORDER BY s.bar_close DESC, s.id DESC
                    LIMIT %s
                    """,
                    (run_id, run_id, run_id, symbol, run_id, limit),
                ).fetchall()

                latest_market = conn.execute(
                    """
                    SELECT id AS snapshot_id, bar_close, provider, provider_symbol,
                           pipeline_version, regime, NULLIF(features->>'price','')::numeric AS price,
                           features->>'confluence' AS confluence, ingested_at
                    FROM market_snapshots WHERE symbol=%s
                    ORDER BY bar_close DESC, id DESC LIMIT 1
                    """, (symbol,),
                ).fetchone()

                open_trades = conn.execute(
                    """
                    SELECT t.id, t.run_id, t.symbol, t.side, t.opened_at, t.entry_price,
                           t.sl_price, t.tp_price, t.status, t.spread_pct,
                           t.spread_provenance, t.slippage_pct, t.costs, t.timeout_bars,
                           d.model, d.confidence, d.as_of AS decision_as_of,
                           CASE
                             WHEN rm.run_id IS NULL THEN 'legacy_unpinned'
                             WHEN COALESCE(rm.manifest->>'run_kind','')='shadow_online'
                               THEN 'shadow_online'
                             ELSE COALESCE(NULLIF(rm.manifest->>'run_kind',''), 'unclassified')
                           END AS run_kind,
                           CASE WHEN rm.run_id IS NOT NULL
                                      AND rm.manifest->>'run_kind' IN
                                          ('shadow_online','executable_backtest')
                                      AND rm.manifest->>'git_dirty'='false'
                                THEN 'verified' ELSE 'unverified' END AS validity
                    FROM trades t JOIN decisions d ON d.id=t.decision_id
                    LEFT JOIN run_manifests rm ON rm.run_id=t.run_id
                    WHERE t.symbol=%s AND t.status='open'
                      AND (CAST(%s AS text) IS NULL OR t.run_id=%s)
                    ORDER BY t.opened_at DESC LIMIT 50
                    """, (symbol, run_id, run_id),
                ).fetchall()

                if run_id is not None:
                    trade_scope_sql = "t.run_id=%s"
                    trade_scope_params: tuple[Any, ...] = (run_id,)
                    decision_scope_sql = "(d.run_id=%s OR EXISTS (SELECT 1 FROM trades rt WHERE rt.decision_id=d.id AND rt.run_id=%s))"
                    decision_scope_params: tuple[Any, ...] = (run_id, run_id)
                    metric_scope = "selected_run"
                else:
                    verified_runs = (
                        "SELECT rm.run_id FROM run_manifests rm "
                        "WHERE rm.manifest->>'run_kind' IN ('shadow_online','executable_backtest') "
                        "AND rm.manifest->>'git_dirty'='false'"
                    )
                    trade_scope_sql = f"t.run_id IN ({verified_runs}) AND t.symbol NOT LIKE 'TST_%%'"
                    trade_scope_params = ()
                    decision_scope_sql = (
                        f"d.run_id IN ({verified_runs}) AND s.symbol NOT LIKE 'TST_%%'"
                    )
                    decision_scope_params = ()
                    metric_scope = "verified_runs_only"

                metrics = conn.execute(
                    f"""
                    SELECT count(*) AS trades_total,
                           count(*) FILTER (WHERE status='open') AS trades_open,
                           count(*) FILTER (WHERE status<>'open') AS trades_closed,
                           count(*) FILTER (WHERE r_multiple>0) AS wins,
                           round(avg(r_multiple) FILTER (WHERE r_multiple IS NOT NULL), 4)
                               AS expectancy_r,
                           count(*) FILTER (WHERE ambiguous) AS ambiguous_trades
                    FROM trades t WHERE t.symbol=%s AND {trade_scope_sql}
                    """, (symbol, *trade_scope_params),
                ).fetchone()

                counts = conn.execute(
                    f"""
                    SELECT
                      (SELECT count(*) FROM market_snapshots WHERE symbol=%s) AS snapshots,
                      (SELECT count(*) FROM decisions d JOIN market_snapshots s
                         ON s.id=d.snapshot_id WHERE s.symbol=%s AND {decision_scope_sql}) AS decisions,
                      (SELECT count(*) FROM llm_calls l JOIN market_snapshots s
                         ON s.id=l.snapshot_id LEFT JOIN decisions d ON d.id=l.decision_id
                         WHERE s.symbol=%s AND {decision_scope_sql}) AS llm_calls,
                      (SELECT COALESCE(sum(l.estimated_cost_usd),0) FROM llm_calls l
                         JOIN market_snapshots s ON s.id=l.snapshot_id
                         LEFT JOIN decisions d ON d.id=l.decision_id
                         WHERE s.symbol=%s AND {decision_scope_sql}) AS llm_cost_usd,
                      (SELECT count(*) FROM market_snapshots WHERE symbol LIKE 'TST_%%') AS test_snapshots,
                      (SELECT count(*) FROM decisions d JOIN market_snapshots s ON s.id=d.snapshot_id
                         WHERE s.symbol=%s AND d.model LIKE 'claude%%') AS claude_decisions
                    """, (symbol,
                          symbol, *decision_scope_params,
                          symbol, *decision_scope_params,
                          symbol, *decision_scope_params,
                          symbol),
                ).fetchone()

                runs = conn.execute(
                    """
                    WITH da AS (
                      SELECT COALESCE(d.run_id,t.run_id) AS run_id,
                             count(DISTINCT d.id) AS decisions, max(d.ts) AS last_decision,
                             max(model) AS model,
                             bool_or(s.symbol LIKE 'TST_%%') AS test_data
                      FROM decisions d LEFT JOIN trades t ON t.decision_id=d.id
                      JOIN market_snapshots s ON s.id=d.snapshot_id
                      WHERE COALESCE(d.run_id,t.run_id) IS NOT NULL
                      GROUP BY COALESCE(d.run_id,t.run_id)
                    ), ta AS (
                      SELECT run_id, count(*) AS trades,
                             count(*) FILTER (WHERE status='open') AS open_trades,
                             count(*) FILTER (WHERE status<>'open') AS closed_trades,
                             round(avg(r_multiple) FILTER (WHERE r_multiple IS NOT NULL),4)
                               AS expectancy_r,
                             max(opened_at) AS last_trade,
                             bool_or(symbol LIKE 'TST_%%') AS test_data
                      FROM trades WHERE run_id IS NOT NULL GROUP BY run_id
                    )
                    SELECT COALESCE(da.run_id,ta.run_id) AS run_id, da.model, da.decisions,
                           ta.trades, ta.open_trades, ta.closed_trades, ta.expectancy_r,
                           da.last_decision, ta.last_trade, rm.manifest_hash, rm.created_at,
                           rm.manifest,
                           CASE
                             WHEN COALESCE(da.test_data,ta.test_data,false) THEN 'test_data'
                             WHEN rm.run_id IS NULL THEN 'legacy_unpinned'
                             ELSE COALESCE(NULLIF(rm.manifest->>'run_kind',''),'unclassified')
                           END AS run_kind,
                           CASE WHEN NOT COALESCE(da.test_data,ta.test_data,false)
                                      AND rm.manifest->>'run_kind' IN
                                          ('shadow_online','executable_backtest')
                                      AND rm.manifest->>'git_dirty'='false'
                                THEN 'verified' ELSE 'unverified' END AS validity
                           ,CASE
                             WHEN COALESCE(da.test_data,ta.test_data,false) THEN 'test_data'
                             WHEN rm.run_id IS NULL THEN 'missing_manifest'
                             WHEN rm.manifest->>'git_dirty' IS DISTINCT FROM 'false'
                               THEN 'dirty_or_unknown_worktree'
                             WHEN rm.manifest->>'run_kind' NOT IN
                                  ('shadow_online','executable_backtest')
                               THEN 'non_executable_run_kind'
                             ELSE 'verified'
                           END AS validity_reason
                    FROM da FULL OUTER JOIN ta ON ta.run_id=da.run_id
                    LEFT JOIN run_manifests rm ON rm.run_id=COALESCE(da.run_id,ta.run_id)
                    ORDER BY GREATEST(da.last_decision,ta.last_trade) DESC NULLS LAST
                    LIMIT 50
                    """
                ).fetchall()

                llm_calls = conn.execute(
                    """
                    SELECT l.id, l.ts, l.ok, l.error, l.requested_model, l.effective_model,
                           l.request_id, l.input_tokens, l.output_tokens, l.cache_read_tokens,
                           l.cache_creation_tokens, l.estimated_cost_usd, l.latency_ms,
                           l.retry_count, l.decision_id, s.symbol
                    FROM llm_calls l LEFT JOIN market_snapshots s ON s.id=l.snapshot_id
                    LEFT JOIN decisions d ON d.id=l.decision_id
                    WHERE (s.symbol=%s OR s.symbol IS NULL)
                      AND (CAST(%s AS text) IS NULL OR d.run_id=%s OR EXISTS (
                          SELECT 1 FROM trades rt WHERE rt.decision_id=d.id AND rt.run_id=%s
                      ))
                    ORDER BY l.ts DESC, l.id DESC LIMIT 50
                    """, (symbol, run_id, run_id, run_id),
                ).fetchall()

                reservations = conn.execute(
                    """
                    SELECT run_id, status, worker, reserved_at, lease_expires_at, decision_id
                    FROM decision_reservations
                    WHERE status<>'done' AND (CAST(%s AS text) IS NULL OR run_id=%s)
                    ORDER BY reserved_at DESC LIMIT 50
                    """, (run_id, run_id),
                ).fetchall()

                services = []
                if "service_heartbeats" in tables:
                    services = conn.execute(
                        """
                        SELECT service_name, instance_id, status, started_at, last_seen_at,
                               last_success_at, next_wake_at, last_error, details, git_commit
                        FROM service_heartbeats ORDER BY service_name
                        """
                    ).fetchall()

                pipeline_runs = []
                if "pipeline_runs" in tables:
                    pipeline_runs = conn.execute(
                        """
                        SELECT id, service_name, instance_id, run_kind, experiment_id, symbol,
                               started_at, finished_at, status, bars_processed, result,
                               error_type, error_message, git_commit
                        FROM pipeline_runs ORDER BY started_at DESC, id DESC LIMIT 80
                        """
                    ).fetchall()

                manifest_count = 0
                if "run_manifests" in tables:
                    manifest_count = conn.execute("SELECT count(*) AS n FROM run_manifests").fetchone()["n"]

            closed = int(metrics["trades_closed"] or 0)
            wins = int(metrics["wins"] or 0)
            summary = {
                **dict(counts), **dict(metrics), "win_rate": wins / closed if closed else None,
                "run_manifests": manifest_count,
                "metric_scope": metric_scope,
            }
            if run_id:
                selected = next((r for r in runs if r["run_id"] == run_id), None)
                summary["selected_run_validity"] = selected["validity"] if selected else "unknown"
                summary["selected_run_kind"] = selected["run_kind"] if selected else "unknown"
            latest = {
                "market": dict(latest_market) if latest_market else None,
                "pipeline": dict(timeline[0]) if timeline else None,
            }
            return {
                "health": {
                    "ok": True,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "schema_version": schema_version["version"] if schema_version else None,
                    "expected_schema_version": self.expected_schema_version,
                    "schema_current": bool(schema_version and
                                           schema_version["version"] == self.expected_schema_version),
                    "schema_applied_at": schema_version["applied_at"] if schema_version else None,
                    "tables": len(tables),
                },
                "summary": summary,
                "latest": latest,
                "timeline": [dict(r) for r in timeline],
                "open_trades": [dict(r) for r in open_trades],
                "runs": [dict(r) for r in runs],
                "llm_calls": [dict(r) for r in llm_calls],
                "reservations": [dict(r) for r in reservations],
                "services": [dict(r) for r in services],
                "pipeline_runs": [dict(r) for r in pipeline_runs],
            }
        except Exception as exc:  # noqa: BLE001 - API remains available when DB is down
            empty["health"] = {
                "ok": False, "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": type(exc).__name__, "message": str(exc)[:240],
            }
            return empty

    def _hands_state(self, *, symbol: str) -> dict[str, Any]:
        # Browsers auto-refresh concurrently; do not fan every tab into three CoreAPI calls.
        # Five seconds is short enough for an operator view and bounds pressure on trading_hands.
        now_monotonic = time.monotonic()
        with self._hands_cache_lock:
            cached = self._hands_cache.get(symbol)
            if cached and now_monotonic - cached[0] < 5.0:
                return cached[1]
        state = self._fetch_hands_state(symbol=symbol)
        with self._hands_cache_lock:
            self._hands_cache[symbol] = (time.monotonic(), state)
        return state

    def _fetch_hands_state(self, *, symbol: str) -> dict[str, Any]:
        started = time.perf_counter()
        state: dict[str, Any] = {"ok": False, "status": None, "quote": None, "candles": None}
        try:
            import httpx

            base = self.settings.trading_hands_url.rstrip("/")
            with httpx.Client(timeout=min(5.0, self.settings.http_timeout_seconds)) as client:
                response = client.get(f"{base}/status")
                response.raise_for_status()
                state["status"] = response.json()
                state["ok"] = bool(state["status"].get("connected"))

                try:
                    quote_response = client.get(f"{base}/quote/{symbol}")
                    quote_response.raise_for_status()
                    quote = quote_response.json()
                    quote_dt = _dt(quote.get("time"))
                    quote["time_iso"] = quote_dt.isoformat() if quote_dt else None
                    state["quote"] = quote
                except Exception as exc:  # quote can legitimately be unavailable while closed
                    state["quote_error"] = type(exc).__name__

                try:
                    candle_response = client.get(f"{base}/candles/{symbol}/M15?count=3")
                    candle_response.raise_for_status()
                    payload = candle_response.json()
                    candles = payload.get("candles") or []
                    if candles:
                        last_dt = _dt(candles[-1].get("t"))
                        payload["last_time_iso"] = last_dt.isoformat() if last_dt else None
                    state["candles"] = payload
                except Exception as exc:
                    state["candles_error"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - local service health, safely summarized
            state["error"] = type(exc).__name__
            state["message"] = str(exc)[:200]
        state["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return state

    @staticmethod
    def _enrich_open_trades(trades: list[dict[str, Any]], quote: dict[str, Any] | None,
                            now: datetime) -> None:
        """Add indicative mark-to-market fields. Never confuse them with settled PnL.

        The live quote is a current midpoint and the timeout is a wall-clock M15 estimate; both
        are labeled explicitly because only the reconciler can produce an authoritative outcome.
        """
        bid = float((quote or {}).get("bid") or 0)
        ask = float((quote or {}).get("ask") or 0)
        midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else None
        for trade in trades:
            opened = _dt(trade.get("opened_at"))
            trade["age_seconds"] = _age_seconds(opened, now)
            timeout_bars = trade.get("timeout_bars")
            if opened and timeout_bars:
                trade["timeout_at_estimate"] = opened + timedelta(minutes=15 * int(timeout_bars))
                trade["timeout_estimate_basis"] = "wall_clock_m15_approximation"
            if midpoint is None:
                continue
            entry = float(trade["entry_price"])
            stop = float(trade["sl_price"])
            target = float(trade["tp_price"])
            risk = abs(entry - stop)
            sign = 1.0 if trade.get("side") == "buy" else -1.0
            trade["current_mid"] = midpoint
            trade["mark_quote_time"] = (quote or {}).get("time_iso")
            trade["unrealized_pct_gross"] = sign * (midpoint - entry) / entry * 100
            trade["unrealized_r_gross"] = sign * (midpoint - entry) / risk if risk else None
            trade["distance_to_sl_pct"] = sign * (midpoint - stop) / midpoint * 100
            trade["distance_to_tp_pct"] = sign * (target - midpoint) / midpoint * 100
            trade["mark_is_indicative"] = True

    def _alerts(self, *, now: datetime, symbol: str, git: dict[str, Any], db: dict[str, Any],
                hands: dict[str, Any], market_open: bool | None) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []

        def add(severity: str, code: str, message: str) -> None:
            alerts.append({"severity": severity, "code": code, "message": message})

        if git.get("dirty"):
            add("warning", "REPO_DIRTY",
                f"Codul are {git.get('changed_files')} fișiere necomise; rezultatul nu este reproductibil.")
        if not db["health"].get("ok"):
            add("critical", "DATABASE_DOWN", f"PostgreSQL indisponibil: {db['health'].get('error','eroare')}")
        elif not db["health"].get("schema_current"):
            add("critical", "DB_SCHEMA_MISMATCH",
                f"Schema DB este {db['health'].get('schema_version')}, codul cere "
                f"{self.expected_schema_version}; rulează migrările înainte de servicii.")
        if not hands.get("ok"):
            add("critical", "XTB_DISCONNECTED", "trading_hands nu raportează o sesiune XTB conectată.")
        status = hands.get("status") or {}
        if status and status.get("environment") != "demo":
            add("critical", "NOT_DEMO", "Mediul brokerului nu este DEMO. Oprește serviciul.")
        if status.get("trading_enabled") is True:
            add("critical", "REAL_ORDERS_ENABLED",
                "trading_hands are TRADING_ENABLED=true; dashboardul nu mai poate garanta shadow-only.")
        elif status and "trading_enabled" not in status:
            add("warning", "TRADING_FLAG_UNKNOWN",
                "Binarul trading_hands este vechi: /status nu expune trading_enabled; repornește-l.")

        latest_market = (db.get("latest") or {}).get("market") or {}
        broker_candles = (hands.get("candles") or {}).get("candles") or []
        broker_last = broker_candles[-1].get("t") if broker_candles else None
        brain_last = latest_market.get("bar_close")
        if broker_last and brain_last:
            broker_dt, brain_dt = _dt(broker_last), _dt(brain_last)
            if broker_dt and brain_dt and (broker_dt - brain_dt).total_seconds() > 1800:
                add("warning", "BRAIN_BEHIND_FEED",
                    f"Ultimul snapshot {symbol} este cu peste 30 minute în urma feedului XTB.")
        elif not latest_market:
            add("warning", "NO_MARKET_SNAPSHOT", f"Nu există niciun snapshot pentru {symbol}.")

        if market_open and broker_last:
            age = _age_seconds(broker_last, now)
            if age is not None and age > self.settings.eligibility_max_feed_lag_seconds:
                add("critical", "FEED_STALE",
                    f"Piața este deschisă, dar ultima bară are lag de {round(age / 60)} minute.")

        open_trades = db.get("open_trades") or []
        for trade in open_trades:
            age = _age_seconds(trade.get("opened_at"), now)
            if age is not None and age > 48 * 3600:
                add("warning", "STALE_OPEN_SHADOW",
                    f"Trade-ul shadow #{trade.get('id')} este open de peste 48h; verifică reconcilierea.")
                break
        overdue = next((trade for trade in open_trades
                        if (_dt(trade.get("timeout_at_estimate")) or now) < now), None)
        if overdue:
            add("warning", "SHADOW_TIMEOUT_OVERDUE",
                f"Trade-ul shadow #{overdue.get('id')} a depășit timeout-ul estimat, dar este "
                "încă open; rulează reconcilierea run-ului său.")

        summary = db.get("summary") or {}
        if summary.get("test_snapshots", 0):
            add("warning", "TEST_DATA_IN_DB",
                f"Baza conține {summary['test_snapshots']} snapshoturi TST_; separă baza de test.")
        if summary.get("claude_decisions", 0) > 0 and summary.get("llm_calls", 0) == 0:
            add("warning", "LLM_AUDIT_GAP",
                "Există decizii Claude, dar niciun apel în llm_calls pentru simbolul selectat.")
        for reservation in db.get("reservations") or []:
            lease = _dt(reservation.get("lease_expires_at"))
            if reservation.get("status") == "in_progress" and lease and lease < now:
                add("warning", "EXPIRED_RESERVATION",
                    f"Rezervare expirată în run {reservation.get('run_id')}; poate indica un crash.")
                break

        services = db.get("services") or []
        by_name = {row.get("service_name"): row for row in services}
        collector = by_name.get("collector_scheduler")
        shadow = by_name.get("shadow_online")
        if not collector:
            add("warning", "COLLECTOR_NOT_RUNNING",
                "Nu există heartbeat pentru schedulerul de colectare; snapshoturile nu se actualizează automat.")
        if not shadow:
            add("info", "SHADOW_NOT_RUNNING",
                "Shadow Online nu rulează; dashboardul arată doar date deja persistate.")
        for service in services:
            name = service.get("service_name")
            seen_age = _age_seconds(service.get("last_seen_at"), now)
            status_value = service.get("status")
            if status_value == "error":
                add("critical", "SERVICE_ERROR", f"Serviciul {name} raportează eroare.")
            elif status_value == "degraded":
                add("warning", "SERVICE_DEGRADED", f"Serviciul {name} este degradat.")
            elif status_value == "stopped" and not str(name).endswith("_once"):
                add("warning", "SERVICE_STOPPED", f"Serviciul {name} este oprit.")
            if status_value not in ("stopped",) and seen_age is not None and seen_age > 20 * 60:
                add("critical", "SERVICE_HEARTBEAT_STALE",
                    f"Heartbeat-ul {name} lipsește de {round(seen_age / 60)} minute.")
        for operation in db.get("pipeline_runs") or []:
            if operation.get("status") == "running":
                age = _age_seconds(operation.get("started_at"), now)
                if age is not None and age > 20 * 60:
                    add("warning", "PIPELINE_RUN_STUCK",
                        f"Execuția #{operation.get('id')} ({operation.get('run_kind')}) "
                        f"rulează de {round(age / 60)} minute.")
                    break
        if summary.get("metric_scope") == "verified_runs_only" and not summary.get("trades_total"):
            add("info", "NO_VERIFIED_TRACK_RECORD",
                "Nu există încă trade-uri într-un run cu manifest verificat; metricile sunt intenționat goale.")
        if summary.get("selected_run_validity") == "unverified":
            add("warning", "UNVERIFIED_RUN_SELECTED",
                "Run-ul selectat este legacy/test/neclasificat; metricile lui nu sunt dovadă de edge executabil.")

        order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(alerts, key=lambda a: (order.get(a["severity"], 9), a["code"]))
