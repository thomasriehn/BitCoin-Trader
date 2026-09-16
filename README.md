# BitCoin-Trader

Automatisierter, gebührenbewusster Bitcoin-Spot-Handel (BTC/EUR bei Bitvavo) auf einem Proxmox-LXC-Container, mit steuerfähigem Handelsprotokoll, Risikowächter, lokalem vLLM-Advisor und Depot-Übersicht. Claude wirkt als Strategie-, Code- und Review-Partner, nicht als Live-Entscheider.

## Status

Phase 0 ist gebaut: Alle Bauteile sind implementiert, getestet und gegengeprüft. Es lief noch kein Dry-Run auf echter Hardware. Nächster Schritt ist die Einrichtung nach [docs/SETUP.md](docs/SETUP.md).

Entscheidungen (15.09.2026): Börse Bitvavo, Startkapital 1.000 EUR, statische IP, Proxmox VE 9.2.10 mit ZFS, USV vorhanden, Advisor lokal über vLLM.

## Dokumente

| Datei | Inhalt |
|---|---|
| [docs/PROJEKTPLAN.md](docs/PROJEKTPLAN.md) | Recherche und Plan: Rechtslage, Steuern, Börsenvergleich, Strategie, Architektur, Risiken, Phasen |
| [docs/KOMPONENTEN.md](docs/KOMPONENTEN.md) | Verbindlicher Vertrag: Pfade, Umgebungsvariablen, DB-Schema, Schnittstellen, Nachträge |
| [docs/SETUP.md](docs/SETUP.md) | Schritt für Schritt: Host, Container, Bitvavo-Keys, Env-Dateien, Tailscale, vLLM, Dry-Run |
| [docs/BETRIEB.md](docs/BETRIEB.md) | Runbook: Kontrollen, Alarme, Kill-Switch zurücksetzen, Updates, Steuerreport, Restore |
| [docs/BACKTEST.md](docs/BACKTEST.md) | Backtest 2019 bis 2026 mit Lookahead- und Recursive-Analyse |
| [docs/VLLM_ADVISOR.md](docs/VLLM_ADVISOR.md) | Modellwahl nach VRAM, vLLM-Start, Schattenmodus, Auswertung |

## Aufbau

```
user_data/            Freqtrade: config.json, config-private.example.json, Strategien BtcTrend / BtcAdvisorGated
btctrader/common/     Settings, Freqtrade-REST-Client, Bitvavo-Public-API, Alarme (Telegram, ntfy), SQLite, JSONL
btctrader/ledger/     Fills, SHA-256-Kette, FIFO je Wallet, § 23-EStG-Regel, Benchmarks (B&H, DCA), Exporte, Steuerreport
btctrader/guard/      Tagesverlustlimit, Drawdown-Kill-Switch, Bilanzabgleich, Heartbeat
btctrader/advisor/    Marktkontext, strukturierte Regime-Entscheidung von einem lokalen vLLM-Modell, Schattenmodus
btctrader/dashboard/  FastAPI-Seite: Equity gegen B&H und DCA, Drawdown, Gebühren, Steuer-Panel, Bot-Status
deploy/proxmox/       create-lxc.sh, Firewall-Vorlage, NUT-Anleitung
deploy/container/     install.sh, sync-config.sh, systemd-Units und Timer, Env-Vorlagen
deploy/vllm/          Docker-Compose für den GPU-Host
scripts/              download-data.sh, backtest.sh
tests/                pytest je Komponente plus Integrationstest
```

## Kern-Ergebnisse

| Backtest 04.10.2019 bis 14.09.2026, 1.000 EUR | BtcTrend (Maker 0,15 %) | Buy-and-Hold |
|---|---|---|
| Endkapital | 3.596 EUR | ca. 9.070 EUR |
| CAGR | 20,2 % | 37,4 % |
| Max. Drawdown (tägliche Bilanz) | 40,1 % | 73,6 % |
| Trades | 13 | 1 |

Die Strategie schlägt Buy-and-Hold nicht. Sie halbiert den Drawdown und ist rund 55 % der Zeit investiert. Jeder Verkauf innerhalb eines Jahres ist steuerpflichtig (§ 23 EStG, Freigrenze 1.000 EUR). Details und Einordnung in `docs/BACKTEST.md`.

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/python -m pytest -q                 # 426 Tests, ohne Freqtrade
# Strategie-Tests und Backtest brauchen Freqtrade 2026.8 in einem zweiten venv:
python3 -m venv ~/ft-venv && ~/ft-venv/bin/pip install "freqtrade==2026.8" pytest
~/ft-venv/bin/python -m pytest tests/strategy -q
FT_BIN=~/ft-venv/bin/freqtrade scripts/download-data.sh
FT_BIN=~/ft-venv/bin/freqtrade scripts/backtest.sh
```

## Sicherheitsregeln

- Kein Secret im Repo. Keys liegen nur in `/etc/freqtrade/*.env` und `config-private.json` auf dem Container.
- API-Keys ohne Withdraw-Recht, mit IP-Whitelist. Trade-Key erst in Phase 4.
- FreqUI und Dashboard nur über Tailscale, kein Port-Forward.
- Der Advisor hat keine Börsen-Keys und setzt keine Orders. Risikogrenzen stehen im Code, nicht im Prompt.
