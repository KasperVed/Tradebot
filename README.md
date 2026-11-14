# FOMO Tradebot

En automatisk trading-bot til Solana-memecoins, bygget til at handle fra den samme wallet som du bruger i FOMO-appen.

> **Vigtigt:** FOMO har ikke et offentligt trading-API. Botten handler derfor **direkte on-chain** via Jupiter
> (samme likviditet som FOMO selv bruger). Hvis du eksporterer din FOMO-wallets private key og giver den til
> botten, dukker dens køb og salg op i din FOMO-portefølje. Du kan også bruge en separat wallet.

> **Risiko:** Memecoins er ekstremt risikable, og de fleste ender på nul. Botten kan tabe penge. Start altid i
> **paper mode** (simuleret handel, standard), og brug kun beløb du har råd til at tabe, når du går live.

## Hvordan botten tænker

Hver 30. sekund kører den denne pipeline:

1. **Opdagelse.** Henter nye og trending Solana-tokens fra DexScreener (nye profiler, boosts og top-boosts,
   søgninger) og GeckoTerminal (trending og nye pools) plus tokens, den allerede holder øje med.
2. **Sikkerhed (hårde filtre).** Afviser tokens med mint- eller freeze-authority, lav LP-lås, en enkelt wallet over
   10 %, top-10-wallets over 30 %, en creator der stadig ejer over 5 %, insider-netværk over 15 %, transfer-fees,
   for få holders, for lav likviditet, for ung alder eller for få handler. Data kommer fra RugCheck. AMM-pools og
   lockers tælles ikke med som holders. Et token, der fejler her, bliver aldrig købt.
3. **Entry-port (backtestet).** Kun setups af den type, der historisk har vist en edge i backtesten: et bestemt
   5-minutters og 1-times kursudsving, stigende volumen, overvægt af købere og ikke for langt under det seneste top.
4. **Kvantitativ analyse.** Beregner momentum over flere tidsrammer, volumenacceleration, købstryk (køb/salg),
   forholdet mellem likviditet og market cap samt EMA-trend, RSI og volatilitet ud fra prishistorik som botten selv
   gemmer. Resultatet samles i en score fra 0 til 100 med begrundelser. Parabolske stigninger straffes, så botten
   ikke køber toppen.
5. **Intelligens (Claude).** Kandidater over score-tærsklen sendes til Claude (Opus 5.5) sammen med porteføljen,
   **lektioner fra tidligere handler** og **historiske base rates fra backtesten**, så Claudes conviction kalibreres
   mod virkelige tal. Claude svarer BUY eller SKIP med conviction, positionsstørrelse,
   take-profit, stop-loss, en tese og risici. Uden en API-nøgle kører botten alene på regler.
6. **Risikostyring.** Positionsstørrelse skaleres efter conviction. Der er loft over antal positioner, samlet
   eksponering og SOL pr. handel, og der holdes en SOL-reserve til fees. En **daglig tabsgrænse** og en **pause efter
   flere tab i træk** stopper nye køb.
7. **Exits.** Stop-loss, delvis take-profit (sælger 50 %, resten følger en trailing stop), exit hvis likviditeten
   kollapser, og en maksimal holdetid.
8. **Læring.** Når en handel lukkes, skriver Claude en kort lektion. Lektionen gemmes og bruges i fremtidige
   beslutninger.

Alt logges i SQLite (`fomobot.db`): beslutninger med begrundelse, handler, positioner, priser og lektioner.

## Kom i gang

```bash
pip install -r requirements.txt
cp .env.example .env          # indsæt din ANTHROPIC_API_KEY
python -m fomobot scan        # analysér markedet én gang uden at handle
python -m fomobot run         # paper trading (simuleret, start-saldo 5 SOL)
python -m fomobot status      # åbne positioner
python -m fomobot report      # resultater, win rate og lærte lektioner
```

## Backtest på rigtige data

```bash
python -m fomobot collect     # henter minut-candles for et par hundrede Solana-memecoin-pools (GeckoTerminal, gratis)
python -m fomobot backtest    # tuner entry og exit på halvdelen af tokens og tester på den anden halvdel
```

Backtesten er bevidst konservativ: den køber på næste candles åbning, regner gebyr og slippage ind på begge sider,
lader stops glide yderligere, og hvis stop-loss og take-profit rammes i samme candle, antager den stop-loss. Den
sammenligner med tilfældige entries med de samme exits for at vise, om entry-signalet faktisk tilfører noget.
Resultatet gemmes i `data/history/backtest_result.json` og vises automatisk til Claude.

Hvad backtesten ikke kan se: holder-data, RugCheck og købs-/salgsantal bagud i tid. De filtre fjerner kun handler,
så de valideres i paper mode.

## Gå live (rigtige penge)

1. Lad botten køre i paper mode i mindst et par dage, og tjek `report`.
2. Sæt `SOLANA_RPC_URL` (brug en betalt RPC, fx Helius eller QuickNode, for pålidelighed) og `WALLET_PRIVATE_KEY`
   (base58) i `.env`.
3. Sæt `execution.mode: live` i `config.yaml`.
4. Start med `python -m fomobot run --live`.

Begge dele kræves (config og flag), så du ikke handler med rigtige penge ved en fejl. Live-data gemmes i en separat
database (`fomobot-live.db`). Din private key forlader aldrig din maskine: transaktioner signeres lokalt.

## Tilpasning

Hele strategien styres fra `config.yaml`: sikkerhedsgrænser, score-tærskler, TP/SL, positionsstørrelse,
tabsgrænser, Claude-model og effort samt maks. antal Claude-kald i timen (for at holde omkostningen nede).

## Struktur

```
fomobot/
  data/          DexScreener, GeckoTerminal (markedsdata) og RugCheck (sikkerhed)
  analysis/      safety.py (filtre), gate.py (backtestet entry-port), indicators.py, scorer.py
  backtest/      dataset.py (GeckoTerminal-data), engine.py (simulator), report.py
  intelligence/  analyst.py (Claude: beslutninger og refleksion)
  execution/     paper.py (simulering), jupiter.py (live swaps på Solana)
  risk.py        sizing, limits, circuit breakers, exit-regler
  engine.py      hovedloopet
  storage.py     SQLite
tests/           pytest-suite med falske markedsdata
```

Kør tests med `python -m pytest`.
