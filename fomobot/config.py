"""Configuration: config.yaml for strategy, environment variables for secrets."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class DiscoveryConfig(BaseModel):
    use_latest_profiles: bool = True
    use_boosted: bool = True
    use_top_boosts: bool = True
    use_geckoterminal: bool = True  # trending + new pools (strict free rate limit)
    geckoterminal_every_ticks: int = 2  # new pools are where the validated edge is
    search_queries: list[str] = Field(default_factory=lambda: ["pumpswap", "raydium"])
    max_candidates: int = 80


class SafetyConfig(BaseModel):
    min_liquidity_usd: float = 10_000
    max_liquidity_usd: float = 5_000_000
    min_market_cap_usd: float = 15_000
    max_market_cap_usd: float = 20_000_000
    min_age_minutes: float = 2
    max_age_hours: float = 72  # the ML gate narrows this to its training domain
    min_txns_h1: int = 50
    max_top_holder_pct: float = 10.0  # largest non-pool wallet
    max_top10_pct: float = 30.0  # top-10 non-pool wallets combined
    max_creator_pct: float = 5.0
    max_insider_pct: float = 15.0  # supply held by RugCheck-detected insider networks
    max_transfer_fee_pct: float = 0.0  # Token-2022 transfer fees eat every trade
    min_holders: int = 50
    min_lp_locked_pct: float = 80.0
    reject_mint_authority: bool = True
    reject_freeze_authority: bool = True
    use_rugcheck: bool = True
    max_rugcheck_danger_risks: int = 0


class EntryGateConfig(BaseModel):
    """Sanity gate on price action (same names as the backtester's EntryParams).
    Backtests showed momentum alone has no edge over random entries, so by default it only
    removes extremes: parabolic spikes, falling knives, dead volume, seller-dominated flow.
    Off by default: the validated selector is the ML model plus the exits, and this gate was
    not part of that test. Turn it on to add your own price-action rules."""

    enabled: bool = False
    min_ret5: float = -0.05
    max_ret5: float = 0.30
    min_ret60: float = -0.25
    max_ret60: float = 3.0
    min_vol_accel: float = 0.5
    min_green: float = 0.45  # live: share of buys in the last 5 min
    min_age_min: float = 2
    max_dd60: float = 0.40


class StrategyConfig(BaseModel):
    rule_buy_threshold: float = 70  # used when the LLM is off
    llm_min_score: float = 60  # only ask Claude about candidates scoring at least this
    llm_min_conviction: float = 65
    default_take_profit_pct: float = 100
    default_stop_loss_pct: float = 25
    tp_sell_fraction: float = 0.5  # sell this share at take-profit, trail the rest
    trailing_stop_pct: float = 35
    max_hold_minutes: float = 240
    exit_on_liquidity_drop_pct: float = 50
    reentry_cooldown_minutes: float = 120
    breakeven_after_tp: bool = True
    entry_gate: EntryGateConfig = Field(default_factory=EntryGateConfig)


class RiskConfig(BaseModel):
    base_position_pct: float = 5.0  # % of equity per trade before conviction scaling
    max_position_sol: float = 0.5
    min_position_sol: float = 0.02
    max_open_positions: int = 4
    max_exposure_pct: float = 40.0
    daily_loss_limit_pct: float = 15.0
    consecutive_loss_pause: int = 3
    pause_minutes: float = 60
    sol_reserve: float = 0.05  # always keep for fees


class MLConfig(BaseModel):
    """Trade-selection model trained on ~11k real memecoin trades (research/train_model.py).
    Out of sample, only the top ~10% of its scores were profitable, so it gates entries."""

    enabled: bool = True
    min_percentile: float = 0.9
    # The model was trained on tokens 1-25 minutes after pool launch. Outside that window it
    # extrapolates, and the out-of-sample edge was gone for tokens older than ~10-30 min.
    max_age_minutes: float = 30
    reject_out_of_domain: bool = True
    model_path: str | None = None  # default: bundled fomobot/intelligence/memecoin_model.joblib


class LLMConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "high"
    decision_cache_minutes: float = 20
    max_calls_per_hour: int = 30
    reflect_on_closed_trades: bool = True
    lessons_in_prompt: int = 12
    # Optional: show Claude base rates from `fomobot backtest`. Off by default because that
    # backtest covers momentum entries on older tokens, not the ML strategy the bot now runs.
    backtest_result_path: str | None = None


class ExecutionConfig(BaseModel):
    mode: str = "paper"  # "paper" or "live"
    paper_starting_sol: float = 5.0
    paper_fee_pct: float = 1.0  # platform/swap fee assumption
    paper_slippage_pct: float = 1.5
    slippage_bps: int = 300
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    priority_fee_lamports: int | str = "auto"
    confirm_timeout_s: float = 60


class Config(BaseModel):
    tick_seconds: float = 30
    db_path: str = "fomobot.db"
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    ml: MLConfig = Field(default_factory=MLConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    # Secrets / endpoints come from the environment, never from config.yaml.
    @property
    def rpc_url(self) -> str:
        return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    @property
    def wallet_private_key(self) -> str | None:
        return os.environ.get("WALLET_PRIVATE_KEY")


def load_env_file(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config.yaml") -> Config:
    load_env_file()
    p = Path(path)
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Config.model_validate(data or {})
