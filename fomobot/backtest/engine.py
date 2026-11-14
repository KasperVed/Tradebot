"""Candle-level backtester for the bot's price/volume entry signal and its exit rules.

Conservative assumptions:
- entry fills at the NEXT candle's open plus slippage (no look-ahead);
- when stop-loss and take-profit are both touched in one candle, the stop is assumed first;
- every fill pays fee + slippage; stops fill at the stop price minus extra slippage
  (memecoins gap through stops);
- one position per token at a time, with a cooldown after each exit.
What it cannot see historically: holder data, RugCheck, buy/sell counts. Those filters
only remove trades, so they are tested live/paper, not here.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class EntryParams:
    min_ret5: float = 0.03  # 5-minute return window
    max_ret5: float = 0.25  # above this = parabolic, skip
    min_ret60: float = 0.0
    max_ret60: float = 1.5
    min_vol_accel: float = 2.0  # last-5-min volume vs prior hour's 5-min average
    min_green: float = 0.6  # share of green candles in the last 10 (buy-pressure proxy)
    min_age_min: float = 10
    max_dd60: float = 0.35  # skip if price is >35% below its 60-min high (falling knife)


@dataclass(frozen=True)
class ExitParams:
    stop_loss: float = 0.20
    take_profit: float = 0.60
    tp_fraction: float = 0.5
    trailing: float = 0.25
    max_hold_min: int = 240
    breakeven_after_tp: bool = True


@dataclass(frozen=True)
class Costs:
    fee: float = 0.01
    slippage: float = 0.015
    stop_slippage: float = 0.03
    cooldown_min: int = 120


@dataclass
class Series:
    pool: str
    source: str
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    created_ts: float | None


def to_series(meta: dict) -> Series | None:
    """Forward-fill candles onto a full minute grid (GeckoTerminal skips minutes without trades)."""
    rows = meta["candles"]
    if len(rows) < 2:
        return None
    arr = np.array(rows, dtype=float)
    t0, t1 = int(arr[0, 0]), int(arr[-1, 0])
    n = (t1 - t0) // 60 + 1
    if n > 20000:
        return None
    idx = ((arr[:, 0] - t0) // 60).astype(int)
    c = np.full(n, np.nan)
    c[idx] = arr[:, 4]
    filled = np.isnan(c)
    # forward fill closes
    last = np.where(~filled, np.arange(n), 0)
    np.maximum.accumulate(last, out=last)
    c = c[last]
    o, h, l, v = c.copy(), c.copy(), c.copy(), np.zeros(n)
    o[idx], h[idx], l[idx], v[idx] = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 5]
    if np.any(c <= 0) or not np.all(np.isfinite(c)):
        return None
    return Series(meta["pool"], meta.get("source", "?"), t0 + np.arange(n) * 60, o, h, l, c, v, meta.get("created_ts"))


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    cs = np.concatenate([[0.0], np.cumsum(x)])
    out = np.full(len(x), np.nan)
    out[w - 1 :] = cs[w:] - cs[:-w]
    return out


@dataclass
class Features:
    ret5: np.ndarray
    ret60: np.ndarray
    vol_accel: np.ndarray
    green: np.ndarray
    dd60: np.ndarray
    age_min: np.ndarray


def features(s: Series) -> Features:
    n = len(s.c)
    ret5 = np.full(n, np.nan)
    ret60 = np.full(n, np.nan)
    ret5[5:] = s.c[5:] / s.c[:-5] - 1
    ret60[60:] = s.c[60:] / s.c[:-60] - 1
    v5 = _rolling_sum(s.v, 5)
    v65 = _rolling_sum(s.v, 65)
    prior = (v65 - v5) / 12
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_accel = np.where(prior > 0, v5 / prior, 0.0)
    green = _rolling_sum((s.c > s.o).astype(float), 10) / 10
    hi60 = _rolling_max(s.h, 60)
    dd60 = 1 - s.c / hi60
    created = s.created_ts or float(s.ts[0])
    age_min = (s.ts - created) / 60
    return Features(ret5, ret60, vol_accel, green, dd60, age_min)


def _rolling_max(x: np.ndarray, w: int) -> np.ndarray:
    from numpy.lib.stride_tricks import sliding_window_view

    pad = np.concatenate([np.full(w - 1, x[0]), x])
    return sliding_window_view(pad, w).max(axis=1)


def entry_mask(f: Features, p: EntryParams) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        m = (
            (f.ret5 >= p.min_ret5) & (f.ret5 <= p.max_ret5)
            & (f.ret60 >= p.min_ret60) & (f.ret60 <= p.max_ret60)
            & (f.vol_accel >= p.min_vol_accel) & (f.green >= p.min_green)
            & (f.age_min >= p.min_age_min) & (f.dd60 <= p.max_dd60)
        )
    return np.nan_to_num(m, nan=False).astype(bool)


def simulate_trade(s: Series, i: int, x: ExitParams, k: Costs) -> tuple[float, int]:
    """Enter at open of candle i+1. Returns (net return on stake, exit index)."""
    e = i + 1
    if e >= len(s.c):
        return math.nan, e
    entry = s.o[e] * (1 + k.slippage)
    sl_px = entry * (1 - x.stop_loss)
    tp_px = entry * (1 + x.take_profit)
    end = min(len(s.c) - 1, e + x.max_hold_min)
    remaining, proceeds = 1.0, 0.0
    tp_hit, peak = False, entry
    sell_cost = k.fee + k.slippage
    buy_mult = 1 - k.fee  # entry fee comes out of the stake
    for j in range(e, end + 1):
        lo, hi = s.l[j], s.h[j]
        stop = max(sl_px, entry) if (tp_hit and x.breakeven_after_tp) else sl_px
        if tp_hit:
            stop = max(stop, peak * (1 - x.trailing))
        if lo <= stop:  # stop first (conservative)
            fill = min(stop, s.o[j]) * (1 - k.stop_slippage)
            proceeds += remaining * fill / entry * (1 - k.fee)
            return proceeds * buy_mult - 1, j
        if not tp_hit and hi >= tp_px:
            proceeds += x.tp_fraction * tp_px / entry * (1 - sell_cost)
            remaining -= x.tp_fraction
            tp_hit, peak = True, max(hi, tp_px)
            if remaining <= 1e-9:
                return proceeds * buy_mult - 1, j
            continue
        if tp_hit:
            peak = max(peak, hi)
    proceeds += remaining * s.c[end] / entry * (1 - sell_cost)
    return proceeds * buy_mult - 1, end


def run(series: list[Series], feats: list[Features], ep: EntryParams, xp: ExitParams, k: Costs = Costs()) -> list[dict]:
    trades = []
    for s, f in zip(series, feats):
        sig = np.flatnonzero(entry_mask(f, ep))
        next_ok = 0
        for i in sig:
            if i < next_ok:
                continue
            r, j = simulate_trade(s, int(i), xp, k)
            if math.isnan(r):
                continue
            trades.append({"pool": s.pool, "source": s.source, "i": int(i), "ret": r, "hold": j - i})
            next_ok = j + k.cooldown_min
    return trades


def random_baseline(series, feats, ep: EntryParams, xp: ExitParams, k: Costs = Costs(), seed: int = 7) -> list[dict]:
    """Same number of entries per token as the signal, at random eligible times.
    If the signal can't beat this, the entry logic has no edge."""
    rng = np.random.default_rng(seed)
    trades = []
    for s, f in zip(series, feats):
        n_sig = int(entry_mask(f, ep).sum())
        if not n_sig:
            continue
        eligible = np.flatnonzero(np.nan_to_num(f.age_min >= ep.min_age_min) & np.isfinite(f.ret60))
        if not len(eligible):
            continue
        n_trades = len(run([s], [f], ep, xp, k))
        for i in rng.choice(eligible, size=min(n_trades, len(eligible)), replace=False):
            r, j = simulate_trade(s, int(i), xp, k)
            if not math.isnan(r):
                trades.append({"pool": s.pool, "source": s.source, "i": int(i), "ret": r, "hold": j - i})
    return trades


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    r = np.array([t["ret"] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    # Per-token average first so one lucky token can't dominate the result.
    by_pool: dict[str, list[float]] = {}
    for t in trades:
        by_pool.setdefault(t["pool"], []).append(t["ret"])
    pool_means = np.array([np.mean(v) for v in by_pool.values()])
    trimmed = np.sort(r)[: max(1, int(len(r) * 0.98))]
    return {
        "n": int(len(r)),
        "tokens": len(by_pool),
        "win_rate": float((r > 0).mean()),
        "mean": float(r.mean()),
        "median": float(np.median(r)),
        "mean_trim2": float(trimmed.mean()),
        "token_mean": float(pool_means.mean()),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_hold": float(np.mean([t["hold"] for t in trades])),
    }


def split(series: list[Series]) -> tuple[list[int], list[int]]:
    """Deterministic 50/50 split by pool address (train / test)."""
    train, test = [], []
    for i, s in enumerate(series):
        (train if int(hashlib.sha1(s.pool.encode()).hexdigest(), 16) % 2 == 0 else test).append(i)
    return train, test


def objective(st: dict, min_trades: int) -> float:
    """Robust score: per-token mean and trimmed mean must both be good; small samples penalized."""
    if st.get("n", 0) < min_trades:
        return -1e9
    return min(st["token_mean"], st["mean_trim2"]) * math.sqrt(min(st["n"], 400) / 400)


ENTRY_GRID = {
    "min_ret5": [0.02, 0.04, 0.08],
    "max_ret5": [0.15, 0.30],
    "min_ret60": [-0.1, 0.0, 0.15],
    "max_ret60": [1.0, 3.0],
    "min_vol_accel": [1.5, 2.5, 4.0],
    "min_green": [0.5, 0.7],
    "max_dd60": [0.2, 0.4],
}
EXIT_GRID = {
    "stop_loss": [0.10, 0.15, 0.20, 0.30],
    "take_profit": [0.25, 0.40, 0.60, 1.00],
    "tp_fraction": [0.5, 1.0],
    "trailing": [0.15, 0.25, 0.35],
    "max_hold_min": [60, 120, 240],
}


def grid(cls, space: dict, base):
    keys = list(space)
    for combo in itertools.product(*(space[k] for k in keys)):
        yield cls(**(asdict(base) | dict(zip(keys, combo))))


def optimize(series, feats, idx, min_trades=60, rounds=2, log=print):
    """Coordinate descent: tune exits with the default entry, then entries, then exits again."""
    S = [series[i] for i in idx]
    F = [feats[i] for i in idx]
    ep, xp = EntryParams(), ExitParams()
    best = objective(stats(run(S, F, ep, xp)), min_trades)
    for rnd in range(rounds):
        for which in ("exit", "entry"):
            cands = grid(ExitParams, EXIT_GRID, xp) if which == "exit" else grid(EntryParams, ENTRY_GRID, ep)
            for c in cands:
                e2, x2 = (ep, c) if which == "exit" else (c, xp)
                sc = objective(stats(run(S, F, e2, x2)), min_trades)
                if sc > best:
                    best, ep, xp = sc, e2, x2
            log(f"round {rnd + 1} {which}: objective {best:.4f}")
    return ep, xp
