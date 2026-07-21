"""Read-only dashboard: pure serialization/alerts and HTTP boundary tests."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from config.settings import Settings
from dashboard.server import DashboardHTTPServer
from dashboard.service import DashboardService, json_safe

UTC = timezone.utc


def test_json_safe_converts_nested_db_values():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    assert json_safe({"price": Decimal("4016.25"), "at": now, "items": (Decimal("1.5"),)}) == {
        "price": 4016.25, "at": "2026-07-18T10:00:00+00:00", "items": [1.5],
    }


def test_alerts_surface_operational_gaps_without_secrets():
    service = DashboardService(Settings())
    now = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    db = {
        "health": {"ok": True},
        "latest": {"market": {"bar_close": now - timedelta(days=2)}},
        "open_trades": [{"id": 7, "opened_at": now - timedelta(days=3)}],
        "summary": {"test_snapshots": 4, "claude_decisions": 1, "llm_calls": 0},
        "reservations": [{"run_id": "crashed", "status": "in_progress",
                          "lease_expires_at": now - timedelta(minutes=1)}],
    }
    hands = {
        "ok": True, "status": {"environment": "demo"},
        "candles": {"candles": [{"t": int(now.timestamp() * 1000)}]},
    }
    alerts = service._alerts(now=now, symbol="GOLD", git={"dirty": True, "changed_files": 2},
                             db=db, hands=hands, market_open=False)
    codes = {a["code"] for a in alerts}
    assert {"REPO_DIRTY", "BRAIN_BEHIND_FEED", "STALE_OPEN_SHADOW", "TEST_DATA_IN_DB",
            "LLM_AUDIT_GAP", "EXPIRED_RESERVATION"} <= codes
    assert "password" not in json.dumps(alerts).lower()


def test_alerts_expose_order_permission_and_stale_service():
    service = DashboardService(Settings())
    now = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    db = {
        "health": {"ok": True, "schema_current": True},
        "latest": {"market": {}}, "open_trades": [],
        "summary": {"metric_scope": "selected_run", "test_snapshots": 0},
        "reservations": [],
        "services": [{"service_name": "collector_scheduler", "status": "healthy",
                      "last_seen_at": now - timedelta(minutes=25)}],
        "pipeline_runs": [],
    }
    hands = {"ok": True, "status": {"environment": "demo", "trading_enabled": True},
             "candles": {"candles": []}}
    codes = {a["code"] for a in service._alerts(
        now=now, symbol="GOLD", git={"dirty": False}, db=db, hands=hands,
        market_open=False)}
    assert "REAL_ORDERS_ENABLED" in codes
    assert "SERVICE_HEARTBEAT_STALE" in codes


def test_open_shadow_mark_is_indicative_and_directional():
    now = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    trades = [
        {"side": "buy", "entry_price": Decimal("100"), "sl_price": Decimal("98"),
         "tp_price": Decimal("104"), "opened_at": now - timedelta(minutes=30),
         "timeout_bars": 8},
        {"side": "sell", "entry_price": Decimal("100"), "sl_price": Decimal("102"),
         "tp_price": Decimal("96"), "opened_at": now - timedelta(minutes=15),
         "timeout_bars": 8},
    ]
    DashboardService._enrich_open_trades(
        trades, {"bid": 100.9, "ask": 101.1, "time_iso": now.isoformat()}, now)
    assert trades[0]["current_mid"] == 101.0
    assert trades[0]["unrealized_r_gross"] == pytest.approx(0.5)
    assert trades[1]["unrealized_r_gross"] == pytest.approx(-0.5)
    assert trades[0]["timeout_at_estimate"] == now + timedelta(minutes=90)
    assert trades[0]["mark_is_indicative"] is True


def test_status_whitelist_drops_the_broker_account_and_unknown_fields():
    """P0/P1: /status carries the XTB account number. Only known-safe fields may reach the browser,
    and a field added later by trading_hands must be dropped by default, not forwarded blindly."""
    from dashboard.service import whitelist_status

    out = whitelist_status({"connected": True, "environment": "demo", "trading_enabled": False,
                            "account": "12345678", "ticket": "TGT-abc", "future_secret": "x"})
    assert out == {"connected": True, "environment": "demo", "trading_enabled": False}
    assert "account" not in out and "ticket" not in out and "future_secret" not in out
    assert whitelist_status(None) is None


def test_redact_strips_sensitive_values_anywhere_in_the_payload():
    """Defence in depth: nested JSONB (manifests, heartbeat details, pipeline results) and error
    text are pass-through, so a sensitive key must be redacted wherever it appears."""
    from dashboard.service import REDACTED, redact

    payload = {
        "runs": [{"run_id": "r1", "manifest": {"db_dsn": "postgresql://u:pw@h/db", "ok": 1}}],
        "services": [{"details": {"api_key": "sk-live-x", "phase": "deciding"}}],
        "health": {"message": "fine", "authorization": "Bearer zzz"},
        "nested": [[{"password": "hunter2"}]],
        "keep": {"symbol": "GOLD", "count": 3},
    }
    out = redact(payload)
    blob = json.dumps(out)
    for leaked in ("postgresql://", "sk-live-x", "Bearer zzz", "hunter2"):
        assert leaked not in blob, f"{leaked} reached the browser payload"
    assert out["runs"][0]["manifest"]["db_dsn"] == REDACTED
    assert out["services"][0]["details"]["api_key"] == REDACTED
    assert out["nested"][0][0]["password"] == REDACTED
    assert out["keep"] == {"symbol": "GOLD", "count": 3}      # harmless data survives
    assert out["runs"][0]["manifest"]["ok"] == 1


def test_alerts_split_session_from_quote_and_candles_health():
    """P1: a live session does NOT imply a usable feed. A broken quote/OHLCV must alert instead of
    showing green, and market-open with ZERO bars must still raise FEED_STALE (previously silent)."""
    service = DashboardService(Settings())
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    db = {"health": {"ok": True, "schema_current": True}, "latest": {"market": {}},
          "open_trades": [], "summary": {"metric_scope": "selected_run"}, "reservations": [],
          "services": [], "pipeline_runs": []}
    hands = {"ok": True, "session_ok": True, "quote_ok": False, "candles_ok": False,
             "status": {"environment": "demo", "trading_enabled": False},
             "candles": {"candles": []}, "quote_error": "ReadTimeout"}
    codes = {a["code"] for a in service._alerts(
        now=now, symbol="GOLD", git={"dirty": False}, db=db, hands=hands, market_open=True)}
    assert {"QUOTE_UNAVAILABLE", "CANDLES_UNAVAILABLE", "FEED_STALE"} <= codes

    # a fully healthy feed raises none of them
    healthy = {**hands, "quote_ok": True, "candles_ok": True,
               "candles": {"candles": [{"t": int(now.timestamp() * 1000)}]}}
    ok_codes = {a["code"] for a in service._alerts(
        now=now, symbol="GOLD", git={"dirty": False}, db=db, hands=healthy, market_open=True)}
    assert not ({"QUOTE_UNAVAILABLE", "CANDLES_UNAVAILABLE", "FEED_STALE"} & ok_codes)


class _FakeService:
    def state(self, **kwargs):
        return {"ok": True, "selection": kwargs}


def test_http_server_serves_ui_api_and_rejects_writes():
    server = DashboardHTTPServer(("127.0.0.1", 0), _FakeService())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            html = response.read().decode()
            assert response.status == 200 and "Trading Brain" in html
            assert response.headers["X-Frame-Options"] == "DENY"
        with urlopen(Request(f"{base}/static/styles.css", method="HEAD"), timeout=2) as response:
            assert response.status == 200
            assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
        with urlopen(f"{base}/api/state?symbol=gold&limit=7", timeout=2) as response:
            payload = json.load(response)
            assert payload["ok"] is True
            assert payload["selection"]["symbol"] == "gold"
            assert payload["selection"]["limit"] == 7
            assert response.headers["Cache-Control"] == "no-store"
        try:
            urlopen(Request(f"{base}/api/state", method="POST"), timeout=2)
            raise AssertionError("POST must be rejected")
        except HTTPError as exc:
            assert exc.code == 405
            assert json.loads(exc.read())["error"] == "read_only_dashboard"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
