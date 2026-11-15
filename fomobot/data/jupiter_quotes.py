"""Jupiter quote round-trip honeypot check."""

from dataclasses import dataclass
from typing import Optional

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


@dataclass
class RoundTrip:
    ok: bool
    sol_in: Optional[float]   # SOL spent on buy
    sol_out: Optional[float]  # SOL recovered on sell
    reason: str = ""

    @property
    def loss_pct(self) -> Optional[float]:
        if self.sol_in and self.sol_out is not None and self.sol_in > 0:
            return (self.sol_in - self.sol_out) / self.sol_in * 100
        return None


class JupiterQuotes:
    def __init__(self, base_url: str, slippage_bps: int = 500, max_spread_pct: float = 10.0):
        self._base = base_url.rstrip("/")
        self._slippage = slippage_bps
        self._max_spread = max_spread_pct

    def round_trip(self, mint: str, sol_amount: float) -> RoundTrip:
        lamports = int(sol_amount * LAMPORTS)
        try:
            buy = self._quote(SOL_MINT, mint, lamports)
        except Exception as e:
            return RoundTrip(False, None, None, f"buy quote failed: {e}")
        if not buy:
            return RoundTrip(False, None, None, "no buy route on Jupiter")

        out_tokens = int(buy["outAmount"])
        buy_price = sol_amount / (out_tokens / 1e6) if out_tokens else None

        try:
            sell = self._quote(mint, SOL_MINT, out_tokens)
        except Exception as e:
            return RoundTrip(False, buy_price, None, f"sell quote failed: {e}")
        if not sell:
            return RoundTrip(False, buy_price, None, "no sell route on Jupiter (possible honeypot)")

        sol_back = int(sell["outAmount"]) / LAMPORTS
        sell_price = sol_back / (out_tokens / 1e6) if out_tokens else None
        spread = (sol_amount - sol_back) / sol_amount * 100 if sol_amount else 100

        if spread > self._max_spread:
            return RoundTrip(
                False, buy_price, sell_price,
                f"round-trip loss {spread:.1f}% > {self._max_spread}%",
            )
        return RoundTrip(True, buy_price, sell_price)

    def _quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[dict]:
        url = f"{self._base}/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": self._slippage,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return None
        return data
