# Betrieb: Runbook

Stand: 15.09.2026. Gilt für den Dry-Run (Phase 2 und 3) und mit den markierten Ergänzungen für Live (Phase 4). Einrichtung: `docs/SETUP.md`. Pfade und Variablen: `docs/KOMPONENTEN.md`.

Alle Kommandos laufen im Container (`pct enter 210`) als root. Die `btctrader-*`-Befehle laufen als Nutzer `freqtrade`, damit Dateien die richtigen Rechte behalten. Kurzform für diese Anleitung:

```bash
alias bt='runuser -u freqtrade -- env PATH=/srv/trading/venv/bin:/usr/bin:/bin'
# Beispiele:
bt btctrader-guard status
bt btctrader-ledger show
```

Die Dienste lesen `/etc/freqtrade/btctrader.env` von selbst (Default in `btctrader.common.config`). Der Alias setzt nur den Nutzer und den Pfad zum venv.

## 1. Tägliche Kontrolle (2 Minuten)

Am besten morgens nach dem Tagesschluss um 00:00 UTC (02:00 MESZ).

| Was | Wie | Erwartung |
|---|---|---|
| Bot lebt | Telegram `/status` an den Freqtrade-Bot, oder FreqUI | Antwort binnen Sekunden, Zustand `running` |
| Kein Alarm | Alarm-Bot (Guard) und Healthchecks ohne neue Nachricht | keine neue Nachricht seit gestern |
| Heartbeat | Healthchecks zeigt "up" | letzter Ping vor unter 2 Minuten |
| Ledger gelaufen | Dashboard: Equity-Kachel hat das heutige Datum; oder `journalctl -u btctrader-ledger --since -12h` | Exit-Code 0, neue Zeile in `equity_daily` |
| Guard-Zustand | `bt btctrader-guard status` | keine `killswitch.lock`, `day_start_equity` von heute, `ft_failures` 0 |

Einmal pro Woche reicht ein Blick auf das Dashboard über Tailscale. Wenn ein Trade stattgefunden hat, prüfe in FreqUI den Fill-Preis gegen die Kerze des Tages.

## 2. Wöchentliche Kontrolle (15 Minuten)

1. Systemzustand:
   ```bash
   systemctl --failed
   systemctl list-timers 'btctrader-*'
   journalctl -p warning --since -7d --no-pager | tail -n 50
   df -h / ; zfs list 2>/dev/null   # auf dem Host: freier Platz im Pool
   ```
2. Neustarts des Bots: `journalctl -u freqtrade-dryrun --since -7d | grep -c 'Starting worker'`. Mehr als 1 bedeutet Watchdog- oder Crash-Neustarts. Ursache im Log suchen.
3. Ledger gegen Freqtrade: `bt btctrader-ledger show` listet die letzten Fills. Die Anzahl muss zur Trade-Liste in FreqUI passen. Bei Abweichung: Abschnitt 4.
4. Advisor: `bt btctrader-advisor evaluate --since <Datum des Dry-Run-Starts>`. Nur lesen, nichts ändern. Fehlerquote in `decisions.jsonl` prüfen: `grep -c '"error": "' /srv/trading/advisor/decisions.jsonl` gegen `wc -l`.
5. Drift zwischen Dry-Run und Backtest: Hat der Bot ein Signal gehandelt, das der Backtest mit denselben Kerzen (`scripts/backtest.sh` mit `TIMERANGE` der letzten Woche) nicht zeigt, oder umgekehrt? Notiere es. Ungeklärter Drift ist ein No-Go für Phase 4.
6. Off-Box-Kopie der Nachweise (`docs/SETUP.md`, Abschnitt 9.2).
7. Host: `pveversion`, `chronyc tracking`, letztes vzdump-Backup erfolgreich (Datacenter, Backup, Log).

## 3. Alarme: Bedeutung und Reaktion

Alarme kommen vom Guard (eigener Telegram-Bot und ntfy), von Freqtrade (Telegram) und von Healthchecks (E-Mail/Push). Die Guard-Events stehen zusätzlich in `/srv/trading/guard/events.jsonl` und im Dashboard.

| Alarm / Event | Bedeutung | Was Du tust |
|---|---|---|
| `daily_loss` | Equity heute >= `GUARD_DAILY_LOSS_PCT` (3 %) unter dem Tagesstart. Guard hat `stopentry` gesetzt: keine neuen Entries, offene Position bleibt und wird von der Strategie verwaltet | Nichts Hastiges. Log lesen, Kurs prüfen. Der Bot bleibt in `stopentry`, bis Du in Telegram `/start` sendest. Sende `/start` frühestens am nächsten Tag |
| `killswitch` | Drawdown vom Equity-Hoch >= `GUARD_MAX_DRAWDOWN_PCT` (20 %). Guard hat `forceexit all` und `stopentry` ausgelöst und `killswitch.lock` geschrieben. Solange die Datei existiert, setzt der Guard jede Minute erneut `stopentry`, auch nach `/start` | Position ist glattgestellt. Ursache verstehen (Markt, Bug, Datenfehler). Reset nur bewusst: Abschnitt 5. Live: Bilanz auf der Börse prüfen, ob der Force-Exit gefüllt wurde |
| `reconcile_mismatch` (nur Live) | Börsen-Bilanz und Freqtrade-Bilanz weichen an zwei Läufen um mehr als `GUARD_RECONCILE_TOL_EUR` ab. `stopentry` gesetzt | In der Bitvavo-App die Bilanz und die letzten Fills ansehen. Häufige Ursache: manuelle Order oder Einzahlung neben dem Bot. Bei unerklärter Differenz: Bot stoppen, Key rotieren (Abschnitt 7), Support kontaktieren |
| `ft_unreachable` | Freqtrade-API antwortet dreimal hintereinander nicht | `systemctl status freqtrade-dryrun`, `journalctl -u freqtrade-dryrun -n 100`. Häufig: Bot startet gerade neu, oder Passwort in `btctrader.env` passt nicht zu `config-private.json` |
| `ft_recovered` | API wieder da | nichts |
| `advisor_stale` (nur `ADVISOR_MODE=gate`) | `decision.json` älter als 3 x `ADVISOR_INTERVAL_HOURS` | vLLM-Host prüfen (`curl http://<IP>:8000/health`), `journalctl -u btctrader-advisor`. Die Strategie läuft ohne Gate weiter |
| `action_failed` | Guard konnte `stopentry`/`forceexit` nicht ausführen | Sofort FreqUI oder Telegram: `/stopentry` und bei Bedarf `/forceexit all` von Hand. Dann Log des Guards lesen |
| Healthchecks "down" | Der Heartbeat hat `/fail` gepingt (Bot antwortet nicht, `last_process` älter als 90 s, `killswitch.lock` existiert; Alarm binnen einer Minute) oder es kam seit der Grace-Zeit gar kein Ping mehr (Container oder Timer weg) | Vom Handy: Tailscale-App, dann FreqUI. Antwortet nichts: Host prüfen (PVE-GUI über LAN), `pct status 210`. Bei Stromausfall: USV-Ablauf abwarten |
| Freqtrade Telegram "OperationalException" | Bot hat den Handel gestoppt (`State.STOPPED`) | Abschnitt 10 |
| Freqtrade Telegram "Exchange ... temporarily unavailable" oder 429 | Bitvavo-Störung oder Rate-Limit | Nichts tun. Freqtrade wiederholt. Kein manuelles Cancel/Replace während der Störung |
| Freqtrade Telegram Watchdog-Neustart (`systemd` im Journal: "Watchdog timeout") | Hauptschleife hat 20 s nicht gemeldet | Log lesen. Einmal ist okay (z. B. langsame API). Wiederholt: Abschnitt 10 |

Grundregel bei jedem Alarm: erst lesen, dann handeln. Der Bot hält im Dry-Run kein echtes Geld. Live gilt: Eine offene Long-Position ohne Bot ist Buy-and-Hold, kein Notfall. Ein Notverkauf über die Bitvavo-App ist die letzte Option, nicht die erste.

## 4. Ledger und Freqtrade weichen ab

Der Ledger baut FIFO bei jedem Lauf deterministisch aus allen Fills neu auf. Abweichungen zwischen `fills` und der Freqtrade-DB entstehen durch:

- Ledger-Lauf vor dem Fill: Der Timer läuft um 00:20 UTC. Ein Fill um 00:25 UTC erscheint erst am nächsten Tag. Kein Fehler.
- Live: Freqtrade hat eine Order abgebrochen, die Börse hatte sie aber teilweise gefüllt. Der Ledger (Quelle Börse) hat den Fill, Freqtrade nicht. Freqtrade gleicht beim nächsten Start ab (`startup_update_open_orders`).
- Manuelle Orders neben dem Bot. Nicht machen.

Prüfen:

```bash
bt btctrader-ledger rebuild          # Hash-Kette prüfen, FIFO neu aufbauen
bt btctrader-ledger sync             # Fills nachladen (Dry-Run: aus der Freqtrade-DB)
bt btctrader-ledger show --limit 50
sqlite3 -readonly /srv/trading/user_data/tradesv3.dryrun.sqlite "select id, pair, open_date, close_date, close_profit_abs from trades order by id desc limit 10;"
```

Bleibt die Differenz, notiere sie mit Datum in Deinem Betriebslog. Die Abweichung ist ein Vorfall im Sinne von `docs/PROJEKTPLAN.md`, Abschnitt 7.4. Live gilt der Börsen-Datensatz.

## 5. Kill-Switch zurücksetzen

Der Kill-Switch ist absichtlich nur manuell rücksetzbar. Vorher beantworten:

1. Warum ist der Drawdown entstanden? Markt (Kurs gefallen), Strategie (falsches Signal), Technik (falsche Equity, weil `/balance` einen Preis von 0 geliefert hat)?
2. Ist die Position tatsächlich glatt? FreqUI zeigt keinen offenen Trade. Live zusätzlich: Bitvavo-App zeigt 0 BTC (bis auf Staub).
3. Willst Du weiterhandeln? Nach einem echten 20-%-Drawdown ist das ein Go/No-Go-Punkt nach `docs/PROJEKTPLAN.md`, Abschnitt 11.

Dann:

```bash
bt btctrader-guard status          # Lock-Inhalt: Zeit, Equity, Peak
bt btctrader-guard reset --confirm
```

Ohne `--confirm` passiert nichts. Der Reset entfernt `killswitch.lock`, setzt `peak_equity` auf die aktuelle Equity zurück (sonst löst der Kill-Switch beim nächsten Lauf sofort wieder aus) und schreibt ein Event `killswitch_reset`. Der Bot bleibt in `stopentry`. Handel wieder freigeben: Telegram `/start` an den Freqtrade-Bot oder FreqUI "Start". Erst danach nimmt die Strategie neue Entries.

Dry-Run-Test des Mechanismus (einmal in Phase 0 machen). Ein kleiner Grenzwert allein reicht nicht: Ohne offene Position bleibt die Dry-Run-Equity konstant bei 1.000 EUR, `peak_equity` wird nie über die Equity gehoben und der Drawdown ist immer 0. Der Guard rechnet den Drawdown aus `peak_equity` in `state.json`, also hebst Du das Hoch von Hand an:

```bash
systemctl stop btctrader-guard.timer                      # kein Lauf dazwischen
bt btctrader-guard status                                 # aktuelle Equity und peak_equity lesen
python3 - <<'EOF'
import json
p = "/srv/trading/guard/state.json"
s = json.load(open(p))
s["peak_equity"] = "2000"                               # weit über der Equity (1000 EUR), Drawdown 50 %
json.dump(s, open(p, "w"))
EOF
chown freqtrade:freqtrade /srv/trading/guard/state.json
bt btctrader-guard check                                  # ein Lauf: forceexit all, stopentry, Lock, Alarm
bt btctrader-guard status                                 # killswitch.lock mit Zeit, Equity, Peak
```

Erwartung: Alarm-Bot und ntfy melden `killswitch`, `events.jsonl` hat ein `killswitch`-Event, FreqUI zeigt den Bot in `stopentry`, der Heartbeat pingt `/fail`. Ohne offene Position ist `forceexit all` ein Leerlauf und kein Fehler. Danach aufräumen: `bt btctrader-guard reset --confirm` (setzt `peak_equity` auf die aktuelle Equity), `systemctl start btctrader-guard.timer`, `/start` in Telegram. Alternative mit echter Bewegung: erst in Telegram `/forceenter BTC/EUR` an den Dry-Run-Bot (braucht `force_entry_enable: true` in der Konfiguration), dann `GUARD_MAX_DRAWDOWN_PCT=0.01` setzen und warten, bis der Kurs 0,01 % unter das beobachtete Hoch fällt.

## 6. Freqtrade aktualisieren

Freqtrade veröffentlicht monatlich. Die Version ist in `install.sh` gepinnt (`FT_VERSION=2026.8`). Vorgehen:

1. Changelog lesen: https://github.com/freqtrade/freqtrade/releases. Achte auf Änderungen an `bitvavo`, `order_time_in_force`, `protections`, `sd_notify`, der REST-API (`/api/v1/balance`, `/health`) und am DB-Schema (Migrationen laufen automatisch, aber Backup vorher).
2. Backup: `vzdump 210 --mode snapshot` auf dem Host, und Kopie von `tradesv3.dryrun.sqlite`.
3. Nur den Dry-Run aktualisieren. Live (Phase 4) läuft mindestens zwei Wochen auf der alten Version weiter, während der Dry-Run die neue testet.
   ```bash
   FT_VERSION=2026.9 bash /srv/trading/repo/deploy/container/install.sh
   systemctl restart freqtrade-dryrun.service
   journalctl -u freqtrade-dryrun -f
   ```
4. Prüfen: Startlog ohne Deprecation-Fehler, `freqtrade --version`, FreqUI verbindet, Guard-Lauf ohne `ft_unreachable`, ein Backtest mit `scripts/backtest.sh` liefert dieselben Trades wie `docs/BACKTEST.md`.
5. Pin im Repo ändern (`deploy/container/install.sh`, `FT_VERSION`), committen. Erst dann Live-Container bzw. `freqtrade.service` mit derselben Version.
6. Rollback: `FT_VERSION=2026.8 bash install.sh`, Restart. Die DB-Migration einer neueren Version ist nicht immer rückwärtskompatibel, deshalb das Backup aus Schritt 2.

Für `btctrader` selbst: `REPO_UPDATE=1 bash /srv/trading/repo/deploy/container/install.sh` zieht das Repo, installiert das Paket neu, kopiert `config.json` und die Units. Danach `systemctl restart freqtrade-dryrun btctrader-dashboard`. Timer nehmen die neue Version beim nächsten Lauf.

## 7. API-Keys rotieren

Turnus: alle 6 Monate, sofort bei Verdacht (fremdes Gerät, Log-Leck, Support-Anfrage mit Key). Bitvavo-Keys laufen nicht ab, Du musst es selbst tun.

1. In Bitvavo einen neuen Key mit demselben Scope und der IP-Whitelist anlegen. Den alten noch nicht löschen.
2. Neuen Key eintragen:
   - Dry-Run-Bot: `/etc/freqtrade/secrets-dryrun.env` oder `config-private.json`, dann `systemctl restart freqtrade-dryrun`.
   - `readonly`: `BITVAVO_API_KEY_RO`/`_SECRET_RO` in `btctrader.env`. Timer lesen die Datei beim nächsten Lauf.
   - `guard-cod` (Phase 4, nur mit `GUARD_COD_ENABLED=true`): `/etc/freqtrade/secrets-guard.env`. Nur der Guard-Timer liest die Datei, beim nächsten Lauf.
   - Live: `/etc/freqtrade/secrets.env`, `systemctl restart freqtrade`. Vorher sicherstellen, dass keine offene Order liegt (FreqUI), sonst nach dem Neustart den Abgleich im Log prüfen.
3. Prüfen: `journalctl -u freqtrade-dryrun -n 50` ohne `AuthenticationError`; ein Guard-Lauf ohne Fehler.
4. Alten Key in Bitvavo löschen. Datum im Passwort-Manager notieren.

Ebenfalls rotieren: `api_server.password` und `jwt_secret_key` in `config-private.json` (dann `FT_API_PASS` in `btctrader.env` anpassen und alle Dienste neu starten), Telegram-Tokens über BotFather (`/revoke`), `ADVISOR_API_KEY` zusammen mit `VLLM_API_KEY` auf dem GPU-Host.

## 8. Monatsexport und Jahresreport

### 8.1 Monatlich (Anfang des Monats)

Der Timer exportiert beim ersten Lauf im neuen Monat den Vormonat automatisch (`btctrader-ledger daily` ruft `export` auf). Prüfen und außer Haus kopieren:

```bash
ls -l /srv/trading/ledger/exports/
cd /srv/trading/ledger/exports && sha256sum -c 2026-08-bitvavo-fills.csv.sha256
```

Fehlt ein Monat: `bt btctrader-ledger export --month 2026-08`. Alle Monate neu: `bt btctrader-ledger export --all` (überschreibt gleiche Inhalte, der Hash bleibt gleich, wenn die Fills gleich sind).

Zusätzlich einmal im Monat in der Bitvavo-App den Transaktionsverlauf als CSV herunterladen und neben die Ledger-CSV legen. Das ist die Aufzeichnung der Handelsplattform, mit der das Finanzamt vergleicht (BMF Rn. 89). Live vergleichst Du die Zeilenzahl beider Dateien.

Dann die Off-Box-Kopie (`docs/SETUP.md`, Abschnitt 9.2).

### 8.2 Jährlich (Januar)

```bash
bt btctrader-ledger rebuild
bt btctrader-ledger report --year 2026
ls -l /srv/trading/ledger/reports/steuer-2026.csv
```

Die Datei enthält eine Zeile pro Veräußerung (Fill-Anteil je Lot): Anschaffungsdatum und -kosten inkl. Kaufgebühr, Veräußerungsdatum, Erlös, Verkaufsgebühr als Werbungskosten, Gewinn, `taxable` und `boundary_case`. Summen stehen am Ende bzw. im Dashboard unter `/api/tax?year=2026`.

Was Du damit machst:

- Steuerberater: Datei plus die Monats-CSVs plus die Bitvavo-CSVs. Fragen mitgeben: Werbungskosten-Ansatz (Server, Tools), Grenzfall `boundary_case = 1` (Verkauf genau am Jahrestag), Lots mit `pre_2027 = 0`, falls der Referentenentwurf (`docs/PROJEKTPLAN.md`, Abschnitt 3.7) Gesetz wird.
- Gegenprobe mit einem Steuertool (Blockpit oder CoinTracking) auf Basis der Bitvavo-CSV. Abweichungen erklären können.
- Dry-Run-Jahre: Der Report enthält nur simulierte Fills (`dry_run = 1`). Nicht in die Steuererklärung. Er dient dem Test des Ablaufs.
- Auch in Verlustjahren und unter der Freigrenze von 1.000 EUR die Anlage SO abgeben, damit Verluste festgestellt werden und die DAC8-Meldung der Börse nicht unerklärt bleibt.

`decisions.jsonl` einmal im Jahr in `decisions-2026.jsonl` umbenennen und ins Off-Box-Backup nehmen. Nicht löschen, nicht rotieren. Die Datei belegt die "legitimen Gründe" jeder Entscheidung (Art. 91 MiCA).

## 9. Aus Backup wiederherstellen

### 9.1 Ganzer Container (Host defekt, Container kaputt)

Auf dem Host (oder einem neuen Host mit demselben Backup-Storage):

```bash
pvesm list <backup-storage> | grep 'vzdump-lxc-210'
pct restore 210 <backup-storage>:backup/vzdump-lxc-210-<datum>.tar.zst --storage local-zfs --unprivileged 1
pct start 210
pct exec 210 -- systemctl is-active freqtrade-dryrun tailscaled
```

Ist die CTID belegt (alter Container noch da), zuerst `pct stop 210 && pct destroy 210` oder in eine andere ID restaurieren und dort prüfen. `--dev0 path=/dev/net/tun`, `onboot` und `startup` sind Teil der Container-Konfiguration und werden mit restauriert. Die Firewall-Datei `/etc/pve/firewall/210.fw` steckt mit im vzdump-Archiv (`etc/vzdump/pct.fw`) und wird von `pct restore` zurückgeschrieben, sofern Dein Nutzer Firewall-Rechte hat. Nach dem Restore `cat /etc/pve/firewall/210.fw` prüfen: Die angepassten Aliase (`vllm_host`, `lan_admin`, `resolver`) müssen drin sein. Nur wenn die Datei fehlt, die Vorlage aus dem Repo kopieren und die Aliase neu setzen. Die Repo-Version blind darüberzukopieren würde DNS und die vLLM-Verbindung mit den Beispieladressen stilllegen.

Nach dem Restore: Das Backup ist bis zu 24 Stunden alt. Freqtrade gleicht offene Orders beim Start mit der Börse ab. Live: FreqUI-Trades mit den Bitvavo-Fills des letzten Tages vergleichen, dann `bt btctrader-ledger sync` (Quelle Börse holt fehlende Fills nach). Tailscale-Login bleibt gültig, der Maschinenname auch.

### 9.2 Nur Dateien (versehentlich gelöscht, DB korrupt)

Aus dem vzdump-Archiv einzelne Dateien holen, ohne den Container zu ersetzen:

```bash
mkdir -p /tmp/restore && cd /tmp/restore
tar --zstd -xf /path/to/vzdump-lxc-210-<datum>.tar.zst ./srv/trading/ledger/ledger.sqlite
pct push 210 ./srv/trading/ledger/ledger.sqlite /srv/trading/ledger/ledger.sqlite.restore --user freqtrade --group freqtrade
```

Im Container den Dienst stoppen, der die Datei nutzt (`systemctl stop btctrader-ledger.timer`), Datei tauschen, `bt btctrader-ledger rebuild` (prüft die Hash-Kette), Timer wieder starten. Für die Freqtrade-DB: Bot stoppen, tauschen, starten, Log lesen.

Vor dem Live-Start (Phase 4) einmal einen Restore in eine andere CTID durchspielen, damit der Ablauf sitzt. Phase-0-Kriterium in `docs/PROJEKTPLAN.md`, Abschnitt 11.

## 10. Bot stoppt mit OperationalException

Vorab, wenn der Prozess gar nicht läuft: `systemctl status freqtrade-dryrun`. Die Bot-Units haben `Restart=always` ohne Startlimit (`StartLimitIntervalSec=0`): Scheitert der Start (Börse oder DNS nach einem Stromausfall noch nicht erreichbar, falscher Key), versucht systemd es alle 10 Sekunden erneut, bis es klappt. Ein Konfigurationsfehler erzeugt deshalb eine Dauerschleife im Journal: `systemctl stop freqtrade-dryrun`, Ursache beheben, `systemctl start freqtrade-dryrun`. Zeigt `systemctl status` trotzdem `failed` (z. B. nach einem manuellen `systemctl start`, der scheiterte, oder einer älteren Unit-Version mit Startlimit): `systemctl reset-failed freqtrade-dryrun && systemctl start freqtrade-dryrun`. Für die Timer-Units gilt dasselbe mit `btctrader-guard.service` usw.

Freqtrade wirft `OperationalException` bei Konfigurationsfehlern, ungültigen Strategien oder unerwarteten Zuständen der Börse. Der Prozess läuft weiter, aber im Zustand `STOPPED`: kein Handel, keine Stoploss-Überwachung. Telegram bekommt eine Nachricht mit dem Traceback. Der Heartbeat pingt `HEALTHCHECKS_URL/fail`, sobald `last_process` älter als 90 Sekunden ist, deshalb kommt auch der Healthchecks-Alarm.

Vorgehen:

1. Log lesen: `journalctl -u freqtrade-dryrun -n 200 --no-pager | grep -A 30 OperationalException`.
2. Häufige Ursachen:
   - `config-private.json` fehlerhaft (JSON-Syntax, falscher Key): `python3 -m json.tool /srv/trading/user_data/config-private.json`.
   - Strategie importiert nicht (nach einem `git pull`): `runuser -u freqtrade -- /opt/freqtrade/venv/bin/freqtrade list-strategies --userdir /srv/trading/user_data`.
   - Börse liefert das Pair nicht oder die Markets sind unvollständig: `curl -s https://api.bitvavo.com/v2/markets | jq '.[] | select(.market=="BTC-EUR")'`.
   - Datenformat oder DB-Migration nach einem Update: Abschnitt 6, Rollback.
3. Ursache beheben.
4. Bot neu starten: `systemctl restart freqtrade-dryrun`. Ein `/start` in Telegram reicht nur, wenn die Ursache ohne Neustart behoben ist (z. B. Börsenstörung vorbei). Bei geänderter Konfiguration immer Neustart.
5. Prüfen: `systemctl status freqtrade-dryrun` zeigt `State: RUNNING`, Guard-Event `ft_recovered` erscheint, Healthchecks geht auf "up".

Live-Zusatz: Solange der Bot steht, ist eine offene Position unbewacht. Bei einer Tageskerzen-Strategie ist das für Stunden vertretbar. Dauert die Behebung länger als einen Tag und der Markt fällt, entscheide bewusst über einen manuellen Exit in der Bitvavo-App. Trage jeden manuellen Eingriff mit Datum und Grund ins Betriebslog ein und lass den Ledger danach mit Quelle Börse synchronisieren, damit der manuelle Fill im Steuerprotokoll landet.

## 11. Phase 4: Live einschalten (Kurzfassung)

Nur wenn die Go-Kriterien aus `docs/PROJEKTPLAN.md`, Abschnitt 11 erfüllt sind. Reihenfolge:

1. Trade-Key `bot-live` (View + Trade, IP-Whitelist, kein Withdraw) anlegen, in `/etc/freqtrade/secrets.env` eintragen. Der Dry-Run läuft mit seinem View-Key weiter.
2. `btctrader.env`: `FT_API_URL=http://127.0.0.1:8081`, `FT_DB_PATH=/srv/trading/user_data/tradesv3.sqlite`, `LEDGER_SOURCE=exchange`, RO-Key gesetzt. Der Guard bewacht damit die Live-Instanz. Optional `GUARD_COD_ENABLED=true`: dann den Key `guard-cod` (View + Trade) in `/etc/freqtrade/secrets-guard.env` eintragen, nie in `btctrader.env`.
3. Zweiten Telegram-Bot für die Live-Instanz in `secrets.env` eintragen.
4. Startkapital 200 bis 500 EUR auf dem Konto, Rest bleibt auf der Bank. `dry_run_wallet` spielt live keine Rolle; `tradable_balance_ratio 0.99` gilt.
5. `systemctl enable --now freqtrade.service`, Log lesen, FreqUI auf Port 8081 als zweiten Bot anlegen (`tailscale serve --bg --https=8444 http://127.0.0.1:8081`).
6. Erste Woche täglich: Bilanzabgleich im Guard-Log, Fills in Bitvavo-App gegen FreqUI, Gebührenwährung im Ledger (`fee_currency`).

Nie beide Bots mit Trade-Rechten auf demselben Konto. Der Dry-Run behält seinen View-only-Key.
