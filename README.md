# BitCoin-Trader

Automatisierter, gebührenbewusster Bitcoin-Spot-Handel (BTC/EUR) auf einem Proxmox-LXC-Container, mit Depot-Übersicht und Claude als Strategie-, Code- und Review-Partner.

## Status

Planungsphase. Es gibt noch keinen Code. Der vollständige, quellenbelegte Projektplan liegt in [docs/PROJEKTPLAN.md](docs/PROJEKTPLAN.md) (Stand 15.09.2026).

## Die vier Ausgangsfragen in Kürze

| Frage | Antwort (Details im Projektplan) |
|---|---|
| Ist das legal? | Ja. Handel mit ausschließlich eigenem Geld auf eigene Rechnung ist keine erlaubnispflichtige Kryptowerte-Dienstleistung (BaFin-Merkblatt zu MiCAR, 03.01.2025). Bedingungen: nur eigenes Kapital, keine Dienste für Dritte, keine manipulativen Ordermuster (Art. 91 MiCA). Abschnitt 2 |
| Welches Konto? | Privatkonto bei Bitvavo (MiCA-Lizenz, 0,15 % Maker / 0,25 % Taker, SEPA kostenlos, REST + WebSocket, ccxt). API-Key nur mit View + Trade, nie Withdraw. Kraken Pro optional als Zweitkonto. Abschnitt 4 |
| Kann Claude handeln? | Nicht als Live-Entscheider. Ein deterministischer Bot (Freqtrade) setzt Orders und erzwingt Risikogrenzen. Claude entwirft Strategie und Code, analysiert Backtests, reviewt Logs und liefert optional ein langsames Regime-Signal als Gate. Abschnitt 5 |
| Wie umsetzen? | Backtest, dann mindestens 3 Monate Dry-Run, dann 200 bis 500 EUR live, dann skalieren, jeweils mit Go/No-Go-Kriterium. Abschnitt 7, 8, 11 |

## Ehrliche Erwartung

Round-Trip-Kosten von 0,30 bis 0,50 % liegen über der typischen Bewegung von Minuten- und Stundenkerzen. Kurzfristiger Handel ist mit Retail-Gebühren strukturell verlustbringend. Realistisches Ziel ist eine Rendite in der Nähe von Buy-and-Hold mit geringerem Drawdown, gemessen gegen Buy-and-Hold und DCA. Jeder Verkauf innerhalb eines Jahres ist steuerpflichtig (§ 23 EStG, Freigrenze 1.000 EUR). Abschnitt 3 und 6.

## Geplanter Stack

- Unprivilegierter Debian-13-LXC auf Proxmox, ohne Docker
- Freqtrade (Dry-Run, Backtesting, FreqUI, Telegram, REST-API) mit Strategie `BtcTrend` (Trendfilter auf Tageskerzen plus Volatilitäts-Targeting)
- Eigenes Handelsprotokoll (`ledger.py`) mit FIFO-Lots und § 23-Auswertung für die Steuer
- `guard.py` für Tagesverlustlimit, Kill-Switch und Bilanzabgleich
- Tailscale für den Zugriff, Healthchecks/Uptime Kuma als Dead-Man's-Switch, USV am Host

## Nächste Schritte

1. Offene Entscheidungen in Abschnitt 12 des Projektplans beantworten (Börse, Zielkapital, statische IP, Proxmox-Version, USV, Advisor ja/nein).
2. Bitvavo-Konto eröffnen, 2FA, View-only-API-Key für den Dry-Run.
3. LXC anlegen, Freqtrade installieren, Kraken-Historie laden, erste Strategie und Backtest.

Kein Secret gehört ins Repo. API-Keys liegen nur in `/etc/freqtrade/secrets.env` (0600) auf dem Container.
