# Backtest BtcTrend (Bitvavo BTC/EUR, Tageskerzen)

Stand: 15.09.2026 (zweiter Lauf nach der Korrektur der Rebalance-Verkäufe, siehe Abschnitt "Korrektur der Rebalance-Verkäufe"). Freqtrade 2026.8, ccxt 4.5.78, Python 3.11.

Dieser Bericht dokumentiert den ersten vollständigen Backtest der Startstrategie `BtcTrend` aus `user_data/strategies/BtcTrend.py`. Er ist keine Anlageberatung. Die Zahlen sind ein Backtest, kein Nachweis für die Zukunft.

## Kurzfassung

- Zeitraum mit Trades: 04.10.2019 bis 14.09.2026 (6,95 Jahre). Die Bitvavo-Daten beginnen am 08.03.2019; die ersten 210 Kerzen braucht die Strategie als Anlauf für SMA200 und Volatilität.
- Bot mit Maker-Gebühr (0,15 %): +259,6 % Gesamtrendite, 20,2 % CAGR, maximaler Drawdown 19,5 % (geschlossene Trades) bzw. 40,1 % (tägliche Wallet-Bilanz, inklusive offener Position).
- Bot mit Taker-Gebühr (0,25 %): +251,0 %, 19,8 % CAGR, Drawdown 20,0 % bzw. 40,5 %.
- Buy-and-Hold im selben Zeitraum: +806 % bis +808 %, 37,4 % CAGR, maximaler Drawdown 73,6 %.
- `lookahead-analysis`: kein Bias (12 Signale, 0 verzerrt). `recursive-analysis`: keine Abweichung bei 210, 300 und 400 Anlaufkerzen.
- Fazit: Die Strategie schlägt Buy-and-Hold nicht. Sie senkt den Drawdown auf geschlossenen Trades auf gut ein Viertel, auf Basis der täglichen Bilanz auf gut die Hälfte, und war nur 55 % der Zeit investiert. Das ist die Erwartung aus `docs/PROJEKTPLAN.md` (Abschnitt 1 und 6), aber mit weniger Drawdown-Schutz, als der erste Lauf mit falsch dimensionierten Rebalance-Verkäufen ausgewiesen hat.

## Kommandos

Alle Läufe sind reproduzierbar über `scripts/backtest.sh`. Parameter kommen aus Umgebungsvariablen, Defaults stehen im Skript. Ohne `TIMERANGE` nimmt das Skript `20190901-` (offenes Ende, also alle geladenen Kerzen); für die Zahlen hier ist das Ende fest auf den 15.09.2026 gesetzt. Zeigt `USERDIR` auf ein reines Datenverzeichnis ohne `config.json` (etwa das von `scripts/download-data.sh` befüllte), nimmt das Skript `config.json` und `config-private.example.json` aus dem Repo. `CONFIG_DIR` überschreibt das.

```bash
# Daten laden (öffentliche Bitvavo-API, kein Key)
FT_BIN=/home/user/ft-venv/bin/freqtrade scripts/download-data.sh

# Backtest mit Maker-Gebühr, danach lookahead-analysis und recursive-analysis
FT_BIN=/home/user/ft-venv/bin/freqtrade \
DATADIR=/home/user/ftdata/user_data/data/bitvavo \
TIMERANGE=20190901-20260915 FEE=0.0015 scripts/backtest.sh

# Backtest mit Taker-Gebühr (ohne die beiden Analysen)
FT_BIN=/home/user/ft-venv/bin/freqtrade \
DATADIR=/home/user/ftdata/user_data/data/bitvavo \
TIMERANGE=20190901-20260915 FEE=0.0025 SKIP_ANALYSIS=1 scripts/backtest.sh
```

Das Skript ruft im Kern diese Freqtrade-Befehle auf:

```bash
freqtrade backtesting --userdir user_data --config config.json --config config-private.example.json \
  --strategy-path user_data/strategies --strategy BtcTrend --datadir <DATADIR> \
  --timeframe 1d --timerange 20190901-20260915 --fee 0.0015 --enable-protections \
  --breakdown year --cache none --export trades --backtest-directory user_data/backtest_results

FREQTRADE__ENTRY_PRICING__PRICE_SIDE=other FREQTRADE__EXIT_PRICING__PRICE_SIDE=other \
freqtrade lookahead-analysis ... --fee 0.0015 --minimum-trade-amount 10 --targeted-trade-amount 20

freqtrade recursive-analysis ... --startup-candle 210 300 400
```

## Daten und Annahmen

| Punkt | Wert |
|---|---|
| Datenquelle | Bitvavo, `BTC/EUR`, Tageskerzen, Feather, geladen mit `freqtrade download-data` |
| Verfügbare Daten | 08.03.2019 bis 14.09.2026 (2.748 Kerzen) |
| Angeforderter Zeitraum | `20190901-20260915` |
| Effektiver Backtest | 04.10.2019 bis 14.09.2026, weil die Strategie 210 Anlaufkerzen braucht und die Daten erst im März 2019 beginnen |
| Startkapital | 1.000 EUR (`dry_run_wallet`), `stake_amount: unlimited`, `tradable_balance_ratio: 0.99`, `max_open_trades: 1` |
| Gebühren | 0,15 % je Seite (Bitvavo Maker, Kategorie A Stufe 0) und zum Vergleich 0,25 % (Taker). Freqtrade wendet `--fee` auf jede Order an, auch auf Rebalance-Orders |
| Spread und Slippage | nicht simuliert. Freqtrade füllt Limit-Orders im Backtest, sobald der Kurs in der Kerze liegt. Für Tageskerzen und einen gemessenen Spread von unter 1 bps ist der Fehler klein, aber nicht null |
| Protections | aktiv (`--enable-protections`): CooldownPeriod 2 Kerzen, StoplossGuard 2 Stops in 30 Kerzen mit 10 Kerzen Pause, MaxDrawdown 10 % über 30 Kerzen mit 10 Kerzen Pause |
| Strategieparameter | Defaults aus `docs/KOMPONENTEN.md` Abschnitt 7: SMA 200, Band 3 %, Mindesthaltedauer 5 Kerzen, Zielvolatilität 35 %, Vol-Fenster 30 Tage, Rebalance-Band 15 Prozentpunkte, Mindestorder max(5 EUR, 2 % des Kapitals), Stoploss -10 % |

Buy-and-Hold ist wie in `docs/KOMPONENTEN.md` Abschnitt 5 gerechnet: Kauf am 04.10.2019 zum Eröffnungskurs (7.430,40 EUR) mit Taker-Gebühr, Bewertung zum Schlusskurs am 14.09.2026 (67.682 EUR), Verkaufsgebühr abgezogen. Freqtrade nennt denselben Wert als `Market change` (807,89 %, Schluss zu Schluss ohne Gebühr).

## Ergebnis Bot gegen Buy-and-Hold

| Kennzahl | Bot, Maker 0,15 % | Bot, Taker 0,25 % | Buy-and-Hold |
|---|---|---|---|
| Endkapital aus 1.000 EUR | 3.596 EUR | 3.510 EUR | 9.063 EUR (Taker) bis 9.081 EUR (Maker) |
| Gesamtrendite | +259,6 % | +251,0 % | +806 % bis +808 % |
| CAGR | 20,2 % | 19,8 % | 37,4 % |
| Max. Drawdown (geschlossene Trades) | 19,5 % (20.05.2021 bis 10.03.2023, 659 Tage) | 20,0 % | entfällt |
| Max. Drawdown (tägliche Bilanz) | 40,1 % (14.04.2021 bis 11.03.2023, 696 Tage) | 40,5 % | 73,6 % (08.11.2021 bis 21.11.2022) |
| Anzahl Trades | 13 (12 geschlossen, 1 am Ende offen) | 13 | 1 |
| Anzahl Orders (inkl. Rebalance) | 65 (32 Käufe, 33 Verkäufe) | 65 | 2 |
| Gebühren gesamt | 90,91 EUR | 149,61 EUR | ca. 15 bis 25 EUR |
| Handelsvolumen | 60.609 EUR | 59.843 EUR | ca. 10.000 EUR |
| Zeit im Markt | 54,9 % (1.392 von 2.537 Tagen) | 54,9 % | 100 % |
| Trefferquote | 5 von 13 (38,5 %) | 5 von 13 | entfällt |
| Profit Factor | 3,68 | 3,56 | entfällt |
| Sharpe (tägliche Bilanz) | 0,81 | 0,79 | nicht berechnet |
| p-Wert mittlerer Trade-Gewinn | 0,22 | 0,23 | entfällt |

Der Gebührenunterschied Maker zu Taker kostet über den ganzen Zeitraum rund 59 EUR beziehungsweise knapp 1 Prozentpunkt Rendite. Die Strategie ist bei beiden Gebührenstufen stabil, weil sie selten handelt.

Der Drawdown auf Basis der täglichen Bilanz (40 %) ist deutlich größer als der auf geschlossenen Trades (19,5 %). Er entsteht im Trade vom 30.04.2020 bis 20.05.2021: Die Bilanz erreicht am 14.04.2021 rund 3.010 EUR, der Kurs fällt bis zum Exit-Signal um gut ein Drittel, danach folgen vier Verlust-Trades bis März 2023. Der Katastrophenstop von -10 % bezieht sich auf den mittleren Einstiegskurs, nicht auf das Bilanzhoch; nach vielen Nachkäufen im Aufwärtstrend liegt er weit unter dem Hoch. Die Vol-Steuerung begrenzt die Exposure in dieser Phase auf 40 bis 60 %, deshalb bleibt der Drawdown bei rund 40 % statt 74 % für Buy-and-Hold.

## Jahre 2019 bis 2026

Freqtrade ordnet den Gewinn eines Trades dem Jahr zu, in dem der Trade geschlossen wurde. Der Trade vom 30.04.2020 bis 20.05.2021 (+1.332 EUR) zählt deshalb ganz zu 2021. Buy-and-Hold ist je Kalenderjahr von Eröffnung zu Schluss gerechnet, 2019 ab dem 04.10., 2026 bis zum 14.09.

| Jahr | Bot Trades | Bot Gewinn (Maker) | Buy-and-Hold |
|---|---|---|---|
| 2019 | 1 | -22 EUR | -13,9 % |
| 2020 | 1 | -69 EUR | +270,5 % |
| 2021 | 3 | +1.112 EUR | +71,7 % |
| 2022 | 1 | -121 EUR | -62,1 % |
| 2023 | 2 | -6 EUR | +149,3 % |
| 2024 | 2 | +1.139 EUR | +134,6 % |
| 2025 | 2 | +366 EUR | -17,4 % |
| 2026 | 1 | +199 EUR (offener Trade, am Ende geschlossen) | -9,1 % |

Lesart: In den Bärenjahren 2022 und 2025 verliert der Bot wenig oder gewinnt. In den starken Jahren 2020 und 2023 hinkt er weit hinterher, weil der Einstieg erst nach dem Trendbruch über SMA200 plus 3 % kommt und die Vol-Steuerung die Position bei hoher Volatilität auf 40 bis 60 % begrenzt.

## Einzeltrades (Maker)

| Einstieg | Ausstieg | Ergebnis | Grund | Orders |
|---|---|---|---|---|
| 27.10.2019 | 09.11.2019 | -22 EUR (-4,9 %) | exit_signal | 2 |
| 29.01.2020 | 27.02.2020 | -69 EUR (-8,7 %) | Stoploss nach Rebalance | 4 |
| 30.04.2020 | 20.05.2021 | +1.332 EUR (+58,7 %) | exit_signal | 16 |
| 10.08.2021 | 21.09.2021 | -87 EUR (-7,2 %) | exit_signal | 2 |
| 02.10.2021 | 04.12.2021 | -133 EUR (-10,3 %) | Stoploss nach Rebalance | 3 |
| 15.12.2021 | 05.01.2022 | -121 EUR (-9,2 %) | stop_loss | 3 |
| 21.01.2023 | 10.03.2023 | -97 EUR (-6,0 %) | stop_loss | 3 |
| 13.03.2023 | 19.08.2023 | +90 EUR (+4,6 %) | exit_signal | 5 |
| 17.10.2023 | 06.07.2024 | +1.379 EUR (+30,1 %) | exit_signal | 11 |
| 16.07.2024 | 04.08.2024 | -240 EUR (-10,3 %) | stop_loss | 2 |
| 15.10.2024 | 10.03.2025 | +565 EUR (+13,4 %) | exit_signal | 7 |
| 09.05.2025 | 05.11.2025 | -199 EUR (-5,6 %) | exit_signal | 4 |
| 21.08.2026 | 14.09.2026 | +199 EUR (+5,9 %) | force_exit (Ende Backtest) | 3 |

Freqtrade meldet den Stoploss als `trailing_stop_loss`, wenn er nach einer Positionsanpassung neu berechnet wurde. Die Strategie hat keinen Trailing-Stop; es ist der feste Katastrophenstop von -10 % auf den mittleren Einstiegskurs. Fünf von 13 Trades enden im Stop. Das ist der Preis eines engen Stops bei einer Volatilität von 40 bis 80 % p. a. Die Prozentangabe je Trade bezieht sich auf den investierten Betrag (`stake_amount`), der durch Rebalance-Verkäufe im Trade sinkt; deshalb sind +58,7 % für 2020/21 mehr Prozent, aber weniger EUR als im ersten Lauf.

## Lookahead- und Recursive-Analyse

`lookahead-analysis` (Maker-Gebühr, Marktorders erzwungen, 12 Signale statt der angestrebten 20, weil die Strategie so selten handelt):

| Datei | Strategie | Bias | Signale | verzerrte Entries | verzerrte Exits | verzerrte Indikatoren |
|---|---|---|---|---|---|---|
| BtcTrend.py | BtcTrend | Nein | 12 | 0 | 0 | keine |

`recursive-analysis` mit 210, 300 und 400 Anlaufkerzen: `No variance on indicator(s) found due to recursive formula.` Die Indikatoren `sma_200`, `rvol_30`, `sma`, `rvol`, `band_upper`, `band_lower` sind an jeder Kerze unabhängig davon, wie viele Kerzen davor geladen wurden. Ein erster Lauf zeigte bei `rvol` Rundungsrauschen von -0,000 %, weil die rollierende Standardabweichung von pandas ein Online-Verfahren nutzt. Die Bibliothek rechnet das Fenster seit dem Fix explizit (`rolling().apply(np.std)`), danach ist die Abweichung exakt null.

## Ehrliche Einordnung

1. Die Strategie ist ein Drawdown-Filter, kein Rendite-Bringer. 20 % CAGR gegen 37 % für Buy-and-Hold, dafür 20 % (geschlossene Trades) bis 40 % (tägliche Bilanz) statt 74 % Drawdown. Wer die 74 % aushält, fährt mit Buy-and-Hold besser, auch steuerlich (Einjahresfrist, `docs/PROJEKTPLAN.md` Abschnitt 3).
2. 13 Trades sind statistisch wenig. Der p-Wert von 0,22 sagt: Der mittlere Trade-Gewinn ist nicht signifikant von null verschieden. Zwei Trades (2020/21 und 2023/24) liefern fast den gesamten Gewinn.
3. Der Backtest enthält keinen Spread, keine Slippage und keine Teilfüllungen. Maker-Fills sind im Backtest sicher, live nicht (Adverse Selection). Der Dry-Run muss zeigen, wie viele Orders wirklich als Maker füllen.
4. Der Zeitraum beginnt im Oktober 2019 und enthält den Bärenmarkt 2018 nicht. Der Projektplan verlangt Daten ab 2017 (Kraken-CSV). Das steht noch aus.
5. Die Parameter sind nicht optimiert. Die Sensitivitätsläufe unten zeigen, dass einzelne Änderungen die Rendite um plus/minus 30 Prozentpunkte verschieben. Das ist normal bei 13 Trades und kein Grund, Parameter auf diesen Zeitraum zu tunen.
6. Gebühren sind bei dieser Frequenz kein Problem: 91 EUR Gebühren auf 2.596 EUR Gewinn über sieben Jahre.

## Konfigurationen, die geprüft wurden

Kein Hyperopt. Neun manuelle Läufe (Basis plus acht Varianten), alle mit Maker-Gebühr 0,15 %, Protections aktiv, gleicher Zeitraum. Die Varianten sind Unterklassen von `BtcTrend` mit genau einer Änderung und liegen nicht im Repo. Die Basis bleibt die verbindliche Konfiguration aus `docs/KOMPONENTEN.md`.

| Nr. | Änderung | Trades | Endkapital | CAGR | Max. DD (Trades) | Max. DD (Bilanz) |
|---|---|---|---|---|---|---|
| 0 | Basis (Vertrag) | 13 | 3.596 EUR | 20,2 % | 19,5 % | 40,1 % |
| 1 | `band_pct` 0,02 | 13 | 4.214 EUR | 23,0 % | 15,2 % | 38,3 % |
| 2 | `band_pct` 0,05 | 12 | 3.743 EUR | 20,9 % | 21,0 % | 41,2 % |
| 3 | `stoploss` -0,15 | 11 | 3.832 EUR | 21,3 % | 12,7 % | 37,8 % |
| 4 | `stoploss` -0,20 | 11 | 3.765 EUR | 21,0 % | 14,8 % | 37,1 % |
| 5 | `target_vol` 0,50 | 15 | 2.955 EUR | 16,9 % | 46,4 % | 64,1 % |
| 6 | `min_hold_candles` 0 | 13 | 3.596 EUR | 20,2 % | 19,5 % | 40,1 % |
| 7 | Rebalance aus (`position_adjustment_enable = False`) | 13 | 3.525 EUR | 19,9 % | 25,6 % | 53,6 % |
| 8 | `sma_period` 100 | 17 | 4.626 EUR | 24,7 % | 27,5 % | 36,9 % |

Beobachtungen: Die Mindesthaltedauer hat in diesem Zeitraum nie gegriffen (Variante 6 identisch). Die Vol-Steuerung ist der wichtigste Baustein für den Drawdown (Variante 5). Das Rebalancing senkt den Bilanz-Drawdown von 54 % auf 40 % und den Trade-Drawdown von 26 % auf 19,5 % (Variante 7); im ersten Lauf sah der Effekt größer aus, weil die Rebalance-Verkäufe in Gewinn-Trades zu groß ausgeführt wurden. Ein weiterer Stop senkt den Trade-Drawdown, ändert aber die Rendite kaum (3 und 4). Ein engeres Band oder ein kürzerer SMA bringen mehr Rendite, aber mehr Trades und mehr Modellrisiko. Die Vertragswerte bleiben.

## Korrektur der Rebalance-Verkäufe

Der erste Lauf vom 15.09.2026 (3.635 EUR Endkapital, 24,6 % Bilanz-Drawdown) war fehlerhaft. `adjust_trade_position` gab den Verkaufswert in EUR zum aktuellen Kurs zurück. Freqtrade 2026.8 rechnet einen negativen Wert aber relativ zum investierten Betrag um: verkaufte Menge = `|Wert| * trade.amount / trade.stake_amount` (`freqtradebot.check_and_call_adjust_trade_position` und `optimize/backtesting._check_adjust_trade_for_candle`). In Gewinn-Trades wurde deshalb um den Faktor Kurs / mittlerer Einstiegskurs zu viel verkauft (im Trade 2020/21 bis zum 3,3-Fachen), in Verlust-Trades zu wenig. Seit der Korrektur rechnet die Strategie den Anteil der Position aus, der verkauft werden soll, und gibt `-Anteil * trade.stake_amount` zurück (`btctrend_lib.partial_exit_stake`). Damit verkauft Freqtrade genau die geplante BTC-Menge, unabhängig vom Kurs. Der Test `tests/strategy/test_strategy_classes.py::test_adjust_trade_position_sell_is_sized_in_freqtrade_units` rechnet den Rückgabewert durch die Freqtrade-Formel zurück.

Folgen im Backtest: 6 Orders weniger (65 statt 71), rund 40 EUR weniger Endkapital, Bilanz-Drawdown 40 % statt 25 %. Der frühere Wert war ein Artefakt: Die zu großen Verkäufe haben die Position im Aufwärtstrend 2021 weit unter die Ziel-Exposure gedrückt und so den Absturz im Mai 2021 abgefedert, was die Regel aus `docs/KOMPONENTEN.md` Abschnitt 7 so nicht vorsieht. Alle Zahlen in diesem Bericht stammen aus dem korrigierten Lauf.

## Hinweise

- Post-only bei Bitvavo: Freqtrade 2026.8 erlaubt für Bitvavo nur `order_time_in_force: GTC` (`freqtrade/exchange/bitvavo.py` setzt kein `order_time_in_force`, der Default in `exchange.py` ist `["GTC"]`; `validate_order_time_in_force` lehnt `PO` ab). ccxt könnte `timeInForce: "PO"` in `postOnly: true` übersetzen (`ccxt/bitvavo.py createOrder`). Der Weg über `exchange._ft_has_params` ist undokumentiert und wird nicht genutzt. Folge: Limit-Orders mit GTC, `entry_pricing.price_side: "same"`, `exit_pricing.price_side: "same"`, `use_order_book: true`, `order_book_top: 1`. Die Order liegt am besten Geld- bzw. Briefkurs und wird normalerweise als Maker gefüllt. Läuft der Kurs vorher durch, wird sie Taker (0,25 %). Der Ledger protokolliert das reale Maker/Taker-Flag je Fill. Deshalb stehen beide Gebührenstufen im Backtest.
- Protections: Freqtrade 2026.8 bricht mit `ConfigurationError` ab, wenn `protections` in `config.json` steht. Die drei Protections aus `docs/KOMPONENTEN.md` Abschnitt 4 stehen deshalb als `protections`-Property in `BtcTrend.py`. Im Backtest wirken sie nur mit `--enable-protections`; das Skript setzt den Schalter (`PROTECTIONS=0` schaltet ab).
- Mindesthaltedauer: Freqtrade ruft `custom_exit` nur auf, wenn kein Exit-Signal gesetzt ist. Ein Veto gegen Signal-Exits ist nur in `confirm_trade_exit` möglich. Dort sitzt die Prüfung; Stoploss, Force-Exit und Emergency-Exit werden nie blockiert. `custom_exit` gibt in `BtcTrend` immer `None` zurück.
- Kill-Switch und Advisor-Gate greifen nur in den Modi `live` und `dry_run`. Im Backtest würde eine liegen gebliebene `killswitch.lock` sonst still alle Entries verhindern, und eine einzelne aktuelle `decision.json` hat für historische Kerzen keine Bedeutung.
- `lookahead-analysis` erzwingt Marktorders und verlangt dafür `price_side: "other"`. Das Skript setzt das nur für diesen Aufruf per `FREQTRADE__ENTRY_PRICING__PRICE_SIDE` und `FREQTRADE__EXIT_PRICING__PRICE_SIDE`. `recursive-analysis` kennt kein `--fee`.
- `lookahead-analysis` bricht in 2026.8 mit `pandas.errors.LossySetitemError` ab, wenn die CSV-Datei `lookahead-<Strategie>.csv` schon eine Zeile für die Strategie enthält (die Analyse selbst ist dann bereits durchgelaufen). Das Skript löscht die alte Datei deshalb vor dem Aufruf.
- Rebalance-Verkäufe: Freqtrade interpretiert einen negativen Rückgabewert von `adjust_trade_position` relativ zu `trade.stake_amount`, nicht als EUR-Wert zum aktuellen Kurs. Die Strategie rechnet um (siehe Abschnitt "Korrektur der Rebalance-Verkäufe"). Eine Restposition, die unter dem Börsenminimum inklusive Stoploss-Reserve läge, wird komplett verkauft. Kill-Switch und Advisor-Gate blocken auch Rebalance-Käufe, weil Freqtrade `confirm_trade_entry` nur beim ersten Einstieg aufruft. Ohne bekannte realisierte Volatilität gibt es weder Einstieg noch Rebalance (kein Rückfall auf 100 % Exposure).
- `--export-filename` ist in 2026.8 für Backtests veraltet. Das Skript nutzt `--backtest-directory` und `--notes`, Ergebnisse landen als `backtest-result-<Zeit>.zip` in `user_data/backtest_results/` (per `.gitignore` ausgeschlossen).
- Die Bitvavo-Daten beginnen am 08.03.2019. Für den Zeitraum ab 2017 braucht es die Kraken-Kerzen aus `docs/PROJEKTPLAN.md` Abschnitt 6.5.
- Die Hilfsfunktionen der Strategie liegen in `user_data/strategies/btctrend_lib.py`. Freqtrade legt das Strategieverzeichnis beim Laden auf `sys.path` (`freqtrade/resolvers/iresolver.py`, `PathModifier`), deshalb reicht `import btctrend_lib`. Die Tests in `tests/strategy/` laden das Modul per Pfad und laufen ohne Freqtrade; die Klassentests brauchen das Freqtrade-venv und überspringen sich sonst.
