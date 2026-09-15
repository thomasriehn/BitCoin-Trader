# Komponenten-Spezifikation (Phase 0)

Stand: 15.09.2026. Diese Datei ist der verbindliche Vertrag zwischen den Bauteilen. Änderungen an Pfaden, Umgebungsvariablen, DB-Schema oder Dateiformaten werden hier zuerst eingetragen.

Entscheidungen von Thomas (15.09.2026): Börse Bitvavo, Startkapital 1.000 EUR, statische öffentliche IP vorhanden, Proxmox VE 9.2.10 mit ZFS, USV vorhanden, Advisor lokal über vLLM (kein Cloud-LLM im Live-Pfad).

## 1. Repository-Layout

```
BitCoin-Trader/
  pyproject.toml                 Python-Paket "btctrader" (eigene Dienste), Dev-Deps (pytest, respx, ruff)
  README.md
  docs/
    PROJEKTPLAN.md               Recherche und Plan (Abschnitt 12 enthält die getroffenen Entscheidungen)
    KOMPONENTEN.md               diese Datei
    SETUP.md                     Schritt-für-Schritt: Proxmox-Host, LXC, Bitvavo-Keys, Tailscale, vLLM-Host, Dry-Run starten
    VLLM_ADVISOR.md              Modellwahl, vLLM-Start, Prompt, Schattenmodus, Auswertung
    BETRIEB.md                   Runbook: Alarme, Kill-Switch zurücksetzen, Updates, Backups, Steuerreport
  deploy/
    proxmox/create-lxc.sh        läuft auf dem PVE-Host: pct create (unprivilegiert, ZFS, onboot, startup order, /dev/net/tun)
    proxmox/firewall/210.fw      Beispiel für /etc/pve/firewall/<CTID>.fw (Default-Deny, Allowlist)
    proxmox/nut/README.md        USV-Anbindung des Hosts (NUT, upsmon, SHUTDOWNCMD)
    container/install.sh         läuft im CT: Pakete, Nutzer freqtrade, /opt/freqtrade (Version gepinnt), /srv/trading, venvs, Units, journald
    container/sync-config.sh     kopiert user_data/config.json aus dem Repo nach /srv/trading/user_data/ (Secrets bleiben außerhalb)
    container/systemd/*.service  freqtrade-dryrun, freqtrade, btctrader-advisor, btctrader-ledger, btctrader-guard, btctrader-heartbeat, btctrader-dashboard
    container/systemd/*.timer    advisor (stündlich), ledger (täglich 00:20 UTC), guard (jede Minute), heartbeat (jede Minute)
    container/env/*.example      Vorlagen für /etc/freqtrade/secrets-dryrun.env, secrets.env, btctrader.env
    vllm/docker-compose.yml      vLLM auf dem GPU-Host (OpenAI-kompatible API), Modell per .env
    vllm/README.md               Hardware-Anforderungen, Modell-Empfehlungen, Test-Aufruf
  user_data/                     Freqtrade-Userdir-Teile, die ins Repo gehören (ohne Daten, Logs, DBs, Secrets)
    config.json                  öffentliche Konfiguration (dry_run true, Bitvavo, BTC/EUR, api_server, protections, Platzhalter-Telegram)
    config-private.example.json  Vorlage für Keys, Telegram-Token, jwt_secret (echte Datei nur im CT, 0600)
    strategies/BtcTrend.py       Startstrategie (Tageskerzen, SMA200-Trendfilter mit Hysterese, Vol-Targeting, Rebalance-Band)
    strategies/BtcAdvisorGated.py  Variante, die decision.json als Entry-Gate liest
  btctrader/                     eigene Dienste (Python 3.11, keine Freqtrade-Importe)
    common/                      config.py (Env-Variablen), ftapi.py (Freqtrade-REST-Client), bitvavo_public.py (Kerzen, Ticker ohne Key), alerts.py (Telegram, ntfy), db.py (SQLite, Schema, WAL)
    advisor/                     cli.py, advisor.py, context.py (Marktkontext), schema.py (Pydantic), prompts/system.md
    ledger/                      cli.py, sync.py (Fills holen), fifo.py (Lots, Disposals, § 23), benchmarks.py (B&H, virtuelles DCA), export.py (CSV + SHA-256, Jahresreport)
    guard/                       cli.py, guard.py (Tagesverlust, Drawdown-Kill-Switch, Bilanzabgleich, optional cancelOrdersAfter), heartbeat.py
    dashboard/                   cli.py, app.py (FastAPI), templates/index.html, static/
  scripts/
    download-data.sh             Bitvavo-Kerzen 1d/4h/1h ab 2019 laden (Freqtrade download-data)
    backtest.sh                  Backtest BtcTrend mit Bitvavo-Gebühren und Slippage, plus lookahead-analysis
  tests/                         pytest; ein Modul je Komponente, Freqtrade-abhängige Tests nur in tests/strategy/
```

Regeln: keine Secrets im Repo (`.gitignore` deckt `*.env`, `config-private.json`, `*.sqlite`). Kein Bauteil außer `ledger` schreibt in `ledger.sqlite`. Die Strategiedateien importieren nichts aus `btctrader` (Freqtrade läuft in einem eigenen venv).

## 2. Laufzeitpfade im Container

| Pfad | Inhalt | Owner / Rechte |
|---|---|---|
| `/opt/freqtrade/` | Freqtrade-Checkout (Tag `2026.8`) und `.venv` | freqtrade |
| `/srv/trading/repo/` | Git-Checkout dieses Repos | freqtrade |
| `/srv/trading/venv/` | venv mit `pip install -e /srv/trading/repo` (btctrader) | freqtrade |
| `/srv/trading/user_data/` | Freqtrade-Userdir: `config.json` (Kopie aus Repo), `config-private.json` (0600), `strategies` (Symlink auf `repo/user_data/strategies`), `data/`, `logs/`, `backtest_results/`, `tradesv3.dryrun.sqlite`, `tradesv3.sqlite` | freqtrade |
| `/srv/trading/advisor/` | `decision.json`, `decisions.jsonl` (10 Jahre aufbewahren), `context-latest.json` | freqtrade |
| `/srv/trading/ledger/` | `ledger.sqlite`, `exports/YYYY-MM-bitvavo-fills.csv` (+ `.sha256`), `reports/steuer-YYYY.csv` | freqtrade |
| `/srv/trading/guard/` | `state.json` (Tagesstart-Equity, Equity-Hoch), `killswitch.lock`, `events.jsonl` | freqtrade |
| `/etc/freqtrade/secrets-dryrun.env` | View-only-Key für den Dry-Run-Bot | root:freqtrade 0640 |
| `/etc/freqtrade/secrets.env` | Trade-Key (erst Phase 4) | root:freqtrade 0640 |
| `/etc/freqtrade/btctrader.env` | Konfiguration der eigenen Dienste (Abschnitt 3) | root:freqtrade 0640 |

Freqtrade-Dry-Run: `freqtrade-dryrun.service` mit `--config config.json --config config-private.json`, `db_url sqlite:////srv/trading/user_data/tradesv3.dryrun.sqlite`, API-Port 8080. Live später als `freqtrade.service` mit eigener DB und Port 8081; nie beide gegen dasselbe Börsenkonto mit Trade-Rechten.

## 3. Umgebungsvariablen (`/etc/freqtrade/btctrader.env`)

Alle eigenen Dienste lesen ausschließlich diese Variablen (Modul `btctrader.common.config`, Pydantic-Settings-artig, mit Defaults). Fehlende Pflichtwerte führen zu einem klaren Fehler beim Start.

| Variable | Default | Bedeutung |
|---|---|---|
| `FT_API_URL` | `http://127.0.0.1:8080` | Freqtrade-REST-API des aktiven Bots |
| `FT_API_USER`, `FT_API_PASS` | Pflicht | Basic-Auth der Freqtrade-API (`api_server.username/password`) |
| `FT_DB_PATH` | `/srv/trading/user_data/tradesv3.dryrun.sqlite` | Freqtrade-DB für Abgleich und Dry-Run-Fills |
| `BITVAVO_API_KEY_RO`, `BITVAVO_API_SECRET_RO` | leer | View-only-Key für Ledger (Fills, Bilanz) und Guard (Bilanzabgleich). Leer = nur Dry-Run-Quelle |
| `BITVAVO_OPERATOR_ID` | `1` | MiCA `operatorId`, wird bei ccxt-Aufrufen gesetzt |
| `LEDGER_DB_PATH` | `/srv/trading/ledger/ledger.sqlite` | eigene DB (Abschnitt 5) |
| `LEDGER_ACCOUNT_ID` | `bitvavo-main` | Wallet-Kennung für die walletbezogene FIFO |
| `LEDGER_SOURCE` | `freqtrade-db` | `freqtrade-db` (Dry-Run) oder `exchange` (Live, braucht RO-Key) |
| `LEDGER_EXPORT_DIR` | `/srv/trading/ledger/exports` | Monats-CSVs |
| `START_CAPITAL_EUR` | `1000` | Startkapital für Benchmarks |
| `BENCHMARK_START` | Pflicht, ISO-Datum | Tag 0 für B&H und DCA |
| `DCA_WEEKS` | `52` | virtuelles DCA verteilt `START_CAPITAL_EUR` über so viele Wochen |
| `FEE_MAKER`, `FEE_TAKER` | `0.0015`, `0.0025` | Bitvavo Kategorie A Stufe 0; Ledger liest reale Gebühren aus den Fills, Benchmarks nutzen diese Werte |
| `ADVISOR_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI-kompatible vLLM-API |
| `ADVISOR_MODEL` | Pflicht | Modellname wie in vLLM gestartet |
| `ADVISOR_API_KEY` | leer | falls vLLM mit `--api-key` läuft |
| `ADVISOR_MODE` | `shadow` | `shadow` (nur loggen) oder `gate` (Strategie darf Entries blocken) |
| `ADVISOR_INTERVAL_HOURS` | `1` | erwarteter Abstand der Aufrufe; `valid_until` = Aufruf + 2 x Intervall |
| `ADVISOR_DIR` | `/srv/trading/advisor` | Ablage von `decision.json`, `decisions.jsonl` |
| `ADVISOR_TIMEOUT_S` | `120` | HTTP-Timeout |
| `GUARD_DIR` | `/srv/trading/guard` | `state.json`, `killswitch.lock`, `events.jsonl` |
| `GUARD_DAILY_LOSS_PCT` | `3.0` | Tagesverlust in % des Tagesstart-Equity, ab dem `stopentry` ausgelöst wird |
| `GUARD_MAX_DRAWDOWN_PCT` | `20.0` | Drawdown vom Equity-Hoch, ab dem alles glattgestellt und der Bot gestoppt wird |
| `GUARD_RECONCILE_TOL_EUR` | `2.0` | erlaubte Abweichung Börsen-Bilanz vs. Freqtrade-Bilanz |
| `GUARD_COD_ENABLED` | `false` | börsenseitiger Dead-Man's-Switch (`cancelOrdersAfter`) erneuern |
| `GUARD_COD_SECONDS` | `120` | Countdown für `cancelOrdersAfter` |
| `HEALTHCHECKS_URL` | leer | Ping-URL (Healthchecks.io oder self-hosted); leer = kein Ping |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | leer | Alarme (eigener Bot, nicht der von Freqtrade) |
| `NTFY_URL` | leer | z. B. `https://ntfy.example.com/btc-bot` |
| `DASHBOARD_BIND` | `127.0.0.1:8090` | Bind-Adresse der Ergänzungsseite |
| `TZ_DISPLAY` | `Europe/Berlin` | nur für die Anzeige; alle gespeicherten Zeitstempel sind UTC |

## 4. Freqtrade-Konfiguration (`user_data/config.json`)

Pflichtinhalte:

- `dry_run: true`, `dry_run_wallet: 1000`, `stake_currency: "EUR"`, `stake_amount: "unlimited"`, `tradable_balance_ratio: 0.99`, `max_open_trades: 1`, `timeframe` wird von der Strategie gesetzt (`1d`).
- `exchange.name: "bitvavo"`, `pair_whitelist: ["BTC/EUR"]`, `ccxt_config.options.operatorId` (Integer), `ccxt_async_config.enableRateLimit: true`. Keys nur in `config-private.json`.
- Order-Typen: `entry: limit`, `exit: limit`, `stoploss: market`, `stoploss_on_exchange: false` (Bitvavo wird von Freqtrade nicht unterstützt). `order_time_in_force`: Post-only (`PO`) nur, wenn Freqtrade/ccxt es für Bitvavo unterstützt; sonst `GTC` mit `entry_pricing.price_side: "same"` und `use_order_book: true`, dokumentiert im Strategiekopf.
- `unfilledtimeout: {entry: 120, exit: 120, unit: "minutes"}` (Tageskerzen, Maker-Orders dürfen liegen).
- `protections`: `CooldownPeriod` (2 Kerzen), `StoplossGuard` (2 Stops in 30 Kerzen, 10 Kerzen Pause), `MaxDrawdown` (10 Kerzen Pause bei 10 % über 30 Kerzen).
- `api_server`: `enabled: true`, `listen_ip_address: "127.0.0.1"`, `listen_port: 8080`, `jwt_secret_key`, `username`, `password`, `ws_token` in `config-private.json`.
- `telegram`: `enabled: false` im Repo; Token und Chat-ID in `config-private.json`.
- `fiat_display_currency: "EUR"`, `internals.sd_notify: true`, `internals.process_throttle_secs: 5`, `db_url` in der Unit per `--db-url`.
- `dataformat_ohlcv: "feather"`, `pairlists: [{"method": "StaticPairList"}]`.

## 5. Ledger-DB (`ledger.sqlite`, WAL, nur `btctrader.ledger` schreibt)

```sql
CREATE TABLE fills (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                 -- 'exchange' | 'freqtrade-db'
  exchange TEXT NOT NULL,               -- 'bitvavo'
  account_id TEXT NOT NULL,             -- LEDGER_ACCOUNT_ID
  exchange_trade_id TEXT NOT NULL,      -- Bitvavo Fill-ID; im Dry-Run 'ft-<order_id>-<n>'
  exchange_order_id TEXT,
  client_order_id TEXT,
  ts_utc TEXT NOT NULL,                 -- ISO-8601 UTC, Fill-Zeitstempel der Börse
  pair TEXT NOT NULL,                   -- 'BTC/EUR'
  side TEXT NOT NULL,                   -- 'buy' | 'sell'
  amount_btc TEXT NOT NULL,             -- Decimal als String, 8 Nachkommastellen
  price_eur TEXT NOT NULL,
  gross_eur TEXT NOT NULL,              -- amount x price
  fee_amount TEXT NOT NULL,
  fee_currency TEXT NOT NULL,           -- 'EUR' | 'BTC'
  fee_eur TEXT NOT NULL,                -- Gebühr in EUR (bei BTC-Gebühr: fee_amount x price)
  net_eur TEXT NOT NULL,                -- buy: gross + fee_eur ; sell: gross - fee_eur
  maker_taker TEXT,                     -- 'maker' | 'taker' | NULL
  strategy TEXT,
  signal_reason TEXT,                   -- Freqtrade enter_tag / exit_reason
  advisor_decision_id TEXT,
  dry_run INTEGER NOT NULL,             -- 1 im Dry-Run
  reconciled_with_bot_db INTEGER NOT NULL DEFAULT 0,
  prev_hash TEXT, row_hash TEXT NOT NULL,   -- SHA-256 Kette über die fachlichen Felder
  UNIQUE (exchange, account_id, exchange_trade_id)
);
CREATE TABLE lots (
  lot_id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  buy_fill_id INTEGER NOT NULL REFERENCES fills(id),
  acquired_at TEXT NOT NULL,
  qty_btc TEXT NOT NULL,
  cost_eur_incl_fees TEXT NOT NULL,     -- Anschaffungskosten inkl. Kaufgebühr (BMF Rn. 59)
  remaining_qty TEXT NOT NULL,
  pre_2027 INTEGER NOT NULL             -- acquired_at < 2027-01-01
);
CREATE TABLE disposals (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  sell_fill_id INTEGER NOT NULL REFERENCES fills(id),   -- Verkaufs-Fill oder Fill mit BTC-Gebühr
  kind TEXT NOT NULL,                   -- 'sell' | 'fee'  (fee: Mini-Veräußerung bei Gebühr in BTC)
  lot_id INTEGER NOT NULL REFERENCES lots(lot_id),
  qty_btc TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  disposed_at TEXT NOT NULL,
  holding_days INTEGER NOT NULL,        -- informativ
  proceeds_eur TEXT NOT NULL,           -- anteiliger Erlös (ohne Verkaufsgebühr)
  cost_eur TEXT NOT NULL,               -- anteilige Anschaffungskosten inkl. Kaufgebühr
  sell_fee_eur TEXT NOT NULL,           -- anteilige Verkaufsgebühr (Werbungskosten)
  gain_eur TEXT NOT NULL,               -- proceeds - cost - sell_fee
  taxable INTEGER NOT NULL,             -- disposed_date <= acquired_date + 1 Jahr (Kalenderregel)
  boundary_case INTEGER NOT NULL        -- 1, wenn disposed_date == acquired_date + 1 Jahr (Jahrestag)
);
CREATE TABLE transfers (id INTEGER PRIMARY KEY, account_id TEXT, ts_utc TEXT, direction TEXT, asset TEXT, amount TEXT, tx_hash TEXT, note TEXT);
CREATE TABLE equity_daily (
  date_utc TEXT PRIMARY KEY,            -- 'YYYY-MM-DD'
  eur_balance TEXT NOT NULL, btc_balance TEXT NOT NULL, btc_price_eur TEXT NOT NULL,
  equity_eur TEXT NOT NULL,             -- eur + btc x price
  bh_equity_eur TEXT NOT NULL,          -- Buy-and-Hold-Benchmark
  dca_equity_eur TEXT NOT NULL,         -- virtuelles wöchentliches DCA
  fees_cum_eur TEXT NOT NULL,
  realized_gain_ytd_eur TEXT NOT NULL,  -- § 23-Gewinn (steuerbar) im laufenden Jahr
  dry_run INTEGER NOT NULL
);
CREATE TABLE expenses (id INTEGER PRIMARY KEY, date_utc TEXT, amount_eur TEXT, description TEXT, receipt_ref TEXT);
CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT);   -- z. B. last_fill_ts
```

Beträge werden als Decimal-Strings gespeichert und mit `decimal.Decimal` gerechnet (kein float). FIFO wird bei jedem `sync` deterministisch aus allen Fills des Kontos neu aufgebaut (Tabellen `lots`, `disposals` werden geleert und neu befüllt), Reihenfolge nach `ts_utc`, dann `exchange_trade_id`.

Steuerregel (§ 23 Abs. 1 Nr. 2 EStG, BMF 06.03.2025): `taxable = disposed_date <= acquired_date + relativedelta(years=1)` auf Kalenderdaten in UTC. Verkaufsgebühren sind Werbungskosten, Kaufgebühren Anschaffungsnebenkosten. Gebühr in BTC: `kind='fee'`-Disposal mit `proceeds = fee_btc x price` gegen die ältesten Lots.

Benchmarks: `bh_equity = START_CAPITAL_EUR / price(BENCHMARK_START) x price(date)` abzüglich einer Taker-Gebühr beim fiktiven Kauf. `dca_equity`: jede Woche ab `BENCHMARK_START` für `DCA_WEEKS` Wochen ein fiktiver Kauf von `START_CAPITAL_EUR / DCA_WEEKS` zum Tagesschlusskurs mit Taker-Gebühr; nicht investierter Rest bleibt als EUR stehen.

## 6. Advisor (lokales vLLM)

- Aufruf: `btctrader-advisor run` (systemd-Timer, stündlich bei Minute 7). `btctrader-advisor context` gibt nur den Marktkontext aus, `btctrader-advisor evaluate --since 2026-09-01` vergleicht geloggte Regime mit der Folge-Rendite.
- Marktkontext (`context.py`, nur öffentliche Bitvavo-API, keine Keys): letzte 30 Tageskerzen und 24 4h-Kerzen BTC-EUR, Schlusskurs, SMA50/SMA200 und Abstand in %, realisierte Volatilität 30 Tage (annualisiert), Returns 1/7/30/90 Tage, Drawdown vom 365-Tage-Hoch, Volumen-Trend; optional Bot-Status aus der Freqtrade-API (Position ja/nein, unrealisierter Gewinn). Keine Nachrichten, keine externen Feeds. Kontext wird als `context-latest.json` gespeichert.
- HTTP: `POST {ADVISOR_BASE_URL}/chat/completions` mit `model`, `messages` (System-Prompt aus `prompts/system.md` zuerst, dann Kontext als JSON), `temperature: 0`, `seed: 42`, `max_tokens: 400`, `response_format: {"type": "json_schema", "json_schema": {"name": "regime_decision", "schema": <Schema>, "strict": true}}`. Fällt der Server mit 400 auf `response_format`, Retry mit `extra_body.guided_json` (ältere vLLM-Versionen). Antwort wird mit Pydantic validiert; Wertebereiche werden in Code geprüft.
- Schema `RegimeDecision`: `regime` in `{"risk_on","neutral","risk_off"}`, `confidence` float 0..1, `horizon_days` int 1..30, `rationale` string max 500 Zeichen, `key_factors` Liste von max 5 Strings.
- `decision.json` (atomar via tmp + `os.replace`):

```json
{"schema_version": 1, "decision_id": "2026-09-15T13:07:02Z-a1b2c3", "created_at": "2026-09-15T13:07:02Z",
 "valid_until": "2026-09-15T15:07:02Z", "mode": "shadow", "model": "Qwen/Qwen3-14B",
 "regime": "neutral", "confidence": 0.55, "horizon_days": 7, "rationale": "...", "key_factors": ["..."],
 "context_hash": "sha256:...", "prompt_hash": "sha256:..."}
```

- `decisions.jsonl`: eine Zeile pro Aufruf mit allen Feldern aus `decision.json` plus `latency_ms`, `usage` (prompt/completion tokens), `raw_response` (gekürzt auf 4.000 Zeichen), `error` (falls Aufruf oder Validierung scheiterte; dann wird `decision.json` nicht überschrieben).
- Fehlerverhalten: Server nicht erreichbar, Timeout, ungültiges JSON: Exit-Code 1, Log-Zeile mit `error`, `decision.json` bleibt unverändert. Kein Alarm bei Einzelfehler; der Guard alarmiert, wenn `decision.json` älter als 3 x Intervall ist und `ADVISOR_MODE=gate`.
- Der Advisor hat keine Börsen-Keys und ruft nie Order-Endpunkte auf.

## 7. Strategien

`BtcTrend` (Freqtrade `IStrategy`, `INTERFACE_VERSION = 3`):

- `timeframe = "1d"`, `can_short = False`, `process_only_new_candles = True`, `startup_candle_count = 210`, `stoploss = -0.10` (Katastrophenstop, bot-seitig), `minimal_roi = {"0": 100}` (praktisch aus), `use_exit_signal = True`, `exit_profit_only = False`, `position_adjustment_enable = True`, `max_entry_position_adjustment = 10`.
- Parameter als Klassenattribute (hyperoptbar, mit Defaults): `sma_period = 200`, `band_pct = 0.03` (Hysterese), `min_hold_candles = 5`, `target_vol = 0.35`, `vol_lookback = 30`, `rebalance_band = 0.15` (Exposure-Drift in Anteilen), `min_order_pct = 0.02` (Mindestordergröße als Anteil des Kapitals), `min_order_eur = 5.0` (Börsenminimum; zur Laufzeit aus `self.dp.market('BTC/EUR')['limits']['cost']['min']` gelesen, Fallback 5.0).
- Signale: `enter_long` wenn `close > sma * (1 + band_pct)`; `exit_long` wenn `close < sma * (1 - band_pct)`. `custom_exit` blockt Signal-Exits (nicht Stoploss) vor `min_hold_candles` Kerzen. `enter_tag = "trend_up"`, `exit_reason` bleibt Freqtrade-Standard.
- Positionsgröße: `custom_stake_amount` = Kapital (Wallet EUR + Positionswert) x `exposure`, `exposure = min(1, target_vol / realized_vol)`, `realized_vol` = Std der Tagesrenditen über `vol_lookback` x sqrt(365). `adjust_trade_position` rebalanciert (positiv = nachkaufen, negativ = teilverkaufen), wenn `|ist_exposure - soll_exposure| > rebalance_band` und der Orderwert >= `max(min_order_eur, min_order_pct x Kapital)`.
- `confirm_trade_entry` / `confirm_trade_exit`: reine Prüfungen ohne Netzwerkaufrufe (Mindestordergröße, kein Entry, wenn `killswitch.lock` existiert; Pfad `GUARD_DIR` aus Env oder Default `/srv/trading/guard`).
- Alle Berechnungen in `populate_indicators` müssen `lookahead-analysis` und `recursive-analysis` bestehen.
- Kommentar im Kopf: welche Order-Einstellungen (`order_types`, `order_time_in_force`, Pricing) für Maker-Ausführung bei Bitvavo gelten und warum.

`BtcAdvisorGated(BtcTrend)`:

- `bot_loop_start` liest `ADVISOR_DIR/decision.json` (Pfad aus Env, Default `/srv/trading/advisor`), prüft `schema_version`, `valid_until` und `mode`. Nur wenn `mode == "gate"` und die Datei gültig ist, gilt: `regime == "risk_off"` blockt neue Entries in `confirm_trade_entry`; `regime == "risk_on"` ändert nichts; `confidence` wird geloggt, nicht verwendet. Fehlende oder abgelaufene Datei: Verhalten wie `BtcTrend`, eine Warnung pro Stunde.
- Kein Netzwerkaufruf in der Strategie. Exits werden nie durch den Advisor blockiert.

## 8. Guard und Heartbeat

`btctrader-guard check` (Timer jede Minute):

1. Holt `/api/v1/health`, `/api/v1/balance`, `/api/v1/status`, `/api/v1/show_config` (für `dry_run`) von Freqtrade. Equity = `balance.total` in EUR (Freqtrade rechnet BTC zum Kurs um).
2. Tagesstart: beim ersten Lauf nach 00:00 UTC wird `day_start_equity` in `state.json` gesetzt. Fällt Equity um >= `GUARD_DAILY_LOSS_PCT` unter `day_start_equity`: `POST /api/v1/stopentry`, Alarm, Event `daily_loss`. Einmal pro Tag.
3. Equity-Hoch `peak_equity` fortschreiben. Drawdown >= `GUARD_MAX_DRAWDOWN_PCT`: `POST /api/v1/forceexit` mit `tradeid: "all"`, dann `POST /api/v1/stopentry`, `killswitch.lock` schreiben (Inhalt: Zeit, Equity, Peak), Alarm, Event `killswitch`. Solange die Lock-Datei existiert, wird bei jedem Lauf erneut `stopentry` gesetzt (falls jemand `/start` sendet). Reset nur manuell: `btctrader-guard reset --confirm`.
4. Bilanzabgleich (nur wenn `dry_run == false` und RO-Key vorhanden): ccxt `fetch_balance` (EUR, BTC) vs. Freqtrade `/balance`. Abweichung > `GUARD_RECONCILE_TOL_EUR` an zwei aufeinanderfolgenden Läufen: `stopentry`, Alarm, Event `reconcile_mismatch`.
5. Advisor-Frische (nur wenn `ADVISOR_MODE=gate`): `decision.json` älter als 3 x `ADVISOR_INTERVAL_HOURS`: Alarm einmal pro 6 Stunden.
6. Optional `GUARD_COD_ENABLED=true`: ccxt `cancel_all_orders_after(GUARD_COD_SECONDS * 1000)` erneuern (braucht Trade-Key; Default aus).
7. Freqtrade nicht erreichbar: Event `ft_unreachable`, Alarm nach 3 aufeinanderfolgenden Fehlläufen.

Alle Events als Zeile in `events.jsonl` (`ts`, `event`, `details`). Alarme über Telegram und ntfy (`common/alerts.py`), beide optional, Fehler beim Senden werden geloggt und ignoriert.

`btctrader-guard heartbeat` (Timer jede Minute): `GET /api/v1/health`; wenn `last_process` jünger als 90 s und keine `killswitch.lock`: `GET HEALTHCHECKS_URL`; sonst `GET HEALTHCHECKS_URL/fail`. Ohne `HEALTHCHECKS_URL` nur Log.

## 9. Freqtrade-REST-Client (`btctrader.common.ftapi`)

`FreqtradeClient(base_url, username, password)`: holt ein JWT über `POST /api/v1/token/login` (Basic-Auth), erneuert bei 401. Methoden: `ping()`, `health()`, `balance()`, `status()`, `profit()`, `show_config()`, `trades(limit)`, `stopentry()`, `start()`, `forceexit(tradeid="all")`, `daily(days)`. Timeouts 10 s, kein Retry außer Token-Refresh. Wird von Guard, Ledger (Dry-Run-Fills alternativ direkt aus `FT_DB_PATH`) und Dashboard genutzt.

Dry-Run-Fills aus der Freqtrade-DB: Tabelle `orders` (Spalten u. a. `ft_trade_id`, `ft_order_side`, `order_id`, `status`, `filled`, `average`, `cost`, `order_filled_date`, `ft_fee_base`, `ft_fee_quote`?) und `trades` (`fee_open`, `fee_close`, `enter_tag`, `exit_reason`, `strategy`). Der Ledger erzeugt pro gefüllter Order einen Fill mit `exchange_trade_id = "ft-<order_id>"`, Gebühr = `filled x average x fee_open|fee_close` in EUR, `maker_taker` NULL, `source = "freqtrade-db"`, `dry_run = 1`. Die genauen Spaltennamen sind gegen `freqtrade/persistence/trade_model.py` der Version 2026.8 zu prüfen.

## 10. Dashboard-Ergänzung

FastAPI-App `btctrader.dashboard.app:app`, `btctrader-dashboard` startet uvicorn auf `DASHBOARD_BIND`. Kein Login (nur über Tailscale erreichbar), keine Börsen-Keys im Prozess, nur Lesezugriff auf `ledger.sqlite`, `decisions.jsonl`, `state.json`, `events.jsonl` und die Freqtrade-API.

Endpunkte: `GET /` (HTML), `GET /api/equity?days=365` (Reihen aus `equity_daily`: equity, bh, dca, drawdown), `GET /api/fees` (kumuliert, letzte 30 Tage, Maker-Anteil), `GET /api/tax?year=2026` (steuerbarer § 23-Gewinn YTD nach Werbungskosten, Gebühren YTD, Abstand zur Freigrenze 1.000 EUR, offene Lots mit `frei_ab`-Datum, Anzahl Fills), `GET /api/status` (Freqtrade health/profit/status, Guard-State, Kill-Switch, letzte Advisor-Entscheidung, letzte 20 Events), `GET /api/decisions?limit=50`, `GET /healthz`.

Seite: Kacheln (Equity, Überrendite vs. B&H und DCA, Max-Drawdown Bot vs. B&H, Gebühren YTD, § 23-Gewinn YTD und Abstand zur Freigrenze, Bot-Status, Advisor-Regime), Equity-Chart (Bot, B&H, DCA), Drawdown-Chart, Fills-Tabelle, Events-Liste. htmx-Polling alle 30 s, Chart.js aus `static/vendor/` (von `install.sh` heruntergeladen), Fallback auf jsDelivr-CDN, wenn die Datei fehlt. Mobil lesbar (eine Spalte unter 700 px), hell/dunkel nach Systemeinstellung.

## 11. Tests und Qualität

- `tests/` mit pytest; Ziel: jede Regel aus dieser Datei hat einen Test. Mindestens: FIFO inkl. Teilverbrauch und Gebühr in BTC; Jahresfrist inkl. Schaltjahr und Jahrestag (`boundary_case`); Benchmarks; Hash-Kette; Guard-Zustandsmaschine (Tagesverlust, Kill-Switch, Lock hält `stopentry`, Reset); Advisor-Schema, Fallback auf `guided_json`, atomares Schreiben, Fehlerfall lässt `decision.json` unverändert; Strategie-Hilfsfunktionen (Hysterese, Exposure, Rebalance-Schwelle in EUR); Dashboard-Endpunkte gegen eine Test-DB.
- HTTP wird mit `respx` gemockt. Keine Tests gegen echte Börsen-Keys.
- `ruff check .` ohne Befunde. Typannotationen in allen eigenen Modulen.
- Backtest der Strategie mit Bitvavo-Tageskerzen ab 2019, `--fee 0.0015`, Vergleich mit Buy-and-Hold, `lookahead-analysis` und `recursive-analysis` bestanden; Ergebnis als `docs/BACKTEST.md` mit Kommando, Zeitraum, Kennzahlen und ehrlicher Einordnung.
