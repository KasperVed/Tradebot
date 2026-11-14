# Research: hvad virker faktisk?

Alle tal er **out-of-sample**. Parametre og modeller er fundet på én del af data og målt på en del, de ikke har set.
Hver handel er simuleret med 2,5 % omkostning pr. side (fee + slippage) og 3 % ekstra slippage på stops. Hvis
stop-loss og take-profit rammes i samme candle, regnes det som stop-loss.

## Datakilder

| Kilde | Indhold |
|---|---|
| [ian05012/solana-memecoin-dataset](https://github.com/ian05012/solana-memecoin-dataset) | 31.013 Solana-memecoins (juni 2026) med ~80 features på opdagelsestidspunktet og 15-min candles bagefter |
| GeckoTerminal (gratis API) | Minut-candles for 99 trending pools (`python -m fomobot collect`) |

## Resultater

**1. At købe alle nye tokens taber penge.** Test-ugen, 3.626 handler: **-11,4 % pr. handel**, 16 % vindere.

**2. Momentum alene giver ingen edge.** Candle-backtesten på trending pools: den optimerede momentum-entry gav
-2,0 % pr. handel på testdata, mens tilfældige entries med samme exits gav -1,6 %.

**3. En model på live-tilgængelige features kan udvælge vindere.** `train_model.py` træner på de første ~2 uger
og tester på den sidste uge. Målet er, om en handel med bottens exits ender i overskud.

| Udvalg (test-ugen) | Handler | Vindere | Gns. pr. handel | Profit factor |
|---|---|---|---|---|
| Alle tokens | 3.626 | 16 % | -11,4 % | 0,50 |
| Modellens top 20 % | 748 | 26 % | -3,4 % | 0,83 |
| **Modellens top 10 %** | 123 | 42 % | **+13,7 %** | 1,95 |
| Modellens top 5 % | 22 | 59 % | +25,0 % | 3,28 |
| Top 10 %, alder ≤30 min, likviditet ≥$10k | 95 | 46 % | **+18,9 %** | 2,38 |
| Top 10 %, alder ≥10 min | 74 | 32 % | +2,5 % | 1,15 |

De vigtigste features er market cap, likviditet, LP-burn, alder, dev-beholdning, 1h-momentum, køb/salg-forhold og
antal holders.

**4. Brede exits slår scalping.** Med samme udvælgelse gav SL 25 %, salg af halvdelen ved +100 %, trailing stop
35 % og max 4 timer overskud. Stramme exits (SL 15 %, TP 30 %, 1 time) tabte.

**5. De fleste sikkerhedsfiltre fra andre bots skader.** `filter_check.py` tester filtrene på modellens
udvalgte handler i test-ugen:

| Filtersæt | Handler | Vindere | Gns. pr. handel | Profit factor |
|---|---|---|---|---|
| Ingen filtre | 123 | 42 % | +13,7 % | 1,95 |
| **Bottens filtre** (dev ≤5 %, holders ≥50, liq ≥$10k, mcap ≥$15k, alder 2-30 min, top-10 ≤30 %) | 48 | **58 %** | **+25,9 %** | **3,67** |
| Bottens filtre + krav om LP låst ≥80 % | 7 | 29 % | -0,9 % | 0,95 |

Kravet om låst LP fjernede netop de bedste handler. LP-lås er derfor et soft flag med halv positionsstørrelse, ikke en
afvisning. Rugs er med i resultaterne, fordi et likviditetstræk ses som et kursfald i candles.

## Hvad botten gør med det

- ML-modellen (`fomobot/intelligence/memecoin_model.joblib`) er porten: kun tokens i modellens top 10 % går videre
  til Claude.
- Kun tokens på 2-30 minutter, altså modellens træningsdomæne. Ældre tokens afvises, fordi modellen dér kun gætter.
- Standard-exits: SL 25 %, sælg 50 % ved +100 %, trailing stop 35 %, max 4 timer.
- Filtrene fra tabellen ovenfor. Ulåst LP giver halv størrelse.

## Undersøgt, men ikke brugt (endnu)

- [git-disl/MELT](https://github.com/git-disl/MELT) (tidligere MemeTrans) har 41.470 pump.fun-tokens fra
  dec. 2024 til mar. 2025 med 122 risiko-features fra bonding-kurven. Data ligger på Google Drive, licensen er
  CC BY-NC, og features kræver, at hver transaktion på bonding-kurven parses live. Det er et godt næste skridt til at
  teste modellen i et andet markedsregime.
- [Smurfetc/solana-memecoin-calls-dataset](https://github.com/Smurfetc/solana-memecoin-calls-dataset) har 7.235
  calls med entry-market cap og senere peak, men ingen candles, så exits kan ikke simuleres.

## Forbehold

- **Ét markedsregime.** Datasættet dækker 20 dage i juni 2026, og markedet kan have ændret sig.
- **Små tal i toppen.** 95-123 test-handler er nok til at se en tendens, men ikke nok til at være sikker.
- **Feature-oversættelse.** Datasættets features kommer fra GMGN, botten bruger DexScreener og RugCheck. Felterne er
  oversat så tæt som muligt, og felter uden pålidelig oversættelse er udeladt. Afvigelser kan svække edgen.
- **15-min candles.** Stops kan ramme værre end simuleret i et hurtigt dump.
- **Survivorship.** Datasættet ser kun tokens, der overlevede længe nok til at blive opdaget.

Derfor: kør botten i paper mode og sammenlign dens faktiske resultater med tabellen, før der bruges rigtige penge.

## Genskab

```bash
# hent tokens.parquet og ohlcv.parquet fra datasættets data/-mappe til ./ian-data
python research/memecoin_dataset.py --data ian-data     # analyse
python research/train_model.py --data ian-data          # træn og gem modellen
python research/filter_check.py --data ian-data         # hvilke sikkerhedsfiltre hjælper
python -m fomobot collect && python -m fomobot backtest # candle-backtest på GeckoTerminal-data
```
