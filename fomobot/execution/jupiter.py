"""Live trading on Solana via the Jupiter swap API, signed locally with your wallet key.

FOMO has no public trading API. Tokens bought here land in the wallet whose key you
provide, so if you export your FOMO wallet's private key the positions show up in the app.
"""

from __future__ import annotations

import base64
import logging
import time

import httpx2 as httpx

from ..config import Config
from ..models import WSOL_MINT, Fill, Position, TokenSnapshot
from .base import ExecutionError, Executor

log = logging.getLogger(__name__)
LAMPORTS = 1_000_000_000


class JupiterExecutor(Executor):
    mode = "live"

    def __init__(self, cfg: Config, http: httpx.Client | None = None):
        from solders.keypair import Keypair  # imported lazily so paper mode needs no solders

        if not cfg.wallet_private_key:
            raise ExecutionError("WALLET_PRIVATE_KEY is not set")
        self.kp = Keypair.from_base58_string(cfg.wallet_private_key)
        self.pubkey = str(self.kp.pubkey())
        self.cfg = cfg.execution
        self.rpc_url = cfg.rpc_url
        self.http = http or httpx.Client(timeout=30)
        self._decimals: dict[str, int] = {}

    # --- RPC helpers ----------------------------------------------------------
    def _rpc(self, method: str, params: list):
        r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise ExecutionError(f"RPC {method}: {data['error']}")
        return data["result"]

    def cash_sol(self) -> float:
        return self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])["value"] / LAMPORTS

    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            self._decimals[mint] = int(self._rpc("getTokenSupply", [mint])["value"]["decimals"])
        return self._decimals[mint]

    def token_balance_raw(self, mint: str) -> int:
        res = self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0
        for acc in res.get("value", []):
            total += int(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        return total

    # --- swap ---------------------------------------------------------------
    def _swap(self, input_mint: str, output_mint: str, amount_raw: int) -> tuple[dict, str]:
        from solders.transaction import VersionedTransaction

        q = self.http.get(
            f"{self.cfg.jupiter_base_url}/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_raw),
                "slippageBps": self.cfg.slippage_bps,
            },
        )
        if q.status_code != 200:
            raise ExecutionError(f"Jupiter quote failed: {q.status_code} {q.text[:200]}")
        quote = q.json()
        impact = float(quote.get("priceImpactPct") or 0) * 100
        if impact > 5:
            raise ExecutionError(f"price impact too high ({impact:.1f}%)")

        body = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": False,
            "prioritizationFeeLamports": self.cfg.priority_fee_lamports,
        }
        s = self.http.post(f"{self.cfg.jupiter_base_url}/swap", json=body)
        if s.status_code != 200:
            raise ExecutionError(f"Jupiter swap build failed: {s.status_code} {s.text[:200]}")
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(s.json()["swapTransaction"]))
        signed = VersionedTransaction(raw_tx.message, [self.kp])
        sig = self._rpc(
            "sendTransaction",
            [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )
        self._confirm(sig)
        return quote, sig

    def _confirm(self, sig: str) -> None:
        deadline = time.time() + self.cfg.confirm_timeout_s
        while time.time() < deadline:
            st = self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])["value"][0]
            if st:
                if st.get("err"):
                    raise ExecutionError(f"transaction {sig} failed: {st['err']}")
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(2)
        raise ExecutionError(f"transaction {sig} not confirmed in time (check explorer before retrying)")

    def buy(self, snap: TokenSnapshot, sol_amount: float, expected_cost_pct: float | None = None) -> Fill:
        before = self.token_balance_raw(snap.mint)
        quote, sig = self._swap(WSOL_MINT, snap.mint, int(sol_amount * LAMPORTS))
        received = self.token_balance_raw(snap.mint) - before or int(quote["outAmount"])
        ui = received / 10 ** self.decimals(snap.mint)
        log.info("BUY %s: %.4f SOL -> %s tokens (%s)", snap.symbol, sol_amount, ui, sig)
        return Fill(sol_amount=sol_amount, token_amount=ui, token_raw=received, price_sol=sol_amount / ui, signature=sig)

    def sell(self, pos: Position, fraction: float, snap: TokenSnapshot | None) -> Fill:
        held = self.token_balance_raw(pos.mint)
        if held <= 0:
            raise ExecutionError(f"no {pos.symbol} balance in wallet")
        amount = held if fraction >= 0.999 else int(held * fraction)
        quote, sig = self._swap(pos.mint, WSOL_MINT, amount)
        sol_out = int(quote["outAmount"]) / LAMPORTS
        ui = amount / 10 ** self.decimals(pos.mint)
        log.info("SELL %s: %s tokens -> ~%.4f SOL (%s)", pos.symbol, ui, sol_out, sig)
        return Fill(sol_amount=sol_out, token_amount=ui, token_raw=amount, price_sol=sol_out / ui if ui else 0, signature=sig)
