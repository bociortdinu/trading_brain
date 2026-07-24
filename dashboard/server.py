"""Dependency-light local HTTP server for the read-only dashboard."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config.settings import load_settings
from dashboard.service import DashboardService

STATIC_DIR = Path(__file__).with_name("static")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TradingBrainDashboard/1.0"

    @property
    def service(self) -> DashboardService:
        return self.server.dashboard_service  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["50"])[0])
            except ValueError:
                return self._json({"error": "limit must be an integer"}, HTTPStatus.BAD_REQUEST)
            try:
                payload = self.service.state(
                    symbol=params.get("symbol", [None])[0],
                    run_id=params.get("run_id", [None])[0],
                    limit=limit,
                )
                return self._json(payload)
            except Exception as exc:  # noqa: BLE001 - keep operator UI alive, no traceback leak
                return self._json(
                    {"error": "dashboard_state_failed", "type": type(exc).__name__},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
        if parsed.path in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html")
        if parsed.path.startswith("/static/"):
            name = parsed.path.removeprefix("/static/")
            # No traversal and no arbitrary filesystem reads.
            if not name or Path(name).name != name:
                return self.send_error(HTTPStatus.NOT_FOUND)
            return self._file(STATIC_DIR / name)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html", head_only=True)
        if parsed.path.startswith("/static/"):
            name = parsed.path.removeprefix("/static/")
            if not name or Path(name).name != name:
                return self.send_error(HTTPStatus.NOT_FOUND)
            return self._file(STATIC_DIR / name, head_only=True)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        self._json({"error": "read_only_dashboard"}, HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body), cache=False)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, *, head_only: bool = False) -> None:
        if not path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._headers(f"{content_type}; charset=utf-8", len(body), cache=True)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _headers(self, content_type: str, length: int, *, cache: bool) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "public, max-age=300" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, fmt: str, *args) -> None:
        # Concise access log; query values contain no secrets, but avoid printing them anyway.
        print(f"[dashboard] {self.address_string()} {self.command} {urlparse(self.path).path} "
              f"{fmt % args}")


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: DashboardService):
        super().__init__(address, DashboardHandler)
        self.dashboard_service = service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trading Brain read-only local dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true", help="open the browser after start")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[dashboard] WARNING: binding beyond loopback exposes operational data on the network")
    server = DashboardHTTPServer((args.host, args.port), DashboardService(load_settings()))
    url = f"http://{args.host}:{args.port}"
    print(f"[dashboard] read-only operator console: {url}")
    print("[dashboard] Ctrl+C to stop; no order or database-write endpoints exist")
    if args.open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[dashboard] stopping")
    finally:
        server.server_close()
    return 0
