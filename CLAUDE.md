# CLAUDE.md: FOMO Tradebot

Context for Claude (or any developer) picking up this project. Read this before changing anything.
The user writes Danish; answer in Danish. User-facing docs (README.md, research/README.md) are Danish;
code, comments and commit messages are English.

## What this is

A data-driven Solana memecoin trading bot. It is "FOMO compatible": FOMO (fomo.family) has no public
trading API, so the bot trades on-chain through Jupiter from its own Solana wallet. Pump.fun tokens it
trades are the same ones visible in the FOMO app.

Strategy in one line: **buy only freshly graduated pump.fun tokens (2-30 min old) that an ML model ranks
in its top 10% and that pass tested safety filters; sell in steps; stop out fast.**

## Commands

```bash
pip install -e .            # installs the `fomobot` command
python -m pytest            # 34 tests, all must pass; also run `python -m pyflakes fomobot tests`
fomobot wallet              # create the bot's own Solana wallet (key -> .env, never printed)
fomobot scan                # one analysis pass, no trading
fomobot run --dashboard     # paper trading + phone dashboard on http://<lan-ip>:8080
fomobot run --live          # real funds; ALSO requires execution.mode: live in config.yaml
fomobot status | report     # open positions | results, win rate, meme-score check, lessons
fomobot retrain             # label own observations with real candles, retrain, keep only if better
fomobot collect | backtest  # GeckoTerminal candle dataset + momentum backtest (historical, see research)
```

Secrets live only in `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `SOLANA_RPC_URL`,
`WALLET_PRIVATE_KEY`, optional `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. `.env` and `data/` are gitignored.

## Pipeline (fomobot/engine.py, every `tick_seconds` = 30 s)

1. **Discovery**: PumpPortal websocket `subscribeMigration` (free, real-time graduations,
   `data/pumpportal.py`) + GeckoTerminal new/trending pools + DexScreener profiles/boosts/search.
   Young tokens (<= `ml.max_age_minutes`) go on a watchlist and are re-checked every tick.
2. **Market filter** (no network calls, runs on every token) then **RugCheck safety** on the survivors
   (`analysis/safety.py`, `data/rugcheck.py`).
3. **ML gate** (`intelligence/ml.py`, bundled `memecoin_model.joblib`): only tokens inside the training
   domain (pump.fun tokens, age <= 30 min) and >= the model's 90th percentile pass. pump.fun is detected
   by the mint suffix "pump", a PumpPortal graduation or RugCheck's launchpad field; Bags/LaunchLab/
   Raydium launches are rejected (the model never saw them).
4. **Rule score** (`analysis/scorer.py`, indicators) as secondary info. The price-action entry gate
   (`analysis/gate.py`) exists but is **off** by default (no proven edge).
5. **Claude analyst** (`intelligence/analyst.py`, model `claude-opus-5-5`, structured output via
   `client.beta.messages.parse`, server-side fallbacks): sees numbers, ML probability, token image +
   description (meme score 0-10, logged, not yet validated), portfolio and past lessons; can veto.
   Without an API key the bot runs on ML + rules (`rule_decision`).
6. **Sizing** (`risk.py`): % of equity x conviction x ML tier (top 5% -> 1.25x, 90-95% -> 0.75x) x
   safety size factor (unlocked LP -> 0.5x); caps on per-trade SOL, open positions, exposure, reserve.
7. **Honeypot check** (`data/jupiter_quotes.py`): Jupiter buy+sell quote round trip; no sell route or
   > 10% loss -> skip. Paper fills use the measured cost.
8. **Exits** (`risk.check_exit`): ladder 25% at +50/+100/+200%, rest trails 35%; stop -25%; never
   below entry after the first step; liquidity-collapse exit; max hold 4 h. **Fast exits** re-price open
   positions every 2 s via Jupiter price API between scans (`data/jupiter_prices.py`).
9. **Learning**: Claude writes a lesson per closed trade; every model-scored token is stored in
   `observations` for `fomobot retrain` (`learn.py`).
10. **Circuit breakers**: daily loss limit, pause after N losses in a row. Telegram notifications
    (`notify.py`), phone dashboard (`dashboard.py` + `dashboard.html`).

Execution: `execution/paper.py` (simulated, fixed 0.003 SOL cost per buy) and `execution/jupiter.py`
(live: Jupiter swap API, signs locally with solders, confirms via RPC, closes emptied token accounts to
reclaim ~0.002 SOL rent). **Live trading has never been run against mainnet**; only mocked RPC tests.

## Evidence behind the defaults (research/, details in research/README.md)

Data: public dataset github.com/ian05012/solana-memecoin-dataset (31k tokens, June 2026, ~80 features
at detection + 15m candles after). Time split: train first ~2 weeks, test last week. Costs 2.5%/side.

| Finding (test week) | Result |
|---|---|
| Buy every detected token | -11.4%/trade, 16% win |
| Momentum entry (GeckoTerminal backtest, trending pools) | -2%, same as random entries |
| ML top 10% | +13.7%/trade, 42% win (n=123) |
| ML top 10% + bot filters | **+25.9%/trade, 58% win, PF 3.67 (n=48)** |
| + requiring LP locked | -0.9% (n=7): removed the best trades -> LP is a soft risk |
| 3-step take-profit ladder | +24.8%, 67% win, PF 4.35 (same mean, more wins) |
| ML top 5% vs 90-95% | +37% vs +19%/trade -> confidence-tier sizing |

Scripts: `memecoin_dataset.py`, `train_model.py`, `filter_check.py`, `exit_ladders.py`.
Caveats: one market regime (June), small n (48), GMGN->DexScreener/RugCheck feature mapping, 15m candles,
survivorship. Edge outside 30 min age disappears (that is why the domain limit exists).

Also checked and rejected: buying in the bonding phase (arXiv 2602.14860: dumps cluster late on the curve,
no evidence of profit), first-block sniping (cost + competition, -11% base rate), tweet-following
(paid X API, too slow), market-regime filter and smart-money features (not testable with current data).

## Known API facts (verified live, Nov 2026)

- DexScreener: free; pump.fun bonding-curve pairs show liquidity $0. SOL-quoted pairs are preferred;
  since pump.fun allows curves quoted in other tokens (e.g. BUTTHOLE/ANTHROPIC), a token with only
  non-SOL pools is priced via USD (SOL/USD implied from SOL pairs in the same response).
- RugCheck `/v1/tokens/{mint}/report`: AMM/pool accounts appear in `topHolders` (exclude via
  `knownAccounts` type AMM); fresh PumpSwap pools report LP ~0% locked until re-indexed.
- PumpPortal: `subscribeMigration` / `subscribeNewToken` free; `subscribeTokenTrade` needs an API key
  funded with >= 0.02 SOL (would give even faster exits).
- Jupiter `lite-api.jup.ag/swap/v1` (quote/swap) and `/price/v3` work without a key.
- GeckoTerminal: ~10 calls/min effective; OHLCV `currency=token` gives prices in SOL.

## Rules for working on this project

- **Evidence first.** Don't add features because they sound smart. Test on data (research/), compare
  out of sample, keep only what improves results, and say so honestly when something doesn't.
- **Safety:** paper mode by default; live needs config + `--live`. Never print, log or commit the
  private key. Keep only a small amount in the bot wallet. Never promise returns.
- **Will not build:** coin launchers for profit, pump-and-dump / rug tooling, wash trading, bundling,
  paid-shill automation, ways around KYC/ID verification. These hurt buyers and are illegal (MiCA,
  Danish straffelov §279). Honest single meme coins for fun are fine to discuss.
- Keep tests green and lint clean; commit with clear messages.

## Status / next steps

- Built and tested offline; paper mode briefly run live in a cloud container (no trades in ~1 h: the
  bot is very selective, expect a few trades per day).
- Next: run paper mode on a computer for 3-7 days (~30 trades), compare `fomobot report` to the table
  above, then `fomobot retrain` once >= 300 observations exist. Only then consider live with ~0.3-0.5 SOL.
- Small-account config (~2 SOL): `config.yaml` (0.1 SOL max per trade, 3 positions, 25% exposure,
  0.1 SOL reserve, -10% daily limit).
