"""Which take-profit ladder earns most? Selling in steps vs. the current single take-profit.

    python research/exit_ladders.py --data <dir with tokens.parquet, ohlcv.parquet>

Same trades as the bot would pick (model top 10% + bot filters), real 15m candles,
2.5% cost per side, 3% extra slippage on stops, stop assumed first within a candle.
Ladders are chosen on the train weeks and reported on the unseen test week."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("fc", HERE / "filter_check.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)
tm, ds = fc.tm, fc.tm.ds
FEE, STOP_SLIP = ds.FEE, ds.STOP_SLIP

# (stop_loss, [(gain, fraction_of_original), ...], trailing, max_bars_15m)
LADDERS = {
    "nu: 50% ved +100%, trail 35%": (0.25, [(1.0, 0.5)], 0.35, 16),
    "3 trin: 25% ved +50/+100/+200": (0.25, [(0.5, 0.25), (1.0, 0.25), (2.0, 0.25)], 0.35, 16),
    "4 trin: 20% ved +30/+60/+100/+200": (0.25, [(0.3, 0.2), (0.6, 0.2), (1.0, 0.2), (2.0, 0.2)], 0.35, 16),
    "tidlig: 33% ved +40/+80": (0.25, [(0.4, 0.33), (0.8, 0.33)], 0.30, 16),
    "sen: 25% ved +100/+300, lang trail": (0.25, [(1.0, 0.25), (3.0, 0.25)], 0.45, 32),
    "hurtig: 50% ved +50, trail 25%": (0.25, [(0.5, 0.5)], 0.25, 16),
    "2 trin: 40% ved +60, 30% ved +150": (0.25, [(0.6, 0.4), (1.5, 0.3)], 0.35, 16),
}


def simulate_ladder(bars, sl, ladder, trail, max_bars) -> float:
    entry = bars[0, 0] * (1 + FEE)
    remaining, proceeds, steps, peak = 1.0, 0.0, 0, entry
    for op, hi, lo, cl in bars[:max_bars]:
        hit_any = steps > 0
        stop = entry * (1 - sl)
        if hit_any:
            stop = max(stop, entry, peak * (1 - trail))  # never turn a winner into a loser
        if lo <= stop:
            return proceeds + remaining * min(stop, op) * (1 - STOP_SLIP) / entry * (1 - FEE) - 1
        while steps < len(ladder) and hi >= entry * (1 + ladder[steps][0]):
            gain, frac = ladder[steps]
            frac = min(frac, remaining)
            proceeds += frac * (1 + gain) * (1 - FEE)
            remaining -= frac
            steps += 1
        peak = max(peak, hi)
        if remaining <= 1e-9:
            return proceeds - 1
    last = bars[min(len(bars), max_bars) - 1, 3]
    return proceeds + remaining * last / entry * (1 - FEE) - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    args = ap.parse_args()
    d = tm.build(args.data)
    t, o = ds.load(args.data)
    o = o.sort_values(["token_address", "ts"])
    bars = {k: g[["open", "high", "low", "close", "ts"]].to_numpy() for k, g in o.groupby("token_address")}
    split = d.captured_at.quantile(0.67)
    tr, te = d[d.captured_at < split], d[d.captured_at >= split]
    m = tm.model().fit(tr[tm.FEATURES], tr.win)
    thr = np.quantile(m.predict_proba(tr[tm.FEATURES])[:, 1], 0.9)
    kw = fc.SETS["bot default"]
    picks = {name: fc.apply(df[m.predict_proba(df[tm.FEATURES])[:, 1] >= thr], **kw) for name, df in (("train", tr), ("test", te))}

    def run(df, spec_):
        out = []
        for _, r in df.iterrows():
            b = bars[r.token_address]
            b = b[b[:, 4] >= r.captured_at.timestamp()]
            if len(b):
                out.append(simulate_ladder(b[:, :4], *spec_))
        return np.array(out)

    print(f"{'stige':<38} {'train snit':>10} {'test snit':>10} {'test win':>9} {'test PF':>8}")
    for name, spec_ in LADDERS.items():
        a, b = run(picks["train"], spec_), run(picks["test"], spec_)
        s = ds.summarize(b)
        print(f"{name:<38} {a.mean():>+10.1%} {b.mean():>+10.1%} {s['win']:>9.0%} {s['pf'] or 0:>8.2f}  (n={s['n']})")


if __name__ == "__main__":
    main()
