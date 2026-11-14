"""Paper trading: simulated fills at the current DexScreener price with fees and slippage."""

from __future__ import annotations

from ..config import ExecutionConfig
from ..models import Fill, Position, TokenSnapshot
from ..storage import Store
from .base import ExecutionError, Executor


class PaperExecutor(Executor):
    mode = "paper"

    def __init__(self, cfg: ExecutionConfig, store: Store):
        self.cfg, self.store = cfg, store
        if self.store.get_state("paper_cash") is None:
            self.store.set_state("paper_cash", cfg.paper_starting_sol)

    def cash_sol(self) -> float:
        return float(self.store.get_state("paper_cash"))

    def _cost(self) -> float:
        return (self.cfg.paper_fee_pct + self.cfg.paper_slippage_pct) / 100

    def buy(self, snap: TokenSnapshot, sol_amount: float, expected_cost_pct: float | None = None) -> Fill:
        cash = self.cash_sol()
        if sol_amount > cash:
            raise ExecutionError("insufficient paper balance")
        cost = self._cost()
        if expected_cost_pct is not None:  # half of the quoted round trip is the buy side
            cost = max(cost, self.cfg.paper_fee_pct / 100 + expected_cost_pct / 200)
        fixed = min(self.cfg.paper_fixed_cost_sol, sol_amount * 0.5)
        tokens = (sol_amount - fixed) * (1 - cost) / snap.price_sol
        self.store.set_state("paper_cash", cash - sol_amount)
        return Fill(sol_amount=sol_amount, token_amount=tokens, token_raw=None, price_sol=sol_amount / tokens)

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill:
        if snap is None:
            raise ExecutionError("no price to simulate sell")
        tokens = pos.token_amount * fraction
        proceeds = tokens * snap.price_sol * (1 - self._cost())
        self.store.set_state("paper_cash", self.cash_sol() + proceeds)
        return Fill(sol_amount=proceeds, token_amount=tokens, token_raw=None, price_sol=proceeds / tokens if tokens else 0)
