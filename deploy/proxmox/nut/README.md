# USV am Proxmox-Host mit NUT

Stand: 15.09.2026. Gilt für Proxmox VE 9.x (Debian 13) und NUT 2.8.x aus den Debian-Paketen.

Ziel: Bei Stromausfall fährt der Host geordnet herunter, bevor die USV leer ist. Proxmox stoppt dabei die Container in umgekehrter Startreihenfolge. So bleibt keine halb geschriebene SQLite-Datei zurück und kein Fill geht verloren. Nach Rückkehr des Stroms startet der Host, `onboot=1` startet den CT und `Restart=always` den Bot. Die Bot-Units haben kein Startlimit (`StartLimitIntervalSec=0`): Kommt der Router oder DNS später hoch als der Container, scheitern die ersten Starts, und systemd versucht es alle 10 Sekunden weiter, statt die Unit auf `failed` zu setzen.

Die USV hängt per USB am Host. Der Container braucht nichts von NUT zu wissen.

## 1. Pakete installieren

Auf dem PVE-Host als root:

```bash
apt-get update
apt-get install -y nut
```

Das Metapaket zieht `nut-server` (Treiber und `upsd`) und `nut-client` (`upsmon`, `upsc`) nach.

## 2. USV erkennen

```bash
nut-scanner -U
```

Die Ausgabe liefert einen fertigen `[nutdev1]`-Block mit `driver`, `port`, `vendorid`, `productid`. Bei fast allen USB-USVs (APC, Eaton, CyberPower) ist der Treiber `usbhid-ups`. Fehlt `nut-scanner`, hilft `lsusb`.

## 3. Konfiguration

Alle Dateien liegen in `/etc/nut/`. Passe die Passwörter an.

`/etc/nut/nut.conf`:

```ini
MODE=standalone
```

`/etc/nut/ups.conf`:

```ini
[ups]
    driver = usbhid-ups
    port = auto
    desc = "USV am Proxmox-Host"
    # Optional, wenn die USV "low battery" zu spät meldet:
    # override.battery.charge.low = 30
    # override.battery.runtime.low = 300
```

`/etc/nut/upsd.conf`:

```ini
LISTEN 127.0.0.1 3493
```

`/etc/nut/upsd.users`:

```ini
[upsmon]
    password = HIER_EIN_PASSWORT
    upsmon primary
```

`/etc/nut/upsmon.conf` (die relevanten Zeilen; der Rest der Datei bleibt bei den Defaults):

```ini
MONITOR ups@localhost 1 upsmon HIER_EIN_PASSWORT primary
SHUTDOWNCMD "/sbin/shutdown -h +0"
POWERDOWNFLAG /etc/killpower
POLLFREQ 5
POLLFREQALERT 5
FINALDELAY 5
```

Rechte setzen und Dienste starten:

```bash
chown root:nut /etc/nut/*.conf /etc/nut/upsd.users
chmod 640 /etc/nut/*.conf /etc/nut/upsd.users
systemctl enable --now nut-driver-enumerator.service nut-server.service nut-monitor.service
upsc ups@localhost
```

`upsc` muss Werte wie `battery.charge`, `battery.runtime` und `ups.status: OL` (on line) zeigen.

## 4. Was passiert bei Stromausfall

1. Die USV meldet `OB` (on battery). `upsmon` schreibt ins Journal und wartet.
2. Meldet die USV zusätzlich `LB` (low battery), setzt `upsmon` das FSD-Flag (forced shutdown), schreibt `POWERDOWNFLAG` und ruft `SHUTDOWNCMD`.
3. `shutdown -h +0` fährt den Host herunter. `pve-guests.service` stoppt die Container in umgekehrter `startup`-Reihenfolge. Der Bot-CT hat `order=20`, ein Monitoring-CT mit kleinerer Nummer würde erst danach gestoppt.
4. Im CT stoppt systemd `freqtrade-dryrun.service` mit SIGTERM. Freqtrade schließt die SQLite-DB sauber. `TimeoutStopSec` in der Unit ist 90 Sekunden.
5. Ganz am Ende schickt NUT (`/lib/systemd/system-shutdown/nutshutdown`) den Befehl, die USV abzuschalten. Kommt der Strom zurück, schaltet die USV die Last wieder ein und der Host bootet, wenn im BIOS "Power on after AC loss" aktiv ist.

Prüfe Punkt 5 im BIOS. Ohne diese Einstellung bleibt der Host nach dem Ausfall aus, und der Bot ist offline, bis Du ihn einschaltest.

Wichtig: Der Host fährt erst bei `LB` herunter, nicht schon bei `OB`. Wenn Deine USV `LB` erst bei wenigen Prozent Restladung meldet, reicht die Zeit für den Shutdown eventuell nicht. Dann `override.battery.charge.low` in `ups.conf` auf 30 bis 40 setzen. Zusätzlich kannst Du in `upsmon.conf` mit `NOTIFYCMD` ein Skript hinterlegen, das bei `ONBATT` nach einer festen Zeit `upsmon -c fsd` auslöst. Rechne konservativ: Der Shutdown des Hosts mit Containern dauert 1 bis 2 Minuten.

## 5. Testablauf

Teste in dieser Reihenfolge. Jeder Schritt wird gefährlicher als der vorherige.

1. Status lesen: `upsc ups@localhost ups.status` zeigt `OL`.
2. Netzstecker der USV ziehen. Nach wenigen Sekunden zeigt `upsc` `OB DISCHRG`, und `journalctl -u nut-monitor -f` meldet "on battery". Stecker wieder einstecken, Status geht zurück auf `OL CHRG`.
3. Shutdown-Kette ohne Stromausfall: `upsmon -c fsd`. Das ist ein echter Shutdown des Hosts mit allen Containern. Vorher prüfen, dass kein Backup läuft. Danach beobachten, ob der Host von selbst wieder hochfährt (USV schaltet die Last aus und ein) und ob der CT und der Bot zurückkommen:
   - Host: `pct list` zeigt den CT als `running`.
   - Im CT: `systemctl status freqtrade-dryrun.service` ist `active (running)`, `systemctl --failed` ist leer, `btctrader-guard status` zeigt keine `killswitch.lock`. Steht eine Unit auf `failed`: `systemctl reset-failed <unit> && systemctl start <unit>`, Ursache im Journal lesen.
   - Telegram: Freqtrade schickt beim Start eine Nachricht.
4. Voller Test: Stecker ziehen und liegen lassen, bis die USV `LB` meldet. Das dauert je nach USV 10 bis 60 Minuten. Der Host muss herunterfahren, bevor die USV abschaltet. Danach Stecker einstecken und die Rückkehr wie in Schritt 3 prüfen.

Wiederhole Schritt 2 einmal im Quartal und Schritt 3 einmal im Jahr. Batterien altern; `battery.runtime` in `upsc` zeigt, wie viel Zeit noch bleibt.

## 6. Fehlerbilder

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `upsc` meldet "Driver not connected" | Treiber läuft nicht oder falsche USB-Rechte | `systemctl status nut-driver@ups`, `journalctl -u nut-driver@ups`; udev-Regeln liefert das Paket, nach dem Umstecken `udevadm trigger` |
| `upsmon` meldet "Poll UPS ... failed" | `upsd` lauscht nicht oder Passwort falsch | `systemctl status nut-server`, `upsd.users` und `upsmon.conf` vergleichen |
| Host fährt bei `LB` nicht herunter | `upsmon` läuft nicht als Primary oder `SHUTDOWNCMD` fehlt | `upsmon.conf` prüfen, `systemctl status nut-monitor` |
| Host bleibt nach Stromrückkehr aus | BIOS-Einstellung fehlt oder USV schaltet die Last nicht durch | BIOS "Power on after AC loss" aktivieren, USV-Handbuch zu "load segment" lesen |
| CT startet, Bot nicht | Unit nicht enabled, oder Unit steht auf `failed` | im CT `systemctl enable freqtrade-dryrun.service`; bei `failed`: `systemctl reset-failed freqtrade-dryrun && systemctl start freqtrade-dryrun` |
