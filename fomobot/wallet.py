"""`fomobot wallet`: create the bot's own Solana wallet (no Phantom needed) or show it.

The private key is written to .env (never printed) so the bot can sign trades.
Send SOL to the printed address, e.g. from Coinbase: Send -> SOL -> Solana network."""

from __future__ import annotations

import os
from pathlib import Path

import httpx2 as httpx

ENV = Path(".env")


def _set_env_key(key: str, value: str) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={value}"]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)  # only you can read the key


def balance_sol(address: str, rpc: str) -> float | None:
    try:
        r = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}, timeout=15)
        return r.json()["result"]["value"] / 1e9
    except Exception:
        return None


def wallet_command() -> None:
    from solders.keypair import Keypair

    from .config import load_env_file

    load_env_file()
    key = os.environ.get("WALLET_PRIVATE_KEY")
    if key:
        kp = Keypair.from_base58_string(key)
        print("Bottens wallet findes allerede (nøglen ligger i .env).")
    else:
        kp = Keypair()
        _set_env_key("WALLET_PRIVATE_KEY", str(kp))
        print("Ny wallet oprettet. Den private nøgle er gemt i .env - del den aldrig, og tag en backup af filen.")
    address = str(kp.pubkey())
    print(f"\nAdresse (send SOL hertil fra Coinbase, vælg Solana-netværket):\n\n    {address}\n")
    rpc = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    bal = balance_sol(address, rpc)
    print(f"Saldo: {bal:.4f} SOL" if bal is not None else "Saldo: kunne ikke hentes (tjek SOLANA_RPC_URL)")
