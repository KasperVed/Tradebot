"""Risk management: position sizing, exposure limits, circuit breakers and exit rules."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import RiskConfig, StrategyConfig
from .models import Decision, Position, TokenSnapshot
from .storage import Store


@dataclass
class EntryCheck:
    allowed: bool
    size_sol: float
    reason: str


@dataclass
class ExitOrder:
    fraction: float  # share of current holding to sell
    reason: str


def _day_start() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class RiskManager:
    def __init__(self, cfg: RiskConfig, strat: StrategyConfig, store: Store):
        self.cfg, self.strat, self.store = cfg, strat, store

    # --- circuit breakers ---------------------------------------------------
    def trading_paused(self, equity_sol: float) -> str | None:
        day = _day_start()
        start = self.store.get_state("day_start")
        if not start or start["ts"] < day:
            start = {"ts": day, "equity": equity_sol}
            self.store.set_state("day_start", start)
        if start["equity"] > 0:
            dd = (start["equity"] - equity_sol) / start["equity"] * 100
            if dd >= self.cfg.daily_loss_limit_pct:
                return f"daily loss limit hit ({dd:.1f}% down today)"

        paused_until = self.store.get_state("paused_until", 0)
        if time.time() < paused_until:
            return f"cooling down after losing streak until {datetime.fromtimestamp(paused_until):%H:%M}"

        recent = self.store.closed_positions(limit=self.cfg.consecutive_loss_pause)
        if len(recent) == self.cfg.consecutive_loss_pause and all((r["pnl_sol"] or 0) < 0 for r in recent):
            last = recent[0]["closed_at"]
            if self.store.get_state("streak_handled") != last:
                self.store.set_state("streak_handled", last)
                self.store.set_state("paused_until", time.time() + self.cfg.pause_minutes * 60)
                return f"{self.cfg.consecutive_loss_pause} losses in a row - pausing {self.cfg.pause_minutes:.0f} min"
        return None

    # --- entries ------------------------------------------------------------
    def check_entry(
        self, decision: Decision, mint: str, equity_sol: float, cash_sol: float, positions: list[Position], exposure_sol: float
    ) -> EntryCheck:
        if any(p.mint == mint for p in positions):
            return EntryCheck(False, 0, "already holding")
        if len(positions) >= self.cfg.max_open_positions:
            return EntryCheck(False, 0, "max open positions")
        last = self.store.last_closed_at(mint)
        if last and time.time() - last < self.strat.reentry_cooldown_minutes * 60:
            return EntryCheck(False, 0, "re-entry cooldown")

        conviction_scale = 0.5 + max(0.0, decision.conviction - 50) / 50  # 50 -> 0.5x, 100 -> 1.5x
        size = equity_sol * self.cfg.base_position_pct / 100 * conviction_scale * decision.size_multiplier
        size = min(size, self.cfg.max_position_sol)
        size = min(size, equity_sol * self.cfg.max_exposure_pct / 100 - exposure_sol)
        size = min(size, cash_sol - self.cfg.sol_reserve)
        if size < self.cfg.min_position_sol:
            return EntryCheck(False, 0, f"size {size:.4f} SOL below minimum (exposure/cash limits)")
        return EntryCheck(True, round(size, 4), "ok")

    # --- exits --------------------------------------------------------------
    def check_exit(self, pos: Position, snap: TokenSnapshot | None, now: float | None = None) -> ExitOrder | None:
        now = now or time.time()
        if snap is None:
            # No price data: only time-based exit is safe to evaluate.
            if now - pos.opened_at > self.strat.max_hold_minutes * 60 * 1.5:
                return ExitOrder(1.0, "no market data and max hold exceeded")
            return None

        price = snap.price_sol
        pos.peak_price = max(pos.peak_price, price)
        change = (price / pos.entry_price - 1) * 100

        if change <= -pos.stop_loss_pct:
            return ExitOrder(1.0, f"stop loss ({change:.1f}%)")

        entry_liq = self.store.entry_liquidity(pos.mint, pos.opened_at - 120)
        if entry_liq and snap.liquidity_usd < entry_liq * (1 - self.strat.exit_on_liquidity_drop_pct / 100):
            return ExitOrder(1.0, f"liquidity collapsed (${entry_liq:,.0f} -> ${snap.liquidity_usd:,.0f})")

        ladder = self.strat.take_profit_ladder
        if ladder:
            if pos.tp_steps < len(ladder) and change >= ladder[pos.tp_steps][0]:
                gain, share = ladder[pos.tp_steps]
                held = pos.token_amount / pos.initial_tokens if pos.initial_tokens else 1.0
                fraction = min(1.0, share / held) if held > 0 else 1.0
                return ExitOrder(fraction, f"take profit {pos.tp_steps + 1}/{len(ladder)} (+{change:.0f}%)")
        elif not pos.tp_hit and change >= pos.take_profit_pct:
            return ExitOrder(self.strat.tp_sell_fraction, f"take profit +{change:.1f}%")

        if pos.tp_hit:
            drawdown = (1 - price / pos.peak_price) * 100
            if drawdown >= self.strat.trailing_stop_pct:
                return ExitOrder(1.0, f"trailing stop ({drawdown:.1f}% off peak)")
            if self.strat.breakeven_after_tp and change < 0:  # winner must not turn into a loser
                return ExitOrder(1.0, "gave back profit below entry")

        if now - pos.opened_at > self.strat.max_hold_minutes * 60:
            return ExitOrder(1.0, f"max hold time ({self.strat.max_hold_minutes:.0f} min)")
        return None
