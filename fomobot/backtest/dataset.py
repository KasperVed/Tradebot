"""Build a historical dataset of SOL-quoted memecoin pools with minute candles.

The universe mixes new pools (most of which die - this keeps the sample honest) with
trending and top-volume pools. Candles are cached as JSON so reruns cost nothing."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..data.geckoterminal import GeckoTerminal

log = logging.getLogger(__name__)


def collect(out_dir: str, new_pages: int = 10, trending_pages: int = 5, top_pages: int = 3,
            candle_pages: int = 3, max_pools: int = 400, min_age_h: float = 3.0, gt: GeckoTerminal | None = None) -> Path:
    gt = gt or GeckoTerminal()
    out = Path(out_dir)
    (out / "candles").mkdir(parents=True, exist_ok=True)
    index_path = out / "pools.json"
    index: dict[str, dict] = json.loads(index_path.read_text()) if index_path.exists() else {}

    for kind, pages in (("new_pools", new_pages), ("trending_pools", trending_pages), ("pools", top_pages)):
        for p in gt.pools(kind, pages):
            p.setdefault("source", kind)
            index.setdefault(p["pool"], p)
        index_path.write_text(json.dumps(index, indent=1))
        log.info("universe: %d pools after %s", len(index), kind)

    now = time.time()

    def needs_fetch(p: dict) -> bool:
        created = p.get("created_ts") or 0
        if now - created < min_age_h * 3600:
            return False  # too young to have history; picked up by a later `collect` run
        f = out / "candles" / f"{p['pool']}.json"
        if not f.exists():
            return True
        # refetch pools that were still young when we last fetched them
        return p.get("fetched_at", 0) - created < min_age_h * 3600 * 2 and now - p.get("fetched_at", 0) > 3600

    order = {"trending_pools": 0, "pools": 1, "new_pools": 2}
    todo = sorted((p for p in index.values() if needs_fetch(p)), key=lambda p: order.get(p.get("source"), 3))
    todo = todo[:max_pools]
    log.info("fetching candles for %d pools", len(todo))
    for i, p in enumerate(todo, 1):
        rows = gt.ohlcv(p["pool"], pages=candle_pages, stop_before=p.get("created_ts"))
        (out / "candles" / f"{p['pool']}.json").write_text(json.dumps(rows))
        p["fetched_at"] = time.time()
        if i % 10 == 0:
            index_path.write_text(json.dumps(index, indent=1))
            log.info("candles %d/%d", i, len(todo))
    index_path.write_text(json.dumps(index, indent=1))
    return out


def load(out_dir: str, min_candles: int = 120) -> list[dict]:
    out = Path(out_dir)
    index = json.loads((out / "pools.json").read_text())
    data = []
    for pool, meta in index.items():
        f = out / "candles" / f"{pool}.json"
        if not f.exists():
            continue
        rows = json.loads(f.read_text())
        if len(rows) >= min_candles:
            data.append(meta | {"candles": rows})
    return data
