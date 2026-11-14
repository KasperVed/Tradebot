"""Telegram notifications so you can follow the bot from your phone.

Setup (2 minutes, on the phone): message @BotFather -> /newbot -> copy the token into
TELEGRAM_BOT_TOKEN. Send your new bot any message, then open
https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id":...} into TELEGRAM_CHAT_ID.
Without both variables, notifications are silently off."""

from __future__ import annotations

import logging
import os
import threading

import httpx2 as httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, mode: str = "paper",
                 client: httpx.Client | None = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self.prefix = "🔴 LIVE" if mode == "live" else "📝 PAPER"
        self.http = client or httpx.Client(timeout=10)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, blocking: bool = False) -> None:
        if not self.enabled:
            return
        payload = {"chat_id": self.chat_id, "text": f"{self.prefix} | {text}", "disable_web_page_preview": True}

        def _post():
            try:
                r = self.http.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload)
                if r.status_code != 200:
                    log.warning("Telegram send failed: %s %s", r.status_code, r.text[:120])
            except httpx.HTTPError as e:
                log.warning("Telegram send failed: %s", e)

        if blocking:
            _post()
        else:  # never slow the trading loop down
            threading.Thread(target=_post, daemon=True).start()

    # --- message helpers -------------------------------------------------
    def bought(self, symbol: str, mint: str, sol: float, tp: float, sl: float, why: str) -> None:
        self.send(f"🟢 KØB {symbol}: {sol:.3f} SOL | TP +{tp:.0f}% / SL -{sl:.0f}%\n{why[:200]}\n"
                  f"https://dexscreener.com/solana/{mint}")

    def sold(self, symbol: str, fraction: float, sol: float, reason: str) -> None:
        self.send(f"🟡 SALG {symbol} {fraction:.0%}: {sol:.3f} SOL ({reason})")

    def closed(self, symbol: str, pnl_sol: float, pnl_pct: float, reason: str) -> None:
        icon = "✅" if pnl_sol > 0 else "❌"
        self.send(f"{icon} LUKKET {symbol}: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%) - {reason}")

    def summary(self, equity: float, start: float, trades: int, wins: int, open_pos: int) -> None:
        wr = f"{wins / trades:.0%}" if trades else "-"
        self.send(f"📊 Status: {equity:.4f} SOL ({(equity / start - 1) * 100:+.1f}% siden start) | "
                  f"{trades} lukkede handler, win rate {wr} | {open_pos} åbne")
