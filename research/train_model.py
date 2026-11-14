"""Train the bot's trade-selection model on the public ian05012/solana-memecoin-dataset.

    python research/train_model.py --data <dir with tokens.parquet, ohlcv.parquet>

Target: did a trade with the bot's default exits (SL 25%, sell half at +100%, trail 35%,
max 4h) end in profit, simulated on real post-detection 15m candles with costs.
Only features the live bot can reconstruct from DexScreener + RugCheck are used.
The time-split test metrics are stored next to the model."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("ds", HERE / "memecoin_dataset.py")
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

FEATURES = [
    "time_since_launch_sec", "market_cap_usd", "liquidity_usd", "holder_count", "top10_holder_pct",
    "dev_remaining_pct", "mint_disabled", "freeze_disabled", "lp_burned_pct", "price_change_1h",
    "price_change_6h", "has_twitter", "has_website", "has_telegram", "is_boosted", "trade_buy_sell_ratio",
    "liq_mcap",
]
EXITS = (0.25, 1.0, 0.5, 0.35, 16)


def build(data_dir: str):
    t, o = ds.load(data_dir)
    o = o.sort_values(["token_address", "ts"])
    bars = {k: g[["open", "high", "low", "close", "ts"]].to_numpy() for k, g in o.groupby("token_address")}
    rets, keep = [], []
    for i, r in t[t.token_address.isin(bars)].iterrows():
        b = bars[r.token_address]
        b = b[b[:, 4] >= r.captured_at.timestamp()]
        if len(b):
            keep.append(i)
            rets.append(ds.simulate(b[:, :4], *EXITS))
    d = t.loc[keep].copy()
    d["ret"] = rets
    d["win"] = (d.ret > 0).astype(int)
    return d.sort_values("captured_at")


def model():
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, min_samples_leaf=60,
                                          l2_regularization=1.0, random_state=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="fomobot/intelligence/memecoin_model.joblib")
    args = ap.parse_args()
    d = build(args.data)
    split = d.captured_at.quantile(0.67)
    tr, te = d[d.captured_at < split], d[d.captured_at >= split]

    m = model().fit(tr[FEATURES], tr.win)
    p_te = m.predict_proba(te[FEATURES])[:, 1]
    p_tr = m.predict_proba(tr[FEATURES])[:, 1]
    metrics = {"auc": round(float(roc_auc_score(te.win, p_te)), 4),
               "test_all": ds.summarize(te.ret.to_numpy())}
    for q in (0.8, 0.9, 0.95):
        thr = float(np.quantile(p_tr, q))
        metrics[f"test_top_{int(round((1 - q) * 100))}pct"] = ds.summarize(te.ret[p_te >= thr].to_numpy()) | {"threshold": thr}
    print(json.dumps(metrics, indent=1))

    final = model().fit(d[FEATURES], d.win)  # deploy on all data
    p_all = final.predict_proba(d[FEATURES])[:, 1]
    meta = {
        "features": FEATURES,
        "exits": dict(zip(["stop_loss", "take_profit", "tp_fraction", "trailing", "max_bars_15m"], EXITS)),
        "time_split_test": metrics,
        "percentiles": {str(q): float(np.quantile(p_all, q)) for q in (0.5, 0.8, 0.9, 0.95, 0.99)},
        "base_win_rate": float(d.win.mean()),
        "trained_on": f"{len(d)} tokens, {d.captured_at.min():%Y-%m-%d} to {d.captured_at.max():%Y-%m-%d}",
        "source": "https://github.com/ian05012/solana-memecoin-dataset",
    }
    joblib.dump({"model": final, "meta": meta}, args.out)
    Path(args.out).with_suffix(".json").write_text(json.dumps(meta, indent=1, default=str))
    print("saved", args.out)


if __name__ == "__main__":
    main()
