"""Research on the public ian05012/solana-memecoin-dataset (June 2026, ~31k tokens).

    python research/memecoin_dataset.py --data <dir with tokens.parquet, ohlcv.parquet>

Questions:
1. Which signals at detection time predict a 2x within 24h, out of sample (time split)?
2. Using only features the live bot can see (DexScreener + RugCheck), is there an edge?
3. What do the bot's exit rules return on real post-detection candles, for all tokens vs.
   model-selected tokens? Fills: next 15m candle open, stop assumed before take-profit,
   2.5% cost per side, extra 3% slippage on stops.
"""

from __future__ import annotations

import argparse
import itertools
import json

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import roc_auc_score

# Features the live bot can compute from DexScreener + RugCheck at decision time.
LIVE = [
    "time_since_launch_sec", "market_cap_usd", "liquidity_usd", "volume_h24", "number_markets",
    "holder_count", "top10_holder_pct", "dev_remaining_pct", "mint_disabled", "freeze_disabled",
    "lp_burned_pct", "price_change_1h", "price_change_6h", "has_twitter", "has_website", "has_telegram",
    "is_boosted", "boost_total", "trade_buy_sell_ratio", "insider_holding_ratio", "liq_mcap", "vol_mcap",
]
LABELS = {"max_return_1h", "max_return_6h", "max_return_24h", "max_return_3d", "reaches_2x", "staged_exit_return",
          "runner_peak_x", "scalp_exit_return", "mdd_from_peak", "time_to_peak_sec", "close_return_1h"}
ID = {"sample_id", "token_address", "symbol", "captured_at", "pool_address", "price_at_capture"}

FEE = 0.025  # per side, fee + slippage
STOP_SLIP = 0.03


def load(d: str):
    t = pd.read_parquet(f"{d}/tokens.parquet")
    o = pd.read_parquet(f"{d}/ohlcv.parquet")
    t = t.sort_values("captured_at").groupby("token_address").head(1).copy()  # first sighting only
    for c in ("mint_disabled", "freeze_disabled"):
        t[c] = t[c].map({True: 1, False: 0, "true": 1, "false": 0, "True": 1, "False": 0}).astype(float)
    t["y"] = t["reaches_2x"].map({True: 1, False: 0})
    t["liq_mcap"] = t.liquidity_usd / t.market_cap_usd.replace(0, np.nan)
    t["vol_mcap"] = t.volume_h24 / t.market_cap_usd.replace(0, np.nan)
    for c in t.columns:
        if c not in ID and c not in LABELS and t[c].dtype == object:
            t[c] = pd.to_numeric(t[c], errors="coerce")
    o = o[o.high <= o.close * 10]  # README: drop glitch candles
    return t, o


def simulate(bars: np.ndarray, sl, tp, tp_frac, trail, max_bars) -> float:
    """bars: [open, high, low, close] rows after detection. Entry at first open."""
    entry = bars[0, 0] * (1 + FEE)
    remaining, proceeds, tp_hit, peak = 1.0, 0.0, False, entry
    sl_px, tp_px = entry * (1 - sl), entry * (1 + tp)
    for j, (op, hi, lo, cl) in enumerate(bars[: max_bars]):
        stop = max(sl_px, entry, peak * (1 - trail)) if tp_hit else sl_px
        if lo <= stop:
            proceeds += remaining * min(stop, op) * (1 - STOP_SLIP) / entry * (1 - FEE)
            return proceeds - 1
        if not tp_hit and hi >= tp_px:
            proceeds += tp_frac * tp_px / entry * (1 - FEE)
            remaining -= tp_frac
            tp_hit, peak = True, max(hi, tp_px)
            if remaining <= 1e-9:
                return proceeds - 1
        elif tp_hit:
            peak = max(peak, hi)
    last = bars[min(len(bars), max_bars) - 1, 3]
    return proceeds + remaining * last / entry * (1 - FEE) - 1


def summarize(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"n": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"n": int(len(r)), "win": round(float((r > 0).mean()), 3), "mean": round(float(r.mean()), 4),
            "median": round(float(np.median(r)), 4),
            "pf": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="research/results.json")
    args = ap.parse_args()
    t, o = load(args.data)
    out: dict = {}

    split = t.captured_at.quantile(0.67)
    lab = t[t.y.notna()]
    tr, te = lab[lab.captured_at < split], lab[lab.captured_at >= split]
    out["data"] = {"tokens": int(len(t)), "labeled": int(len(lab)), "train": int(len(tr)), "test": int(len(te)),
                   "split_at": str(split), "base_2x_rate_test": round(float(te.y.mean()), 4)}
    print(json.dumps(out["data"], indent=1))

    ALL = [c for c in t.columns if c not in LABELS and c not in ID and c != "y" and pd.api.types.is_numeric_dtype(t[c])]
    models = {}
    for name, feats in (("live_features", LIVE), ("all_features", ALL)):
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                           min_samples_leaf=80, l2_regularization=1.0, random_state=0)
        m.fit(tr[feats], tr.y)
        p = m.predict_proba(te[feats])[:, 1]
        thr = np.quantile(m.predict_proba(tr[feats])[:, 1], 0.9)  # top decile threshold from TRAIN
        sel = p >= thr
        res = {"auc": round(float(roc_auc_score(te.y, p)), 4), "top_decile_threshold": float(thr),
               "test_selected": int(sel.sum()), "2x_rate_selected": round(float(te.y[sel].mean()), 4),
               "2x_rate_rest": round(float(te.y[~sel].mean()), 4)}
        pi = permutation_importance(m, te[feats], te.y, scoring="roc_auc", n_repeats=3, random_state=0)
        imp = sorted(zip(feats, pi.importances_mean), key=lambda x: -x[1])[:15]
        res["top_features"] = [(f, round(float(v), 4)) for f, v in imp]
        out[name] = res
        models[name] = (m, feats, thr)
        print(f"\n{name}: AUC {res['auc']}  2x-rate top decile {res['2x_rate_selected']:.1%} vs rest {res['2x_rate_rest']:.1%}")
        for f, v in res["top_features"][:10]:
            print(f"   {f:<24} {v:+.4f}")

    # Univariate: 2x rate by quintile for interpretable live features (all labeled data).
    uni = {}
    for f in ["top10_holder_pct", "dev_remaining_pct", "holder_count", "liquidity_usd", "market_cap_usd",
              "time_since_launch_sec", "price_change_1h", "trade_buy_sell_ratio", "liq_mcap", "vol_mcap",
              "insider_holding_ratio", "lp_burned_pct", "boost_total"]:
        s = lab[[f, "y"]].dropna()
        if s[f].nunique() < 5:
            continue
        q = pd.qcut(s[f], 5, duplicates="drop")
        g = s.groupby(q, observed=True).y.agg(["mean", "count"])
        uni[f] = [{"bin": str(i), "rate_2x": round(float(r["mean"]), 4), "n": int(r["count"])} for i, r in g.iterrows()]
    out["univariate_2x_rate"] = uni
    print("\n2x rate by quintile:")
    for f, rows in uni.items():
        print(f"  {f:<22} " + "  ".join(f"{r['rate_2x']:.1%}" for r in rows))

    # Strategy simulation on post-detection candles.
    o = o.sort_values(["token_address", "ts"])
    bars_by = {k: g[["open", "high", "low", "close", "ts"]].to_numpy() for k, g in o.groupby("token_address")}
    sim = t[t.token_address.isin(bars_by)].copy()
    cap_ts = sim.captured_at.astype("int64") // 10**9
    seqs = {}
    for addr, ts0 in zip(sim.token_address, cap_ts):
        b = bars_by[addr]
        b = b[b[:, 4] >= ts0]  # candles starting after detection (no look-ahead)
        if len(b) >= 1:
            seqs[addr] = b[:, :4]
    sim = sim[sim.token_address.isin(seqs)]
    sim_tr, sim_te = sim[sim.captured_at < split], sim[sim.captured_at >= split]
    print(f"\nSimulation: {len(sim)} tokens with post-detection candles ({len(sim_tr)} train / {len(sim_te)} test)")

    grid = list(itertools.product([0.15, 0.25, 0.35, 0.5], [0.3, 0.6, 1.0, 2.0], [0.5, 1.0], [0.2, 0.35], [4, 16, 96]))

    def run(df, params):
        return np.array([simulate(seqs[a], *params) for a in df.token_address])

    def best_params(df):
        best, bp = -9, None
        for g in grid:
            r = run(df, g)
            score = np.mean(np.sort(r)[: int(len(r) * 0.99)])  # trimmed mean, robust to one moonshot
            if score > best:
                best, bp = score, g
        return bp

    sims = {}
    for label, mask_fn in (
        ("all_tokens", lambda df: np.ones(len(df), bool)),
        ("live_model_top_decile", lambda df: models["live_features"][0].predict_proba(df[LIVE])[:, 1] >= models["live_features"][2]),
        ("all_model_top_decile", lambda df: models["all_features"][0].predict_proba(df[models["all_features"][1]])[:, 1] >= models["all_features"][2]),
    ):
        dtr, dte = sim_tr[mask_fn(sim_tr)], sim_te[mask_fn(sim_te)]
        if len(dtr) < 30 or len(dte) < 30:
            continue
        bp = best_params(dtr)
        sims[label] = {"params(sl,tp,tp_frac,trail,max_bars15m)": bp,
                       "train": summarize(run(dtr, bp)), "test": summarize(run(dte, bp))}
        print(f"  {label:<24} params {bp}  train {sims[label]['train']}  test {sims[label]['test']}")
    out["strategy_sim"] = sims

    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"\nSaved {args.out}")



def tradeable_edge(data_dir: str, exit_params=(0.25, 1.0, 0.5, 0.35, 16)) -> dict:
    """Train directly on the simulated trade outcome (profit > 0) instead of the 2x label,
    without the inconsistent volume_h24 field. Time split as above."""
    t, o = load(data_dir)
    o = o.sort_values(["token_address", "ts"])
    bars_by = {k: g[["open", "high", "low", "close", "ts"]].to_numpy() for k, g in o.groupby("token_address")}
    rows = []
    for _, r in t[t.token_address.isin(bars_by)].iterrows():
        b = bars_by[r.token_address]
        b = b[b[:, 4] >= r.captured_at.timestamp()]
        if len(b):
            rows.append((r.name, simulate(b[:, :4], *exit_params)))
    idx, ret = zip(*rows)
    d = t.loc[list(idx)].copy()
    d["ret"] = ret
    d["win"] = (d.ret > 0).astype(int)
    split = d.captured_at.quantile(0.67)
    tr, te = d[d.captured_at < split], d[d.captured_at >= split]
    res = {"exit_params": exit_params, "all_test": summarize(te.ret.to_numpy())}
    for name, feats in (
        ("live_no_volume", [f for f in LIVE if f not in ("volume_h24", "vol_mcap")]),
        ("all_no_volume", [c for c in t.columns if c not in LABELS and c not in ID and c not in ("y", "volume_h24", "vol_mcap")
                           and pd.api.types.is_numeric_dtype(t[c])]),
    ):
        m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, min_samples_leaf=60,
                                           l2_regularization=1.0, random_state=0)
        m.fit(tr[feats], tr.win)
        p = m.predict_proba(te[feats])[:, 1]
        ptr = m.predict_proba(tr[feats])[:, 1]
        out = {"auc": round(float(roc_auc_score(te.win, p)), 4)}
        for q in (0.8, 0.9, 0.95):
            thr = np.quantile(ptr, q)
            out[f"top_{int(round((1 - q) * 100))}pct"] = summarize(te.ret[p >= thr].to_numpy())
        pi = permutation_importance(m, te[feats], te.win, scoring="roc_auc", n_repeats=3, random_state=0)
        out["top_features"] = [(f, round(float(v), 4)) for f, v in sorted(zip(feats, pi.importances_mean), key=lambda x: -x[1])[:10]]
        res[name] = out
    return res


if __name__ == "__main__":
    main()
