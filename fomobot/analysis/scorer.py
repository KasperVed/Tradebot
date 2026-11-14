"""Combine indicators and safety into a 0-100 opportunity score with readable reasons."""

from __future__ import annotations

from ..models import Indicators, SafetyReport, Signal, TokenSnapshot

WEIGHTS = {
    "momentum": 0.25,
    "volume": 0.20,
    "buy_pressure": 0.20,
    "liquidity": 0.10,
    "trend": 0.10,
    "safety": 0.15,
}


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def score(snap: TokenSnapshot, safety: SafetyReport, ind: Indicators) -> Signal:
    reasons: list[str] = []
    parts: dict[str, float] = {}

    # Momentum: reward healthy uptrend, penalize parabolic blow-offs (late FOMO entries).
    m = ind.momentum
    if snap.change_m5 > 35 or snap.change_h1 > 250:
        parts["momentum"] = 30
        reasons.append("parabolic move - high risk of buying the top")
    else:
        parts["momentum"] = _clamp(50 + m * 0.8)
        if m > 30:
            reasons.append(f"strong momentum ({snap.change_m5:+.1f}% 5m, {snap.change_h1:+.1f}% 1h)")
        elif m < -20:
            reasons.append("falling price")

    va = ind.volume_accel
    parts["volume"] = _clamp(25 * va + 10) if va < 3 else _clamp(85 + (va - 3) * 3)
    if va > 1.5:
        reasons.append(f"volume accelerating ({va:.1f}x normal rate)")
    elif va < 0.5:
        reasons.append("volume fading")

    bp = ind.buy_pressure
    parts["buy_pressure"] = _clamp((bp - 0.35) / 0.35 * 100)
    if bp > 0.6:
        reasons.append(f"buyers dominate ({bp:.0%} of txns)")
    elif bp < 0.45:
        reasons.append(f"sellers dominate ({1 - bp:.0%} of txns)")

    lr = ind.liquidity_mcap_ratio
    parts["liquidity"] = _clamp(lr * 500)  # 20% liq/mcap -> 100

    if ind.ema_trend is not None:
        parts["trend"] = _clamp(50 + ind.ema_trend * 5)
        if ind.rsi is not None and ind.rsi > 85:
            parts["trend"] -= 25
            reasons.append(f"overbought (RSI {ind.rsi:.0f})")
    else:
        parts["trend"] = 50  # neutral until we have our own history

    parts["safety"] = safety.score

    total = sum(parts[k] * w for k, w in WEIGHTS.items())
    if not safety.passed:
        total = min(total, 20)
        reasons.extend(f"BLOCKED: {r}" for r in safety.hard_fail_reasons)
    return Signal(snapshot=snap, safety=safety, indicators=ind, score=round(total, 1), breakdown=parts, reasons=reasons)
