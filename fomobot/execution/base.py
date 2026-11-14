from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Fill, Position, TokenSnapshot


class ExecutionError(Exception):
    pass


class Executor(ABC):
    mode: str

    @abstractmethod
    def cash_sol(self) -> float: ...

    @abstractmethod
    def buy(self, snap: TokenSnapshot, sol_amount: float, expected_cost_pct: float | None = None) -> Fill:
        """expected_cost_pct: measured round-trip cost from Jupiter quotes, if available."""
        ...

    @abstractmethod
    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill: ...
