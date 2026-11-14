"""The backtested entry gate, applied live.

Maps DexScreener aggregates onto the same features the candle backtester optimizes,
so the live bot only considers setups of the kind that showed an edge historically."""

from __future__ import annotations

from ..config import EntryGateConfig
from ..models import TokenSnapshot


def live_features(snap: TokenSnapshot, history: list[float]) -> dict[str, float | None]:
    prior_rate = (snap.volume_h1 - snap.volume_m5) / 11 if snap.volume_h1 > snap.volume_m5 else 0
    vol_accel = snap.volume_m5 / prior_rate if prior_rate > 0 else 0.0
    trades_m5 = snap.buys_m5 + snap.sells_m5
    buy_share = snap.buys_m5 / trades_m5 if trades_m5 else None
    dd60 = None
    if len(history) >= 10:  # bot polls every ~30s, so 10+ points ~ 5+ minutes of own history
        hi = max(history[-120:] + [snap.price_sol])
        dd60 = 1 - snap.price_sol / hi
    return {
        "ret5": snap.change_m5 / 100,
        "ret60": snap.change_h1 / 100,
        "vol_accel": vol_accel,
        "buy_share": buy_share,
        "dd60": dd60,
        "age_min": snap.age_minutes,
    }


def check(snap: TokenSnapshot, history: list[float], g: EntryGateConfig) -> tuple[bool, list[str]]:
    if not g.enabled:
        return True, []
    f = live_features(snap, history)
    fails = []
    if not g.min_ret5 <= f["ret5"] <= g.max_ret5:
        fails.append(f"5m move {f['ret5']:+.1%} outside [{g.min_ret5:+.0%}, {g.max_ret5:+.0%}]")
    if not g.min_ret60 <= f["ret60"] <= g.max_ret60:
        fails.append(f"1h move {f['ret60']:+.1%} outside [{g.min_ret60:+.0%}, {g.max_ret60:+.0%}]")
    if f["vol_accel"] < g.min_vol_accel:
        fails.append(f"volume accel {f['vol_accel']:.1f}x < {g.min_vol_accel}x")
    if f["buy_share"] is not None and f["buy_share"] < g.min_green:
        fails.append(f"5m buy share {f['buy_share']:.0%} < {g.min_green:.0%}")
    if f["dd60"] is not None and f["dd60"] > g.max_dd60:
        fails.append(f"{f['dd60']:.0%} below recent high")
    if f["age_min"] < g.min_age_min:
        fails.append("too young")
    return not fails, fails
