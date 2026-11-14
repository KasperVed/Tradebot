"""Build a historical dataset of SOL-quoted memecoin pools with minute candles.

The universe mixes new pools (most of which die - this keeps the sample honest) with
trending and top-volume pools. Candles are cached as JSON so reruns cost nothing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..data.geckoterminal import GeckoTerminal

log = logging.getLogger(__name__)


def collect(out_dir: str, new_pages: int = 10, trending_pages: int = 5, top_pages: int = 3,
            candle_pages: int = 3, max_pools: int = 400, gt: GeckoTerminal | None = None) -> Path:
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

    todo = [p for p in index.values() if not (out / "candles" / f"{p['pool']}.json").exists()][:max_pools]
    for i, p in enumerate(todo, 1):
        rows = gt.ohlcv(p["pool"], pages=candle_pages, stop_before=p.get("created_ts"))
        (out / "candles" / f"{p['pool']}.json").write_text(json.dumps(rows))
        if i % 10 == 0:
            log.info("candles %d/%d", i, len(todo))
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
