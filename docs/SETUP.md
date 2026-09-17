# Setup: Proxmox-Host, Container, Bitvavo, Tailscale, Dry-Run

Stand: 15.09.2026. Für Proxmox VE 9.2.10 mit ZFS, statischer öffentlicher IP und USV. Freqtrade 2026.8, Debian 13 im Container.

Diese Anleitung führt Dich vom leeren Host bis zum laufenden Dry-Run. Jeder Schritt hat eine Prüfung am Ende. Mach den nächsten Schritt erst, wenn die Prüfung stimmt. Die verbindlichen Pfade und Variablen stehen in `docs/KOMPONENTEN.md`. Der Betrieb danach steht in `docs/BETRIEB.md`.

Alle Dateien für diese Anleitung liegen unter `deploy/`:

| Datei | Läuft wo | Zweck |
|---|---|---|
| `deploy/proxmox/create-lxc.sh` | PVE-Host | Container anlegen (unprivilegiert, ZFS, Autostart, `/dev/net/tun`) |
| `deploy/proxmox/firewall/210.fw` | PVE-Host | Firewall-Regeln des Containers (Default-Deny) |
| `deploy/proxmox/nut/README.md` | PVE-Host | USV-Anbindung mit NUT |
| `deploy/container/install.sh` | im Container | Pakete, Nutzer, Freqtrade, Repo, Units, Tailscale |
| `deploy/container/sync-config.sh` | im Container | `config.json` aus dem Repo übernehmen |
| `deploy/container/systemd/` | im Container | die Units |
| `deploy/container/env/` | im Container | Vorlagen für `/etc/freqtrade/*.env` |
| `deploy/vllm/` | GPU-Host | vLLM für den Advisor |

## 0. Voraussetzungen

- Proxmox VE 9.2.10 auf dem Host, Storage `local-zfs` (oder ein anderer ZFS-Pool), Template-Storage `local`.
- Eine freie statische Adresse im LAN für den Container, z. B. `192.168.1.210/24`, und das Gateway.
- Die statische öffentliche IP Deines Anschlusses (für die Bitvavo-IP-Whitelist). Prüfen: `curl -4 -s https://ifconfig.me`.
- Ein Tailscale-Konto (kostenloser Personal-Plan reicht).
- Ein Telegram-Konto, um über BotFather zwei Bots anzulegen (einer für Freqtrade, einer für die Alarme).
- Optional: ein Healthchecks.io-Konto oder eine eigene Instanz, ein self-hosted ntfy.
- Der GPU-Host für vLLM (Abschnitt 7). Ohne ihn läuft alles bis auf den Advisor.
- Dieses Repo auf GitHub (privat, ohne Secrets) oder auf einem anderen Git-Server, den der Container erreichen kann.
- Ein Steuerberater-Termin steht noch nicht an, aber ein Ordner für die monatlichen CSV-Exporte außerhalb des Hosts.

## 1. Host prüfen

Auf dem PVE-Host als root:

```bash
pveversion                 # pve-manager/9.2.10 oder neuer
zfs list                   # rpool und der Storage für den Container
pvesm status               # local-zfs aktiv
chronyc tracking           # "Leap status : Normal", Offset im Millisekundenbereich
pve-firewall status        # Status des Firewall-Dienstes
cat /etc/pve/firewall/cluster.fw 2>/dev/null || echo "keine Datacenter-Firewall-Datei"
```

Warum das wichtig ist:

- Die Container teilen sich die Kernel-Uhr des Hosts. Bitvavo lehnt Anfragen mit falscher Zeit ab (Nonce-Fehler). chrony muss auf dem Host sauber laufen.
- ZFS erlaubt den Snapshot-Modus von vzdump. Backups laufen dann ohne Stopp des Bots.
- Die Firewall-Regeln pro Container greifen nur, wenn die Datacenter-Firewall aktiv ist (Schritt 2).

Prüfung: `pveversion` zeigt 9.x, `chronyc tracking` zeigt einen Offset unter 100 ms.

## 2. Container anlegen

Hol Dir das Repo auf den Host (nur für die Skripte unter `deploy/proxmox/`):

```bash
apt-get install -y git
git clone <REPO_URL> /root/BitCoin-Trader
cd /root/BitCoin-Trader/deploy/proxmox
```

Passe die Firewall-Aliase an Dein Netz an. Öffne `firewall/210.fw` und setze unter `[ALIASES]`:

- `vllm_host`: die LAN-IP des GPU-Hosts (oder eine Dummy-Adresse, falls Du noch keinen hast),
- `lan_admin`: Dein LAN-Subnetz, aus dem Du SSH und Ping erlauben willst,
- `resolver`: Dein DNS-Server (meist der Router).

Dann den Container anlegen. `CT_DNS` muss dieselbe Adresse sein wie der Alias `resolver`: Die Firewall lässt DNS nur zu diesem Alias durch. Ohne `CT_DNS` erbt der Container die `/etc/resolv.conf` des Hosts, und wenn der Host über 9.9.9.9 oder den Provider auflöst, scheitert im Container jede Namensauflösung ohne Logzeile (apt, pip, Bitvavo, Telegram, Tailscale).

```bash
CT_DNS=192.168.1.1 CT_IP=192.168.1.210/24 CT_GW=192.168.1.1 bash create-lxc.sh
```

Das Skript lädt das Debian-13-Template, erstellt CT 210 (`btc-bot`, 2 Kerne, 4 GB RAM, 512 MB Swap, 32 GB auf `local-zfs`), setzt `onboot=1`, `startup order=20,up=30`, `firewall=1` auf `net0` und reicht `/dev/net/tun` mit `--dev0 path=/dev/net/tun` durch. Es ist unprivilegiert ohne `nesting` oder `keyctl`. Weitere Parameter (CTID, CT_HOSTNAME, STORAGE, CORES, MEMORY, SSH_KEYS) stehen im Kopf des Skripts. Die Firewall-Datei wird nach `/etc/pve/firewall/210.fw` kopiert, wenn dort noch keine liegt.

Datacenter-Firewall aktivieren, falls noch nicht geschehen. Vorsicht: Mach das aus einer Sitzung im LAN, nicht über eine andere Firewall-Regel hinweg. Beim Aktivieren trägt Proxmox das lokale Cluster-Netz in das IP-Set `management` ein, so dass GUI (8006) und SSH (22) zum Host weiter erreichbar bleiben.

```bash
# /etc/pve/firewall/cluster.fw anlegen oder ergänzen:
cat > /etc/pve/firewall/cluster.fw <<'EOF'
[OPTIONS]
enable: 1
EOF
pve-firewall restart
pve-firewall status        # "Status: enabled/running"
```

Wenn die Datei schon existiert, setze nur `enable: 1` unter `[OPTIONS]`. Die Host-Policy bleibt beim Default. Prüfe danach, dass Du die PVE-GUI noch erreichst.

Prüfung: `pct status 210` zeigt `running`. `pct config 210` zeigt `dev0: /dev/net/tun`, `onboot: 1`, `unprivileged: 1`, `nameserver: 192.168.1.1` (der `resolver`-Alias) und `firewall=1` in `net0`. `pct exec 210 -- ls -l /dev/net/tun` zeigt das Gerät.

## 3. Im Container installieren

Betritt den Container und hol das Repo:

```bash
pct enter 210
apt-get update && apt-get install -y git ca-certificates
git clone <REPO_URL> /root/BitCoin-Trader
REPO_URL=<REPO_URL> bash /root/BitCoin-Trader/deploy/container/install.sh
```

`install.sh` macht folgendes und lässt sich jederzeit wiederholen:

1. apt-Pakete (python3-venv, build-essential, git, curl, sqlite3), Zeitzone UTC, kein Zeitdienst im Container.
2. Nutzer `freqtrade` mit Home `/srv/trading`, Verzeichnisse `advisor/`, `ledger/`, `guard/`.
3. `/opt/freqtrade/venv` mit `freqtrade==2026.8` von PyPI und FreqUI (`freqtrade install-ui`). `/opt/freqtrade/.venv` ist ein Symlink darauf, damit `scripts/*.sh` funktionieren.
4. `/srv/trading/repo` als Klon von `REPO_URL` (Owner `freqtrade`).
5. `/srv/trading/venv` mit `pip install -e /srv/trading/repo`. Danach gibt es `btctrader-advisor`, `btctrader-ledger`, `btctrader-guard`, `btctrader-dashboard`.
6. `freqtrade create-userdir --userdir /srv/trading/user_data`, `strategies` als Symlink auf `repo/user_data/strategies`, `config.json` per `sync-config.sh` kopiert, `config-private.json` aus der Vorlage (0600).
7. `/etc/freqtrade/btctrader.env`, `secrets-dryrun.env`, `secrets.env`, `secrets-guard.env` aus den Vorlagen (root:freqtrade 0640), nur wenn sie fehlen.
8. systemd-Units nach `/etc/systemd/system/`, `daemon-reload`, journald-Limits (500 MB, 1 Jahr).
9. Tailscale aus dem offiziellen apt-Repo, `tailscaled` gestartet (nicht `tailscale up`).
10. Vendor-JS für das Dashboard (Chart.js, htmx) mit Hash-Prüfung.

Das Skript startet nichts. Erst mit `ENABLE_DRYRUN=1` (Schritt 8) aktiviert es den Dry-Run, die Timer und das Dashboard. `freqtrade.service` (Live) startet es nie.

Für einen Test der Strategie mit historischen Daten kannst Du schon jetzt Kerzen laden und einen Backtest laufen lassen (`scripts/download-data.sh`, `scripts/backtest.sh`, Ergebnis in `docs/BACKTEST.md`). Das ist optional.

Prüfung:

```bash
/opt/freqtrade/venv/bin/freqtrade --version          # 2026.8
/srv/trading/venv/bin/btctrader-guard --help
ls -l /srv/trading/user_data/strategies               # -> /srv/trading/repo/user_data/strategies
ls -l /etc/freqtrade/                                 # vier Dateien, root freqtrade, 640
systemctl list-unit-files 'freqtrade*' 'btctrader-*'  # alle "disabled"
```

## 4. Bitvavo-Konto und API-Keys

Privatkonto, nicht die GmbH. Details und Begründung: `docs/PROJEKTPLAN.md`, Abschnitt 4.3.

1. Registrieren, sofort 2FA (TOTP) aktivieren. Kein SMS-2FA.
2. Identität in der App verifizieren.
3. Erste SEPA-Einzahlung von einem Konto auf Deinen Namen. Für den Dry-Run reicht ein kleiner Betrag. Das Konto wird das Referenzkonto für Auszahlungen.
4. In der App die Gebührentabelle prüfen: Kategorie A, Stufe 0, 0,15 % Maker / 0,25 % Taker. Weicht das ab, `FEE_MAKER` und `FEE_TAKER` in `btctrader.env` anpassen.
5. Settings, API, Keys anlegen. Jeder Key bekommt die IP-Whitelist mit Deiner statischen öffentlichen IP.

| Key-Name | Scope | Wo eintragen | Wann |
|---|---|---|---|
| `bot-dryrun` | View | `secrets-dryrun.env` oder `config-private.json` | jetzt |
| `readonly` | View | `btctrader.env` (`BITVAVO_API_KEY_RO`, `_SECRET_RO`) | jetzt (optional im Dry-Run, Pflicht ab Live) |
| `bot-live` | View + Trade | `secrets.env` | erst Phase 4 |
| `guard-cod` | View + Trade | `secrets-guard.env` (`GUARD_TRADE_API_KEY`, `_SECRET`) | erst Phase 4, nur mit `GUARD_COD_ENABLED=true`; die Datei lädt allein der Guard |

Regeln:

- Nie den Scope Withdraw. Bitvavo-Auszahlungen per API umgehen die 2FA.
- Der Dry-Run braucht keinen Trade-Scope. Freqtrade sendet im Dry-Run keine Orders.
- Der Trade-Key wird erst in Phase 4 angelegt, nach mindestens 3 Monaten Dry-Run (`docs/PROJEKTPLAN.md`, Abschnitt 11).
- Notiere jeden Key mit Datum und Scope in Deinem Passwort-Manager. Rotation: `docs/BETRIEB.md`.

Prüfung der Whitelist (ohne Key, nur Erreichbarkeit): `curl -s https://api.bitvavo.com/v2/time`. Mit Key prüfst Du in Schritt 8 über die Freqtrade-Logs.

## 5. Env-Dateien und config-private.json füllen

Im Container als root. Die Dateien gehören root:freqtrade mit 0640, `config-private.json` gehört freqtrade mit 0600. `install.sh` hat sie angelegt.

### 5.1 `/srv/trading/user_data/config-private.json`

```bash
nano /srv/trading/user_data/config-private.json
```

- `exchange.key` / `exchange.secret`: der `bot-dryrun`-Key (View). Alternativ in `secrets-dryrun.env`, dann hier leer lassen. Nicht beides.
- `api_server.username`: z. B. `freqtrader`. `api_server.password`: langes Zufallspasswort. `jwt_secret_key`: mindestens 32 Zufallszeichen. `ws_token`: Zufallsstring.
- `telegram.enabled: true`, `token` und `chat_id` des Freqtrade-Bots (BotFather: `/newbot`; Chat-ID über `@userinfobot` oder die Freqtrade-Doku).

Zufallswerte erzeugen: `openssl rand -hex 32`.

### 5.2 `/etc/freqtrade/secrets-dryrun.env`

Entweder Key und Secret des `bot-dryrun`-Keys eintragen oder die beiden Zeilen auskommentieren, wenn sie in `config-private.json` stehen. Platzhalter dürfen nicht stehen bleiben, sonst verweigert `install.sh` das Aktivieren.

### 5.3 `/etc/freqtrade/btctrader.env`

Die Datei ist vollständig kommentiert. Pflicht:

- `FT_API_USER`, `FT_API_PASS`: dieselben Werte wie in `config-private.json`.
- `BENCHMARK_START`: das Datum, an dem Du den Dry-Run startest (Tag 0 für Buy-and-Hold und DCA). Die Vorlage enthält absichtlich einen Platzhalter, kein Datum: ein altes Datum würde die Benchmarks still vom falschen Kurs rechnen.
- `ADVISOR_BASE_URL`, `ADVISOR_MODEL`, `ADVISOR_API_KEY`: Werte vom GPU-Host (Schritt 7). Ohne GPU-Host: Werte stehen lassen, der Advisor-Timer schlägt dann stündlich mit Exit-Code 1 fehl und `decision.json` bleibt leer. Das stört nichts, weil `ADVISOR_MODE=shadow`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: der zweite Bot, nur für Alarme des Guards. Nicht der Freqtrade-Bot.

Optional jetzt: `BITVAVO_API_KEY_RO`/`_SECRET_RO` (Key `readonly`), `HEALTHCHECKS_URL`, `NTFY_URL`.

### 5.4 `/etc/freqtrade/secrets.env`

Bleibt mit Platzhaltern liegen bis Phase 4. `freqtrade.service` ist nicht aktiviert.

### 5.5 `/etc/freqtrade/secrets-guard.env`

Bleibt leer bis Phase 4. Nur hier gehört später der Trade-Key für den Dead-Man's-Switch hin (`GUARD_TRADE_API_KEY`, `GUARD_TRADE_API_SECRET`). Die Datei lädt allein `btctrader-guard.service`; `btctrader.env` landet dagegen in der Umgebung aller Dienste, auch des Dashboards ohne Login. Findet der Guard den Key trotzdem in `btctrader.env`, schreibt er eine Warnung ins Log.

Prüfung:

```bash
BTCTRADER_ENV_FILE=/etc/freqtrade/btctrader.env /srv/trading/venv/bin/btctrader-advisor context --no-bot | head -c 300
grep -cE '^[[:space:]]*[A-Za-z_]+=.*(CHANGE_ME|PLACEHOLDER)' /etc/freqtrade/btctrader.env /etc/freqtrade/secrets-dryrun.env
grep -c 'CHANGE_ME\|PLACEHOLDER' /srv/trading/user_data/config-private.json
```

Die erste Zeile gibt den Marktkontext als JSON aus (öffentliche Bitvavo-API, kein Key nötig). Die zweite und dritte müssen jeweils `0` liefern. Die zweite zählt nur aktive Zeilen, genau wie `install.sh`: auskommentierte Platzhalter (Weg b in `secrets-dryrun.env`) stören nicht.

## 6. Tailscale

Zugriff auf FreqUI und Dashboard läuft nur über Dein Tailnet. Kein Port-Forward am Router, kein Tailscale Funnel.

### 6.1 Admin-Konsole vorbereiten

In der Tailscale-Admin-Konsole (DNS-Seite): MagicDNS aktivieren und unter "HTTPS Certificates" HTTPS einschalten.

Warnung zur Certificate Transparency: Tailscale stellt Let's-Encrypt-Zertifikate aus. Der Maschinenname (`btc-bot.<tailnet>.ts.net`) landet damit in öffentlichen Certificate-Transparency-Logs. Tailscale schreibt selbst: "Do not enable the HTTPS feature if any of your machine names contain sensitive information." Wähle deshalb einen unverfänglichen Hostnamen. `btc-bot` verrät, was die Maschine tut. Wenn Dich das stört: Container mit `CT_HOSTNAME=<neutraler Name>` anlegen (Schritt 2) oder nachträglich `tailscale up --hostname=<neutraler Name>`.

### 6.2 Im Container anmelden

```bash
tailscale up --ssh=false
```

Das Kommando druckt eine Login-URL. Öffne sie im Browser, bestätige das Gerät. In der Admin-Konsole "Key expiry" für dieses Gerät deaktivieren, sonst musst Du Dich alle 180 Tage neu anmelden.

Prüfung: `tailscale status` zeigt das Gerät online mit einer 100.x-Adresse. `tailscale ip -4` zeigt die Adresse.

Falls `tailscale up` mit einem Fehler zu `/dev/net/tun` abbricht: auf dem Host `pct config 210 | grep dev0` prüfen, nötigenfalls `pct set 210 --dev0 path=/dev/net/tun` und `pct reboot 210`.

### 6.3 FreqUI und Dashboard veröffentlichen

Beide Dienste lauschen nur auf `127.0.0.1` (8080 und 8090). `tailscale serve` macht daraus HTTPS-Endpunkte im Tailnet:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8080     # FreqUI + REST-API
tailscale serve --bg --https=8443 http://127.0.0.1:8090    # Dashboard-Ergänzung
tailscale serve status
```

Die Konfiguration überlebt Neustarts. Zurücksetzen mit `tailscale serve reset`.

Prüfung (erst nach Schritt 8, wenn die Dienste laufen): vom Handy oder Laptop im Tailnet `https://btc-bot.<tailnet>.ts.net/` öffnet FreqUI, `https://btc-bot.<tailnet>.ts.net:8443/` das Dashboard. Beim ersten Aufruf dauert die Zertifikatsausstellung einige Sekunden.

In FreqUI: "Add new bot", URL `https://btc-bot.<tailnet>.ts.net`, Nutzer und Passwort aus `config-private.json`.

## 7. vLLM-Host erreichbar machen

Der Advisor spricht mit einem vLLM-Server auf einem GPU-Host im Heimnetz. Einrichtung des Servers: `docs/VLLM_ADVISOR.md` und `deploy/vllm/README.md`.

Zwei Wege vom Container zum GPU-Host:

- LAN: vLLM in `deploy/vllm/.env` mit `VLLM_BIND=<LAN-IP des GPU-Hosts>` starten. Die Firewall des Containers erlaubt TCP 8000 zum Alias `vllm_host` (Schritt 2). `ADVISOR_BASE_URL=http://<LAN-IP>:8000/v1`.
- Tailscale: GPU-Host ins Tailnet, `VLLM_BIND=<Tailscale-IP des GPU-Hosts>`, `ADVISOR_BASE_URL=http://<Tailscale-IP>:8000/v1`. Die Firewall braucht dann keine LAN-Regel, der Verkehr läuft im Tunnel.

`ADVISOR_API_KEY` in `btctrader.env` ist derselbe Wert wie `VLLM_API_KEY` auf dem GPU-Host. `ADVISOR_MODEL=advisor`, wenn vLLM mit `--served-model-name advisor` läuft (Default der Compose-Datei).

Prüfung im Container:

```bash
curl -fsS http://<IP>:8000/health && echo OK
runuser -u freqtrade -- /srv/trading/venv/bin/btctrader-advisor run --dry-run    # druckt den Request, ruft nichts auf
runuser -u freqtrade -- /srv/trading/venv/bin/btctrader-advisor run              # echter Aufruf
cat /srv/trading/advisor/decision.json
```

`decision.json` enthält `regime`, `confidence`, `mode: "shadow"`. Der Advisor bekommt nie Börsen-Keys. Er liest nur öffentliche Kerzen und optional den Bot-Status.

## 8. Dry-Run starten und prüfen

```bash
ENABLE_DRYRUN=1 bash /srv/trading/repo/deploy/container/install.sh
```

Das aktiviert und startet die vier Timer (`guard`, `heartbeat`, `ledger`, `advisor`), `btctrader-dashboard.service` und zuletzt `freqtrade-dryrun.service`. Sind noch Platzhalter in den Dateien, bricht das Skript vorher ab. Startet der Bot nicht (falscher Key, falsches API-Passwort, Börse nicht erreichbar), gibt das Skript nur eine Warnung aus: Guard, Heartbeat und Dashboard laufen dann schon, und die Unit versucht den Start alle 10 Sekunden erneut, bis Du die Ursache behoben hast.

Dann, in dieser Reihenfolge:

1. Bot-Log: `journalctl -u freqtrade-dryrun -f`. Erwartet: "Bot heartbeat" alle 60 Sekunden, keine `AuthenticationError`, keine Nonce-Fehler. Bitvavo-Fehlercodes im Log: `errorCode 305` oder `306` heißt, der Key ist ungültig oder noch nicht aktiviert (Key und Secret prüfen, Bestätigungs-Mail von Bitvavo klicken). `errorCode 307` heißt, Deine IP steht nicht in der Whitelist des Keys. Die Datei `/srv/trading/user_data/logs/freqtrade-dryrun.log` enthält dasselbe mit Rotation (10 x 10 MB).
2. Watchdog: `systemctl status freqtrade-dryrun` zeigt `Status: "State: RUNNING"` und keinen Neustart. Freqtrade meldet sich alle 5 Sekunden bei systemd (`WatchdogSec=20`). Scheitert eine Unit sofort mit `Failed to set up mount namespacing` im Journal, verweigert der Container die Mount-Isolierung der Units (`ProtectSystem`, `PrivateTmp`, `ProtectHome`). Dann auf dem Host `pct set 210 --features nesting=1` und `pct reboot 210`; Proxmox nennt `nesting` als Voraussetzung dafür, dass systemd Dienste isolieren darf. Alternativ die drei Zeilen aus allen Units entfernen.
3. FreqUI über Tailscale (Schritt 6.3): Bot online, Pair `BTC/EUR`, Strategie `BtcTrend`, Dry-Run-Wallet 1.000 EUR. Auf Tageskerzen passiert an den meisten Tagen nichts. Ein Entry kommt erst, wenn der Schlusskurs über SMA200 x 1,03 liegt.
4. Telegram: Der Freqtrade-Bot hat eine Startnachricht geschickt. `/status` und `/balance` antworten. Der Alarm-Bot (Guard) meldet sich erst bei einem Ereignis. Test: `runuser -u freqtrade -- /srv/trading/venv/bin/btctrader-guard status`.
5. Guard: `systemctl list-timers 'btctrader-*'` zeigt vier Timer mit "next" innerhalb der nächsten Minute bzw. Stunde. `journalctl -u btctrader-guard --since -5min` zeigt einen Lauf pro Minute ohne Traceback. `cat /srv/trading/guard/state.json` zeigt `day_start_equity` und `peak_equity`. `/srv/trading/guard/events.jsonl` enthält ein `day_start`-Event.
6. Heartbeat: `journalctl -u btctrader-heartbeat --since -5min`. Mit `HEALTHCHECKS_URL` zeigt Healthchecks das Check als "up". Setz die Grace-Zeit dort auf 3 Minuten und teste den Alarm einmal: `systemctl stop freqtrade-dryrun`. Die Benachrichtigung kommt innerhalb von etwa einer Minute, denn der Heartbeat pingt `HEALTHCHECKS_URL/fail`, sobald `/health` nicht antwortet, und Healthchecks meldet ein `/fail` sofort. Die Grace-Zeit greift nur, wenn gar kein Ping mehr ankommt, also wenn der ganze Container oder der Heartbeat-Timer weg ist. Danach `systemctl start freqtrade-dryrun`; das Check geht mit dem nächsten Ping wieder auf "up".
7. Dashboard: `https://btc-bot.<tailnet>.ts.net:8443/` zeigt die Kacheln. Vor dem ersten Ledger-Lauf sind Equity-Chart und Fills leer, `Bot-Status` muss "erreichbar" zeigen.
8. Ledger: Der erste Lauf kommt um 00:20 UTC. Vorziehen mit `systemctl start btctrader-ledger.service`, dann `journalctl -u btctrader-ledger` und `runuser -u freqtrade -- /srv/trading/venv/bin/btctrader-ledger show`. Erwartet: eine Zeile in `equity_daily` mit Equity 1.000 EUR, B&H- und DCA-Benchmark, noch keine Fills.
9. Advisor: `journalctl -u btctrader-advisor` nach der nächsten vollen Stunde plus 7 Minuten. `tail -n 1 /srv/trading/advisor/decisions.jsonl`.

Ab jetzt läuft der Dry-Run mindestens 3 Monate. Die täglichen und wöchentlichen Kontrollen stehen in `docs/BETRIEB.md`.

## 9. Backups

Zwei Ebenen: Container-Backup mit vzdump auf dem Host und eine Off-Box-Kopie der Nachweise.

### 9.1 vzdump auf dem Host

Datacenter, Backup, Add: CT 210, Modus `snapshot` (geht mit ZFS ohne Stopp), Zeitplan täglich z. B. 03:30, Storage PBS oder NAS, Aufbewahrung `keep-daily 7, keep-weekly 4, keep-monthly 3`, Kompression zstd, E-Mail bei Fehler. Auf der Kommandozeile:

```bash
cat >> /etc/pve/jobs.cfg <<'EOF'
vzdump: btc-bot-daily
        schedule 03:30
        vmid 210
        mode snapshot
        storage <dein-backup-storage>
        compress zstd
        prune-backups keep-daily=7,keep-weekly=4,keep-monthly=3
        enabled 1
EOF
```

Prüfung: einmal manuell `vzdump 210 --mode snapshot --storage <storage> --compress zstd` und die Restore-Probe aus `docs/BETRIEB.md` (Abschnitt "Aus Backup wiederherstellen") durchspielen.

### 9.2 Off-Box-Kopie der Nachweise

Das Container-Backup liegt auf demselben Standort. Für die Steuer (10 Jahre Aufbewahrung, `docs/PROJEKTPLAN.md` Abschnitt 3.5) brauchst Du eine Kopie außer Haus. Kopiere mindestens einmal pro Woche und nach jedem Monatsexport:

- `/srv/trading/ledger/ledger.sqlite` (plus `-wal` und `-shm`, oder besser ein konsistenter Dump: `sqlite3 ledger.sqlite ".backup '/tmp/ledger-$(date -u +%F).sqlite'"`),
- `/srv/trading/ledger/exports/` (Monats-CSVs mit `.sha256`),
- `/srv/trading/ledger/reports/`,
- `/srv/trading/advisor/decisions.jsonl`,
- `/srv/trading/guard/events.jsonl`,
- `/srv/trading/user_data/tradesv3.dryrun.sqlite` (später `tradesv3.sqlite`),
- `/etc/freqtrade/` und `config-private.json` nur verschlüsselt (enthalten Keys).

Ein einfacher Weg: vom Laptop im Tailnet per `tailscale ssh` oder `scp` (dafür `tailscale up --ssh` und einen Nutzer mit Leserechten), Ziel ein verschlüsselter Cloud-Ordner oder eine externe Platte.

## 10. USV und NUT testen

Die Einrichtung steht in `deploy/proxmox/nut/README.md`. Der Test dort in Abschnitt 5 hat vier Stufen. Mindestens Stufe 2 (Stecker ziehen, Status beobachten) und Stufe 3 (`upsmon -c fsd`, echter Shutdown mit Wiederanlauf) müssen einmal durchlaufen sein, bevor der Dry-Run als "Phase 0 abgeschlossen" gilt.

Nach dem Wiederanlauf prüfen:

```bash
pct list                                       # 210 running
pct exec 210 -- systemctl is-active freqtrade-dryrun btctrader-dashboard tailscaled
pct exec 210 -- systemctl list-timers 'btctrader-*'
```

Und der Freqtrade-Bot hat in Telegram eine neue Startnachricht geschickt.

## 11. Was Du nicht tun solltest

- Keinen Port am Router weiterleiten. Kein Tailscale Funnel. FreqUI hat kein eigenes HTTPS und keine Brute-Force-Sperre.
- Nie einen Bitvavo-Key mit Scope Withdraw anlegen. Auch nicht "nur kurz".
- Keinen Trade-Key vor Phase 4. Der Dry-Run braucht ihn nicht, und ein Key ohne Trade-Scope kann keine Order erzeugen, egal was der Code tut.
- `freqtrade.service` nicht aktivieren, bevor `secrets.env` einen Trade-Key hat und Du die Go-Kriterien aus `docs/PROJEKTPLAN.md` Abschnitt 11 erfüllt hast. Nie beide Bots mit Trade-Rechten auf demselben Konto.
- Keine Secrets ins Repo. `config-private.json` und `*.env` stehen in `.gitignore`. Vor jedem `git push` einmal `git status` lesen.
- Keine alten BTC-Bestände auf das Bot-Konto. FIFO gilt pro Wallet. Alte Coins würden zuerst als verkauft gelten (`docs/PROJEKTPLAN.md` Abschnitt 3.1).
- Keine DCA-Käufe von Hand auf dem Bot-Konto. Das DCA ist nur eine virtuelle Benchmark im Ledger.
- Kein Docker im LXC. Wenn Du Docker brauchst (z. B. Uptime Kuma), dann in einer kleinen VM.
- Keine Container-Features `nesting` oder `keyctl` einschalten, nur weil ein Tutorial es sagt. Der Stack braucht sie nicht. Einzige Ausnahme: `nesting=1`, wenn die Units mit `Failed to set up mount namespacing` scheitern (Schritt 8, Punkt 2).
- Keine Parameteränderungen an der Strategie direkt in `/srv/trading/repo`. Änderungen gehen über einen Commit im Repo, `git pull` im Container (`REPO_UPDATE=1 bash install.sh`) und einen Neustart des Bots.
