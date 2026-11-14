"""RugCheck token reports (free, no key): authorities, holders, insiders, LP, known risks."""

from __future__ import annotations

import logging
import time

import httpx2 as httpx

log = logging.getLogger(__name__)
BASE = "https://api.rugcheck.xyz/v1"

# Pool types where "LP locked %" is meaningful. Concentrated-liquidity pools (DLMM, CLMM,
# Whirlpool, DAMM v2) have no fungible LP token, so RugCheck reports 0% for them.
LP_TOKEN_MARKETS = {"pump_fun_amm", "raydium", "raydium_cpmm", "meteora", "meteora_damm"}


class RugCheck:
    def __init__(self, client: httpx.Client | None = None, cache_seconds: float = 600):
        self.http = client or httpx.Client(base_url=BASE, timeout=20)
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict | None]] = {}

    def report(self, mint: str) -> dict | None:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        data = None
        for attempt in range(3):
            try:
                r = self.http.get(f"/tokens/{mint}/report")
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    data = r.json()
                else:
                    log.info("RugCheck %s -> HTTP %s", mint, r.status_code)
                break
            except (httpx.HTTPError, ValueError) as e:
                log.warning("RugCheck failed for %s: %s", mint, e)
        self._cache[mint] = (time.time(), data)
        return data


def parse_report(report: dict) -> dict:
    """Normalize the fields we care about; missing data stays None."""
    known = report.get("knownAccounts") or {}

    def is_program_account(h: dict) -> bool:
        for key in (h.get("owner"), h.get("address")):
            if key and (known.get(key) or {}).get("type") in ("AMM", "LOCKER", "CREATOR_LOCKED"):
                return True
        return False

    holders = [h for h in (report.get("topHolders") or []) if not is_program_account(h)]
    pcts = sorted((float(h.get("pct") or 0) for h in holders), reverse=True)
    top_pct = pcts[0] if pcts else None
    top10 = sum(pcts[:10]) if pcts else None

    markets = report.get("markets") or []
    lp_locked = None
    lp_markets = [m for m in markets if m.get("lp") and m.get("marketType") in LP_TOKEN_MARKETS]
    if lp_markets:
        lp_locked = max(float(m["lp"].get("lpLockedPct") or 0) for m in lp_markets)

    token = report.get("token") or {}
    supply = float(token.get("supply") or 0)
    creator_pct = None
    if supply and report.get("creatorBalance") is not None:
        creator_pct = float(report["creatorBalance"]) / supply * 100
    insider_pct = None
    nets = report.get("insiderNetworks") or []
    if supply and nets:
        insider_pct = sum(float(n.get("currentHolding") or 0) for n in nets) / supply * 100

    fee = report.get("transferFee") or {}
    risks = report.get("risks") or []
    launchpad = report.get("launchpad")
    return {
        "mint_authority": bool(report.get("mintAuthority")) if "mintAuthority" in report else None,
        "freeze_authority": bool(report.get("freezeAuthority")) if "freezeAuthority" in report else None,
        "top_holder_pct": top_pct,
        "top10_pct": top10,
        "lp_locked_pct": lp_locked,
        "creator_pct": creator_pct,
        "insider_pct": insider_pct,
        "insiders_detected": int(report.get("graphInsidersDetected") or 0),
        "transfer_fee_pct": float(fee.get("pct") or 0),
        "total_holders": report.get("totalHolders"),
        "launchpad": launchpad.get("name") if isinstance(launchpad, dict) else launchpad,
        "danger_risks": [r.get("name", "?") for r in risks if r.get("level") == "danger"],
        "warn_risks": [r.get("name", "?") for r in risks if r.get("level") == "warn"],
        "rugged": bool(report.get("rugged")),
        "score_normalised": report.get("score_normalised"),
    }
