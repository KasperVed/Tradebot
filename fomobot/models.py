"""Core data types shared across the bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class TokenSnapshot:
    """One point-in-time view of a token's best SOL-quoted pair (from DexScreener)."""

    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    price_sol: float
    price_usd: float
    liquidity_usd: float
    market_cap_usd: float
    fdv_usd: float
    volume_m5: float
    volume_h1: float
    volume_h6: float
    volume_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    change_m5: float
    change_h1: float
    change_h6: float
    change_h24: float
    pair_created_ms: int
    has_socials: bool = False
    has_website: bool = False
    boosted: bool = False
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_minutes(self) -> float:
        if not self.pair_created_ms:
            return 0.0
        return max(0.0, (self.fetched_at * 1000 - self.pair_created_ms) / 60000)


@dataclass
class SafetyReport:
    mint: str
    passed: bool
    score: float  # 0 (dangerous) .. 100 (clean)
    flags: list[str] = field(default_factory=list)
    hard_fail_reasons: list[str] = field(default_factory=list)
    top_holder_pct: float | None = None
    lp_locked_pct: float | None = None
    mint_authority: bool | None = None
    freeze_authority: bool | None = None


@dataclass
class Indicators:
    momentum: float  # -100..100
    volume_accel: float  # ratio of short-term volume rate vs longer-term rate
    buy_pressure: float  # 0..1 share of buys
    liquidity_mcap_ratio: float
    volatility: float  # stdev of recent returns (from own history), 0 if unknown
    rsi: float | None
    ema_trend: float | None  # (ema_fast/ema_slow - 1) * 100
    history_points: int


@dataclass
class Signal:
    snapshot: TokenSnapshot
    safety: SafetyReport
    indicators: Indicators
    score: float  # 0..100
    breakdown: dict[str, float]
    reasons: list[str]


class Action(str, Enum):
    BUY = "BUY"
    SKIP = "SKIP"


@dataclass
class Decision:
    action: Action
    conviction: float  # 0..100
    size_multiplier: float  # 0.25..1.5 applied to base position size
    take_profit_pct: float
    stop_loss_pct: float
    thesis: str
    risks: list[str]
    source: str  # "llm" or "rules"


@dataclass
class Fill:
    sol_amount: float  # SOL spent (buy) or received (sell), net of fees
    token_amount: float  # UI units
    token_raw: int | None  # raw base units (live only)
    price_sol: float  # effective price per token in SOL
    signature: str | None = None


@dataclass
class Position:
    id: int | None
    mint: str
    symbol: str
    entry_price: float
    token_amount: float
    token_raw: int | None
    cost_sol: float  # remaining cost basis
    opened_at: float
    stop_loss_pct: float
    take_profit_pct: float
    peak_price: float
    tp_hit: bool = False
    realized_sol: float = 0.0
    thesis: str = ""
    status: str = "open"
