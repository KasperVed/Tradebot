"""Phone dashboard: `fomobot dashboard` (or `fomobot run --dashboard`).

Serves a read-only page with the four agents, equity chart and live activity from the
bot's SQLite database. Open http://<computer-ip>:8080 on a phone on the same Wi-Fi.
No secrets are exposed: it only reads trades, decisions and agent status."""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("dashboard.html")


def snapshot(db_path: str) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        def state(key, default=None):
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

        eq = [(r["ts"], r["equity_sol"]) for r in db.execute(
            "SELECT ts, equity_sol FROM equity WHERE ts >= ? ORDER BY ts", (time.time() - 7 * 86400,))]
        step = max(1, len(eq) // 300)  # keep the chart light on a phone
        eq = eq[::step] + ([eq[-1]] if eq and eq[-1] not in eq[::step] else [])
        start = state("start_equity") or (eq[0][1] if eq else None)
        trades = [dict(r) for r in db.execute(
            "SELECT ts, symbol, mint, side, sol_amount, reason FROM trades ORDER BY ts DESC LIMIT 40")]
        closed = [dict(r) for r in db.execute(
            "SELECT symbol, pnl_sol, exit_reason, closed_at FROM positions WHERE status='closed' "
            "ORDER BY closed_at DESC LIMIT 200")]
        open_pos = [dict(r) for r in db.execute(
            "SELECT symbol, mint, cost_sol, entry_price, opened_at, tp_hit FROM positions WHERE status='open'")]
        decisions = [dict(r) for r in db.execute(
            "SELECT ts, symbol, action, conviction, source, thesis FROM decisions ORDER BY ts DESC LIMIT 20")]
        wins = sum(1 for c in closed if (c["pnl_sol"] or 0) > 0)
        return {
            "now": time.time(),
            "agents": state("agents", {}),
            "equity": eq,
            "start_equity": start,
            "trades": trades,
            "open_positions": open_pos,
            "decisions": decisions,
            "stats": {"closed": len(closed), "wins": wins, "pnl_sol": sum(c["pnl_sol"] or 0 for c in closed)},
        }
    finally:
        db.close()


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/state"):
                try:
                    self._send(200, json.dumps(snapshot(db_path), default=str).encode(), "application/json")
                except sqlite3.Error as e:
                    self._send(503, json.dumps({"error": str(e)}).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):  # keep the bot's log clean
            pass

    return Handler


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def serve(db_path: str, port: int = 8080, background: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path))
    log.info("Dashboard: http://%s:%d (open on your phone, same Wi-Fi)", local_ip(), port)
    if background:
        threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    else:
        server.serve_forever()
    return server
