import numpy as np
import pytest

from fomobot.backtest.engine import Costs, EntryParams, ExitParams, Series, features, run, simulate_trade, stats, to_series

NO_COST = Costs(fee=0, slippage=0, stop_slippage=0, cooldown_min=0)


def series_from_closes(closes, highs=None, lows=None):
    c = np.array(closes, dtype=float)
    n = len(c)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.array(highs, dtype=float) if highs is not None else np.maximum(o, c)
    l = np.array(lows, dtype=float) if lows is not None else np.minimum(o, c)
    return Series("P", "test", np.arange(n) * 60.0, o, h, l, c, np.ones(n), 0.0)


def test_stop_loss_exact_without_costs():
    s = series_from_closes([1.0, 1.0, 1.0, 0.5, 0.5])
    r, j = simulate_trade(s, 0, ExitParams(stop_loss=0.2, take_profit=1.0), NO_COST)
    assert r == pytest.approx(-0.2)
    assert j == 3


def test_take_profit_then_trailing():
    # entry 1.0; TP at +50% sells half; peak 2.0; trail 25% -> exit 1.5 on the rest
    closes = [1.0, 1.0, 1.6, 2.0, 1.4, 1.4]
    s = series_from_closes(closes)
    r, _ = simulate_trade(s, 0, ExitParams(stop_loss=0.2, take_profit=0.5, tp_fraction=0.5, trailing=0.25), NO_COST)
    assert r == pytest.approx(0.5 * 1.5 + 0.5 * 1.5 - 1)


def test_stop_assumed_before_tp_in_same_candle():
    s = series_from_closes([1.0, 1.0, 1.0, 1.0], highs=[1, 1, 3.0, 1], lows=[1, 1, 0.5, 1])
    r, _ = simulate_trade(s, 0, ExitParams(stop_loss=0.2, take_profit=0.5), NO_COST)
    assert r == pytest.approx(-0.2)


def test_costs_make_flat_trade_negative():
    s = series_from_closes([1.0] * 10)
    r, _ = simulate_trade(s, 0, ExitParams(max_hold_min=5), Costs())
    assert -0.06 < r < -0.03


def test_forward_fill_and_signal():
    rows = [[i * 60, 1 + i * 0.01, 1 + i * 0.01, 1 + i * 0.01, 1 + i * 0.01, 1.0] for i in range(0, 200, 2)]
    s = to_series({"pool": "X", "candles": rows, "created_ts": 0})
    assert len(s.c) == 199 and np.all(s.c > 0)
    f = features(s)
    assert np.isnan(f.ret60[10]) and f.ret60[150] > 0
    trades = run([s], [f], EntryParams(min_ret5=0.0, min_vol_accel=0, min_green=0, min_age_min=0), ExitParams(), NO_COST)
    assert trades and stats(trades)["n"] == len(trades)
