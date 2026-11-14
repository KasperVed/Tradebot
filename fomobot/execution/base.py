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
    def buy(self, snap: TokenSnapshot, sol_amount: float) -> Fill: ...

    @abstractmethod
    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill: ...
