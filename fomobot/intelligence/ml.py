"""Trade-selection model trained on ~11k real Solana memecoin trades (see research/train_model.py).

Predicts the probability that a trade with the bot's exit rules ends in profit. Inputs are
rebuilt from DexScreener + RugCheck in the units the training data used (fractions, not %).
Fields we cannot map reliably are passed as NaN, which the model handles natively."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from ..models import TokenSnapshot

log = logging.getLogger(__name__)
DEFAULT_PATH = Path(__file__).with_name("memecoin_model.joblib")


def _frac(pct: float | None) -> float:
    return pct / 100 if pct is not None else math.nan


def live_features(snap: TokenSnapshot, rug: dict | None) -> dict[str, float]:
    rug = rug or {}
    nan = math.nan
    mint, freeze = rug.get("mint_authority"), rug.get("freeze_authority")
    return {
        # DexScreener pair age; for pump.fun tokens this is time since graduation, a proxy for launch age.
        "time_since_launch_sec": snap.age_minutes * 60 if snap.pair_created_ms else nan,
        "market_cap_usd": snap.market_cap_usd or nan,
        "liquidity_usd": snap.liquidity_usd or nan,
        "holder_count": float(rug["total_holders"]) if rug.get("total_holders") is not None else nan,
        "top10_holder_pct": _frac(rug.get("top10_pct")),
        "dev_remaining_pct": _frac(rug.get("creator_pct")),
        "mint_disabled": nan if mint is None else float(not mint),
        "freeze_disabled": nan if freeze is None else float(not freeze),
        "lp_burned_pct": _frac(rug.get("lp_locked_pct")),
        "price_change_1h": snap.change_h1,
        "price_change_6h": snap.change_h6,
        "has_twitter": float("twitter" in snap.social_types),
        "has_website": float(snap.has_website),
        "has_telegram": float("telegram" in snap.social_types),
        "is_boosted": float(snap.boosted),
        "trade_buy_sell_ratio": snap.buys_h1 / max(snap.sells_h1, 1),
        "liq_mcap": snap.liquidity_usd / snap.market_cap_usd if snap.market_cap_usd else nan,
    }


class TradeModel:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        import joblib  # sklearn dependency only needed when the model is used

        bundle = joblib.load(path)
        self.model, self.meta = bundle["model"], bundle["meta"]
        self.features: list[str] = self.meta["features"]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "TradeModel | None":
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            log.warning("ML model not found at %s - ML filter disabled", p)
            return None
        try:
            return cls(p)
        except Exception as e:  # sklearn missing / incompatible pickle
            log.warning("could not load ML model (%s) - ML filter disabled", e)
            return None

    def threshold(self, percentile: float) -> float:
        pts = {float(k): v for k, v in self.meta["percentiles"].items()}
        key = min(pts, key=lambda q: abs(q - percentile))
        return pts[key]

    def predict(self, snap: TokenSnapshot, rug: dict | None) -> float:
        import pandas as pd

        f = live_features(snap, rug)
        x = pd.DataFrame([[f[name] for name in self.features]], columns=self.features, dtype=float)
        return float(self.model.predict_proba(x)[0, 1])
