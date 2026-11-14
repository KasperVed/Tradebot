"""GeckoTerminal public API (free, no key, ~30 calls/min): pool discovery and OHLCV history.
https://www.geckoterminal.com/dex-api"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx2 as httpx

from ..models import WSOL_MINT

log = logging.getLogger(__name__)
BASE = "https://api.geckoterminal.com/api/v2"


class GeckoTerminal:
    def __init__(self, client: httpx.Client | None = None, min_interval: float = 2.2):
        self.http = client or httpx.Client(
            base_url=BASE, timeout=20, headers={"Accept": "application/json;version=20230302"}
        )
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, path: str, params: dict | None = None, retries: int = 6):
        for attempt in range(retries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.http.get(path, params=params)
            except httpx.HTTPError as e:
                log.warning("GeckoTerminal %s: %s", path, e)
                time.sleep(3)
                continue
            if r.status_code == 429:
                time.sleep(10 + 10 * attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                log.warning("GeckoTerminal %s -> %s", path, r.status_code)
                time.sleep(3)
                continue
            return r.json()
        return None

    @staticmethod
    def _pool(p: dict) -> dict | None:
        a, rel = p["attributes"], p.get("relationships") or {}
        base = (rel.get("base_token") or {}).get("data", {}).get("id", "")
        quote = (rel.get("quote_token") or {}).get("data", {}).get("id", "")
        if not quote.endswith(WSOL_MINT):
            return None
        created = a.get("pool_created_at")
        return {
            "pool": a["address"],
            "mint": base.split("_", 1)[-1],
            "name": a.get("name"),
            "dex": (rel.get("dex") or {}).get("data", {}).get("id"),
            "created_ts": datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None,
            "reserve_usd": float(a.get("reserve_in_usd") or 0),
            "market_cap_usd": float(a.get("market_cap_usd") or a.get("fdv_usd") or 0),
        }

    def pools(self, kind: str = "trending_pools", pages: int = 1) -> list[dict]:
        """kind: trending_pools | new_pools | pools (top by volume)."""
        out = []
        for page in range(1, pages + 1):
            data = self._get(f"/networks/solana/{kind}", {"page": page})
            if not data:
                break
            out += [x for x in (self._pool(p) for p in data.get("data", [])) if x]
        return out

    def ohlcv(self, pool: str, timeframe: str = "minute", aggregate: int = 1, pages: int = 1,
              stop_before: float | None = None) -> list[list[float]]:
        """Candles [ts, open, high, low, close, volume] oldest-first, priced in SOL."""
        candles: list[list[float]] = []
        before = None
        for _ in range(pages):
            params = {"aggregate": aggregate, "limit": 1000, "currency": "token"}
            if before:
                params["before_timestamp"] = int(before)
            data = self._get(f"/networks/solana/pools/{pool}/ohlcv/{timeframe}", params)
            rows = ((data or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
            if not rows:
                break
            candles += rows
            before = min(r[0] for r in rows)
            if len(rows) < 1000 or (stop_before and before <= stop_before):
                break
        uniq = {int(r[0]): r for r in candles}
        return [uniq[k] for k in sorted(uniq)]
