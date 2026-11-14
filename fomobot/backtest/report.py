"""python -m fomobot backtest: optimize on half the tokens, report on the unseen half."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from . import dataset
from .engine import (Costs, EntryParams, ExitParams, features, optimize, random_baseline, run, split, stats,
                     to_series)

log = logging.getLogger(__name__)


def fmt(st: dict) -> str:
    if not st.get("n"):
        return "no trades"
    return (f"n={st['n']:>4} tokens={st['tokens']:>3} win={st['win_rate']:.0%} mean={st['mean']:+.2%} "
            f"median={st['median']:+.2%} per-token={st['token_mean']:+.2%} trim={st['mean_trim2']:+.2%} "
            f"PF={st['profit_factor']:.2f} hold={st['avg_hold']:.0f}m")


def backtest(data_dir: str, out_file: str | None = None, min_trades: int = 60) -> dict:
    raw = dataset.load(data_dir)
    series = [s for s in (to_series(m) for m in raw) if s is not None]
    feats = [features(s) for s in series]
    tr, te = split(series)
    print(f"Dataset: {len(series)} pools ({len(tr)} train / {len(te)} test), "
          f"{sum(len(s.c) for s in series):,} minute candles")

    sub = lambda idx: ([series[i] for i in idx], [feats[i] for i in idx])
    ep0, xp0 = EntryParams(), ExitParams()
    print("\nDefault parameters")
    for name, idx in (("train", tr), ("test", te)):
        print(f"  {name:<5} {fmt(stats(run(*sub(idx), ep0, xp0)))}")

    print("\nOptimizing on train set...")
    ep, xp = optimize(series, feats, tr, min_trades=min_trades, log=lambda m: print("  " + m))
    res = {}
    print("\nOptimized parameters")
    for name, idx in (("train", tr), ("test", te)):
        st = stats(run(*sub(idx), ep, xp))
        res[name] = st
        print(f"  {name:<5} {fmt(st)}")
    base = stats(random_baseline(*sub(te), ep, xp))
    res["test_random_entry"] = base
    print(f"  random entries, same exits (test): {fmt(base)}")

    print("\nTest set by universe source (survivorship check)")
    test_trades = run(*sub(te), ep, xp)
    for src in sorted({t["source"] for t in test_trades}):
        print(f"  {src:<15} {fmt(stats([t for t in test_trades if t['source'] == src]))}")

    print("\nCost sensitivity (test): ", end="")
    for mult in (0.5, 1.0, 2.0):
        k = Costs(fee=0.01 * mult, slippage=0.015 * mult, stop_slippage=0.03 * mult)
        print(f"x{mult}: {stats(run(*sub(te), ep, xp, k)).get('mean', 0):+.2%}  ", end="")
    print()

    result = {"entry": asdict(ep), "exit": asdict(xp), "results": res,
              "pools": len(series), "candles": int(sum(len(s.c) for s in series))}
    print("\nBest entry:", json.dumps(asdict(ep)))
    print("Best exit: ", json.dumps(asdict(xp)))
    if out_file:
        with open(out_file, "w") as f:
            json.dump(result, f, indent=1)
    return result
