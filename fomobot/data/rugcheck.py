"""RugCheck token reports (mint/freeze authority, holders, LP lock, known risks)."""

from __future__ import annotations

import logging
import time

import httpx2 as httpx

log = logging.getLogger(__name__)
BASE = "https://api.rugcheck.xyz/v1"


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
        try:
            r = self.http.get(f"/tokens/{mint}/report")
            if r.status_code == 200:
                data = r.json()
            else:
                log.info("RugCheck %s -> HTTP %s", mint, r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("RugCheck failed for %s: %s", mint, e)
        self._cache[mint] = (time.time(), data)
        return data


def parse_report(report: dict) -> dict:
    """Normalize the fields we care about; missing data stays None."""
    holders = report.get("topHolders") or []
    top_pct = None
    if holders:
        # Exclude AMM/LP accounts when RugCheck marks them; otherwise use the max.
        pcts = [float(h.get("pct") or 0) for h in holders if not h.get("isAmm") and not h.get("owner_is_amm")]
        top_pct = max(pcts) if pcts else None
    lp_locked = None
    markets = report.get("markets") or []
    locks = [float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in markets if m.get("lp")]
    if locks:
        lp_locked = max(locks)
    risks = report.get("risks") or []
    return {
        "mint_authority": bool(report.get("mintAuthority")) if "mintAuthority" in report else None,
        "freeze_authority": bool(report.get("freezeAuthority")) if "freezeAuthority" in report else None,
        "top_holder_pct": top_pct,
        "lp_locked_pct": lp_locked,
        "danger_risks": [r.get("name", "?") for r in risks if r.get("level") == "danger"],
        "warn_risks": [r.get("name", "?") for r in risks if r.get("level") == "warn"],
        "rugged": bool(report.get("rugged")),
        "score_normalised": report.get("score_normalised"),
    }
