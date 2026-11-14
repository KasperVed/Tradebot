"""DexScreener market data (free, no API key). https://docs.dexscreener.com/api/reference"""

from __future__ import annotations

import logging
import time

import httpx2 as httpx

from ..models import WSOL_MINT, TokenSnapshot

log = logging.getLogger(__name__)
BASE = "https://api.dexscreener.com"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def pair_to_snapshot(pair: dict) -> TokenSnapshot | None:
    """Convert a DexScreener pair to a snapshot. Only SOL-quoted Solana pairs are used,
    so prices are directly in SOL."""
    if pair.get("chainId") != "solana":
        return None
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    if quote.get("address") != WSOL_MINT or not base.get("address"):
        return None
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    info = pair.get("info") or {}
    price_sol = _f(pair.get("priceNative"))
    if price_sol <= 0:
        return None
    return TokenSnapshot(
        mint=base["address"],
        symbol=base.get("symbol") or "?",
        name=base.get("name") or "?",
        pair_address=pair.get("pairAddress") or "",
        dex_id=pair.get("dexId") or "",
        price_sol=price_sol,
        price_usd=_f(pair.get("priceUsd")),
        liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
        market_cap_usd=_f(pair.get("marketCap") or pair.get("fdv")),
        fdv_usd=_f(pair.get("fdv")),
        volume_m5=_f(vol.get("m5")),
        volume_h1=_f(vol.get("h1")),
        volume_h6=_f(vol.get("h6")),
        volume_h24=_f(vol.get("h24")),
        buys_m5=_i((txns.get("m5") or {}).get("buys")),
        sells_m5=_i((txns.get("m5") or {}).get("sells")),
        buys_h1=_i((txns.get("h1") or {}).get("buys")),
        sells_h1=_i((txns.get("h1") or {}).get("sells")),
        change_m5=_f(chg.get("m5")),
        change_h1=_f(chg.get("h1")),
        change_h6=_f(chg.get("h6")),
        change_h24=_f(chg.get("h24")),
        pair_created_ms=_i(pair.get("pairCreatedAt")),
        has_socials=bool(info.get("socials")),
        has_website=bool(info.get("websites")),
        boosted=bool((pair.get("boosts") or {}).get("active")),
        fetched_at=time.time(),
    )


def best_snapshots(pairs: list[dict]) -> dict[str, TokenSnapshot]:
    """Pick the most liquid SOL pair per token."""
    best: dict[str, TokenSnapshot] = {}
    for pair in pairs:
        snap = pair_to_snapshot(pair)
        if snap and (snap.mint not in best or snap.liquidity_usd > best[snap.mint].liquidity_usd):
            best[snap.mint] = snap
    return best


class DexScreener:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(base_url=BASE, timeout=15, headers={"Accept": "application/json"})

    def _get(self, path: str):
        for attempt in range(3):
            try:
                r = self.http.get(path)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("DexScreener %s failed (%s)", path, e)
                time.sleep(1 + attempt)
        return None

    def latest_profile_mints(self) -> list[str]:
        data = self._get("/token-profiles/latest/v1") or []
        return [d["tokenAddress"] for d in data if d.get("chainId") == "solana" and d.get("tokenAddress")]

    def boosted_mints(self) -> list[str]:
        data = self._get("/token-boosts/latest/v1") or []
        return [d["tokenAddress"] for d in data if d.get("chainId") == "solana" and d.get("tokenAddress")]

    def search_pairs(self, query: str) -> list[dict]:
        data = self._get(f"/latest/dex/search?q={query}") or {}
        return data.get("pairs") or []

    def pairs_for_tokens(self, mints: list[str]) -> list[dict]:
        pairs: list[dict] = []
        for i in range(0, len(mints), 30):  # endpoint accepts up to 30 addresses
            chunk = ",".join(mints[i : i + 30])
            data = self._get(f"/tokens/v1/solana/{chunk}")
            if isinstance(data, list):
                pairs.extend(data)
        return pairs

    def snapshots(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        return best_snapshots(self.pairs_for_tokens(list(dict.fromkeys(mints)))) if mints else {}
