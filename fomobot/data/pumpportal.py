"""PumpPortal websocket feed for real-time pump.fun graduations (migrations)."""

import json
import threading
import time
from typing import Optional


def parse_message(raw: str) -> Optional[tuple]:
    """Parse a PumpPortal websocket message; return (mint, timestamp) or None."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("txType") != "migrate":
        return None
    mint = msg.get("mint")
    if not mint:
        return None
    return (mint, time.time())


class MigrationFeed:
    """Subscribes to PumpPortal and buffers graduated token mints."""

    WS_URL = "wss://pumpportal.fun/api/data"

    def __init__(self):
        self._queue: list[tuple[str, float]] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MigrationFeed":
        self._thread = threading.Thread(target=self._run, daemon=True, name="pumpportal")
        self._thread.start()
        return self

    def drain(self) -> list[tuple[str, float]]:
        with self._lock:
            items, self._queue = self._queue, []
        return items

    def _run(self):
        try:
            import websocket  # optional dependency
        except ImportError:
            return
        while True:
            try:
                ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=lambda ws: ws.send(json.dumps({"method": "subscribeMigration"})),
                    on_message=self._on_message,
                )
                ws.run_forever(ping_interval=30)
            except Exception:
                pass
            time.sleep(5)

    def _on_message(self, ws, raw: str):
        result = parse_message(raw)
        if result:
            with self._lock:
                self._queue.append(result)
