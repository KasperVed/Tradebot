"""Self-learning: the bot records every token its model scores, labels the outcome later
with real candles, and `fomobot retrain` fits a new model on that data.

A new model is only switched on if it beats the current one on the most recent,
unseen observations (time split). Outcomes are simulated with the bot's own exit rules
on GeckoTerminal minute candles: entry at the next candle's open, stop assumed before
take-profit inside a candle, fees and slippage on every fill."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from .config import Config, StrategyConfig
from .storage import Store

log = logging.getLogger(__name__)
FEE = 0.025
STOP_SLIP = 0.03
LABEL_AFTER_S = 4.5 * 3600
LOCAL_MODEL = "data/memecoin_model_local.joblib"


def simulate(candles: list[list[float]], strat: StrategyConfig) -> float | None:
    """candles: [ts, o, h, l, c, v] oldest first, starting after the decision."""
    if len(candles) < 2:
        return None
    bars = np.array(candles, dtype=float)[1:, 1:5]  # next candle's open is the entry
    entry = bars[0, 0] * (1 + FEE)
    sl, trail = strat.default_stop_loss_pct / 100, strat.trailing_stop_pct / 100
    ladder = [(g / 100, f) for g, f in strat.take_profit_ladder] or [(strat.default_take_profit_pct / 100,
                                                                       strat.tp_sell_fraction)]
    remaining, proceeds, steps, peak = 1.0, 0.0, 0, entry
    for op, hi, lo, cl in bars[: int(strat.max_hold_minutes)]:
        stop = max(entry * (1 - sl), entry, peak * (1 - trail)) if steps else entry * (1 - sl)
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
    return proceeds + remaining * bars[min(len(bars), int(strat.max_hold_minutes)) - 1, 3] / entry * (1 - FEE) - 1


def label_pending(store: Store, cfg: Config, gt=None, limit: int = 500) -> int:
    from .data.geckoterminal import GeckoTerminal

    gt = gt or GeckoTerminal()
    rows = store.db.execute(
        "SELECT rowid, ts, pair FROM observations WHERE label IS NULL AND ts < ? ORDER BY ts LIMIT ?",
        (time.time() - LABEL_AFTER_S, limit)).fetchall()
    done = 0
    for r in rows:
        candles = [c for c in gt.ohlcv(r["pair"], pages=1) if c[0] >= r["ts"]]
        ret = simulate(candles, cfg.strategy)
        # A pool with no candles after the decision has usually died: count as a full stop-out.
        if ret is None:
            ret = -cfg.strategy.default_stop_loss_pct / 100 - FEE * 2 - STOP_SLIP
        store.db.execute("UPDATE observations SET label=?, labeled_at=? WHERE rowid=?", (ret, time.time(), r["rowid"]))
        store.commit()
        done += 1
        if done % 20 == 0:
            log.info("labeled %d/%d", done, len(rows))
    return done


def retrain(store: Store, cfg: Config, min_samples: int = 300, out_path: str = LOCAL_MODEL) -> dict:
    import joblib
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingClassifier

    from .intelligence.ml import TradeModel

    rows = store.db.execute("SELECT ts, prob, features, label FROM observations WHERE label IS NOT NULL ORDER BY ts").fetchall()
    if len(rows) < min_samples:
        return {"status": "not enough data", "labeled": len(rows), "needed": min_samples}
    current = TradeModel.load(cfg.ml.model_path)
    feats = current.features if current else json.loads(rows[0]["features"]).keys()
    X = pd.DataFrame([json.loads(r["features"]) for r in rows])[list(feats)].astype(float)
    y_ret = np.array([r["label"] for r in rows])
    old_p = np.array([r["prob"] for r in rows])
    cut = int(len(rows) * 0.7)
    model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, min_samples_leaf=30,
                                           l2_regularization=1.0, random_state=0)
    model.fit(X[:cut], (y_ret[:cut] > 0).astype(int))
    new_p = model.predict_proba(X[cut:])[:, 1]
    hold = y_ret[cut:]
    q = cfg.ml.min_percentile

    def top(p_hold, p_train):
        sel = p_hold >= np.quantile(p_train, q)
        return float(hold[sel].mean()) if sel.sum() >= 10 else None, int(sel.sum())

    new_mean, new_n = top(new_p, model.predict_proba(X[:cut])[:, 1])
    old_mean, old_n = top(old_p[cut:], old_p[:cut])
    result = {"labeled": len(rows), "holdout": len(hold), "holdout_all_mean": float(hold.mean()),
              "new_top_mean": new_mean, "new_top_n": new_n, "current_top_mean": old_mean, "current_top_n": old_n}
    better = new_mean is not None and (old_mean is None or new_mean > old_mean)
    if better:
        final = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, min_samples_leaf=30,
                                               l2_regularization=1.0, random_state=0).fit(X, (y_ret > 0).astype(int))
        p_all = final.predict_proba(X)[:, 1]
        meta = {"features": list(feats), "percentiles": {str(k): float(np.quantile(p_all, k)) for k in (0.5, 0.8, 0.9, 0.95, 0.99)},
                "trained_on": f"{len(rows)} of the bot's own observations", "holdout": result}
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": final, "meta": meta}, out_path)
        result["status"] = f"new model saved to {out_path}"
    else:
        result["status"] = "kept current model (new one was not better on unseen data)"
    return result


def retrain_command(cfg: Config, store: Store) -> None:
    print("Labeling observations older than 4.5 h with real candles (GeckoTerminal, ~2 s each)...")
    n = label_pending(store, cfg)
    print(f"Labeled {n} new observations.")
    res = retrain(store, cfg)
    print(json.dumps(res, indent=1))
    if res.get("status", "").startswith("new model"):
        print(f"\nSlå den nye model til i config.yaml:\n  ml:\n    model_path: {LOCAL_MODEL}")
