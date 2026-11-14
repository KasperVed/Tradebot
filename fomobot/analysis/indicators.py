"""Technical indicators built from DexScreener aggregates plus the bot's own price history."""

from __future__ import annotations

import math
import statistics

from ..models import Indicators, TokenSnapshot


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(values[-period - 1 : -1], values[-period:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def volatility(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(values[:-1], values[1:]) if a > 0 and b > 0]
    return statistics.pstdev(rets) * 100 if len(rets) >= 2 else 0.0


def compute(snap: TokenSnapshot, price_history: list[float]) -> Indicators:
    # Momentum: weighted blend of multi-timeframe change, squashed to -100..100.
    raw = 0.45 * snap.change_m5 + 0.35 * snap.change_h1 + 0.20 * (snap.change_h6 / 3)
    momentum = 100 * math.tanh(raw / 40)

    # Volume acceleration: 5-minute rate vs average 5-minute rate over the last hour/6h.
    rate_m5 = snap.volume_m5
    rate_h1 = snap.volume_h1 / 12 if snap.volume_h1 else 0
    rate_h6 = snap.volume_h6 / 72 if snap.volume_h6 else 0
    base = max(rate_h1, rate_h6, 1e-9)
    volume_accel = rate_m5 / base if rate_m5 else 0.0

    buys = snap.buys_m5 + snap.buys_h1 * 0.25
    sells = snap.sells_m5 + snap.sells_h1 * 0.25
    buy_pressure = buys / (buys + sells) if buys + sells else 0.5

    liq_ratio = snap.liquidity_usd / snap.market_cap_usd if snap.market_cap_usd else 0.0

    hist = price_history + [snap.price_sol]
    fast, slow = ema(hist, 5), ema(hist, 15)
    ema_trend = (fast / slow - 1) * 100 if fast and slow else None

    return Indicators(
        momentum=momentum,
        volume_accel=volume_accel,
        buy_pressure=buy_pressure,
        liquidity_mcap_ratio=liq_ratio,
        volatility=volatility(hist[-30:]),
        rsi=rsi(hist),
        ema_trend=ema_trend,
        history_points=len(hist),
    )
