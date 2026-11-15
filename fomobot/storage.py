"""SQLite persistence: positions, trades, decisions, price history and learned lessons."""

from __future__ import annotations

import json
import sqlite3
import time

from .models import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL, symbol TEXT, entry_price REAL, token_amount REAL, token_raw INTEGER,
    cost_sol REAL, opened_at REAL, stop_loss_pct REAL, take_profit_pct REAL, peak_price REAL,
    tp_hit INTEGER DEFAULT 0, realized_sol REAL DEFAULT 0, thesis TEXT, status TEXT DEFAULT 'open',
    closed_at REAL, pnl_sol REAL, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, position_id INTEGER, mint TEXT, symbol TEXT,
    side TEXT, sol_amount REAL, token_amount REAL, price_sol REAL, reason TEXT, signature TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mint TEXT, symbol TEXT, score REAL,
    action TEXT, conviction REAL, source TEXT, thesis TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS prices (mint TEXT, ts REAL, price_sol REAL, liquidity_usd REAL);
CREATE INDEX IF NOT EXISTS prices_mint_ts ON prices(mint, ts);
CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, position_id INTEGER, lesson TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, equity_sol REAL);
"""

POS_COLS = [
    "id", "mint", "symbol", "entry_price", "token_amount", "token_raw", "cost_sol", "opened_at",
    "stop_loss_pct", "take_profit_pct", "peak_price", "tp_hit", "realized_sol", "thesis", "status",
]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- state ---------------------------------------------------------------
    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    # --- prices --------------------------------------------------------------
    def add_price(self, mint: str, price: float, liquidity: float, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO prices VALUES (?, ?, ?, ?)", (mint, ts or time.time(), price, liquidity))

    def price_history(self, mint: str, since_s: float = 6 * 3600, limit: int = 200) -> list[float]:
        rows = self.db.execute(
            "SELECT price_sol FROM prices WHERE mint=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (mint, time.time() - since_s, limit),
        ).fetchall()
        return [r["price_sol"] for r in reversed(rows)]

    def entry_liquidity(self, mint: str, since: float) -> float | None:
        row = self.db.execute(
            "SELECT liquidity_usd FROM prices WHERE mint=? AND ts>=? ORDER BY ts ASC LIMIT 1", (mint, since)
        ).fetchone()
        return row["liquidity_usd"] if row else None

    def add_equity(self, equity_sol: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?, ?)", (time.time(), equity_sol))
        self.db.commit()

    def prune_prices(self, older_than_s: float = 48 * 3600) -> None:
        self.db.execute("DELETE FROM prices WHERE ts < ?", (time.time() - older_than_s,))
        self.db.commit()

    # --- positions -----------------------------------------------------------
    def _row_to_pos(self, r) -> Position:
        return Position(**{c: r[c] for c in POS_COLS} | {"tp_hit": bool(r["tp_hit"])})

    def open_positions(self) -> list[Position]:
        return [self._row_to_pos(r) for r in self.db.execute("SELECT * FROM positions WHERE status='open'")]

    def insert_position(self, p: Position) -> int:
        cols = [c for c in POS_COLS if c != "id"]
        cur = self.db.execute(
            f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [getattr(p, c) for c in cols],
        )
        self.db.commit()
        p.id = cur.lastrowid
        return p.id

    def update_position(self, p: Position) -> None:
        cols = [c for c in POS_COLS if c != "id"]
        self.db.execute(
            f"UPDATE positions SET {','.join(f'{c}=?' for c in cols)} WHERE id=?",
            [getattr(p, c) for c in cols] + [p.id],
        )
        self.db.commit()

    def close_position(self, p: Position, pnl_sol: float, reason: str) -> None:
        p.status = "closed"
        self.update_position(p)
        self.db.execute(
            "UPDATE positions SET closed_at=?, pnl_sol=?, exit_reason=? WHERE id=?", (time.time(), pnl_sol, reason, p.id)
        )
        self.db.commit()

    def closed_positions(self, since: float = 0, limit: int = 500) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()

    def last_closed_at(self, mint: str) -> float | None:
        row = self.db.execute("SELECT MAX(closed_at) AS t FROM positions WHERE mint=?", (mint,)).fetchone()
        return row["t"] if row else None

    # --- trades / decisions / lessons ---------------------------------------
    def record_trade(self, **kw) -> None:
        kw.setdefault("ts", time.time())
        cols = list(kw)
        self.db.execute(f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(kw.values()))
        self.db.commit()

    def trades_for(self, position_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE position_id=? ORDER BY ts", (position_id,)).fetchall()

    def record_decision(self, mint, symbol, score, action, conviction, source, thesis, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO decisions (ts, mint, symbol, score, action, conviction, source, thesis, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), mint, symbol, score, action, conviction, source, thesis, json.dumps(payload, default=str)),
        )
        self.db.commit()

    def recent_decision(self, mint: str, within_s: float) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM decisions WHERE mint=? AND ts>=? ORDER BY ts DESC LIMIT 1", (mint, time.time() - within_s)
        ).fetchone()

    def add_lesson(self, position_id: int, lesson: str) -> None:
        self.db.execute("INSERT INTO lessons (ts, position_id, lesson) VALUES (?,?,?)", (time.time(), position_id, lesson))
        self.db.commit()

    def lessons(self, limit: int) -> list[str]:
        rows = self.db.execute("SELECT lesson FROM lessons ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [r["lesson"] for r in rows]

    def commit(self) -> None:
        self.db.commit()
