"""Do the safety filters help or hurt? Tested on the model's out-of-sample picks.

    python research/filter_check.py --data <dir with tokens.parquet, ohlcv.parquet>
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location("tm", Path(__file__).with_name("train_model.py"))
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

SETS = {
    "none": {},
    "bot default": dict(top10=0.3, dev=0.05, holders=50, liq=10000, mcap=15000, age=(120, 1800)),
    "bot default without top10": dict(dev=0.05, holders=50, liq=10000, mcap=15000, age=(120, 1800)),
    "bot default + LP locked >= 80%": dict(top10=0.3, dev=0.05, holders=50, liq=10000, mcap=15000, lp=0.8,
                                           age=(120, 1800)),
}


def apply(sel, top10=1.0, dev=1.0, holders=0, liq=0, mcap=0, lp=0, age=(0, 1e9)):
    k = sel[~(sel.top10_holder_pct > top10).fillna(False)]
    k = k[~(k.dev_remaining_pct > dev).fillna(False)]
    k = k[~(k.holder_count < holders).fillna(False)]
    k = k[(k.liquidity_usd >= liq) & (k.market_cap_usd >= mcap)]
    k = k[~(k.lp_burned_pct < lp).fillna(False)]
    return k[k.time_since_launch_sec.between(*age)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()
    d = tm.build(args.data)
    split = d.captured_at.quantile(0.67)
    tr, te = d[d.captured_at < split], d[d.captured_at >= split]
    m = tm.model().fit(tr[tm.FEATURES], tr.win)
    thr = np.quantile(m.predict_proba(tr[tm.FEATURES])[:, 1], 0.9)
    picks = te[m.predict_proba(te[tm.FEATURES])[:, 1] >= thr]
    for name, kw in SETS.items():
        print(f"{name:<32} {tm.ds.summarize(apply(picks, **kw).ret.to_numpy())}")


if __name__ == "__main__":
    main()
