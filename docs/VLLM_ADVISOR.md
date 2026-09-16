# Advisor mit lokalem vLLM

Stand: 15.09.2026. Geprüft gegen vLLM **v0.29.0** (aktuelle Version auf PyPI, veröffentlicht am 09.09.2026; Docker-Image `vllm/vllm-openai:v0.29.0` existiert auf Docker Hub). Modell-Lizenzen und Hugging-Face-IDs wurden am selben Tag über die Hugging-Face-API geprüft.

## Was am 15.09.2026 geprüft wurde

- vLLM 0.29.0 ist die neueste Version auf PyPI (Upload 09.09.2026). Das Docker-Tag `v0.29.0` existiert.
- Im Quelltext (`vllm/entrypoints/openai/chat_completion/protocol.py`, Branch `main`): `ChatCompletionRequest` erlaubt unbekannte Felder (`extra="allow"`) und loggt sie nur. `response_format` vom Typ `json_schema` nimmt `name`, `schema` und `strict`. Das aktuelle Feld für direkte Grammatik-Vorgaben heißt `structured_outputs` (`{"json": ...}`), die alten `guided_*`-Felder wurden in v0.12.0 (03.12.2025) entfernt.
- `max_tokens` ist in vLLM als veraltet markiert (Nachfolger `max_completion_tokens`), wird aber weiter angenommen. Der Advisor sendet `max_tokens: 400`, wie in `docs/KOMPONENTEN.md` festgelegt.
- `--structured-outputs-config.backend` kennt `auto`, `xgrammar`, `guidance`, `outlines` und `lm-format-enforcer` (Quelle: `vllm/config/structured_outputs.py`).
- Alle unten genannten Hugging-Face-IDs antworten mit HTTP 200, sind nicht gated und tragen `license: apache-2.0` in der Modellkarte.

## Was der Advisor tut

Der Advisor ist ein Experiment. Ein lokales Sprachmodell bekommt einmal pro Stunde den Marktkontext von BTC/EUR als JSON und antwortet mit genau einem JSON-Objekt: `regime` (`risk_on`, `neutral`, `risk_off`), `confidence` (0 bis 1), `horizon_days` (1 bis 30), `rationale` (max. 500 Zeichen) und bis zu fünf `key_factors`.

Was er nicht tut:

- Er hat keine Börsen-Keys. Er sieht nur öffentliche Kerzendaten und optional den Bot-Status (Position ja/nein, unrealisierter Gewinn) aus der Freqtrade-API.
- Er ruft nie Order-Endpunkte auf. Er schreibt nur zwei Dateien.
- Er entscheidet nie über Ordergrößen, Hebel oder Preise. Der System-Prompt verbietet das ausdrücklich, und die Strategie liest nur das Feld `regime`.
- Er bekommt keine Nachrichten und keine externen Feeds. Nur Zahlen aus dem Kontext.

## Erwartungsmanagement

Kurz und ehrlich: Es gibt keine belastbaren Belege, dass Sprachmodelle Kursrichtungen vorhersagen können. Studien zu LLM-Prognosen auf Finanzzeitreihen finden bestenfalls Ergebnisse auf dem Niveau einfacher Regeln, und Modelle neigen dazu, den letzten Trend fortzuschreiben. Ein Regime-Klassifikator auf denselben Indikatoren, die die Strategie ohnehin nutzt, kann kein Wissen hinzufügen, das nicht schon im SMA200-Filter steckt.

Darum:

- Der Advisor läuft zuerst im **Schattenmodus** (`ADVISOR_MODE=shadow`). Er loggt nur. Die Strategie ignoriert ihn.
- Erst wenn `btctrader-advisor evaluate` über mehrere Monate eine Trefferquote zeigt, die deutlich über der Basisrate liegt, lohnt ein Test im Gate-Modus im Dry-Run. Auch dann blockt er nur neue Entries bei `risk_off`. Exits blockt er nie.
- Rechne damit, dass die Auswertung nahe 50 % landet. Das ist das erwartete Ergebnis, kein Fehler.
- Der Nutzen des Experiments liegt in der sauberen Protokollierung: Jede Entscheidung trägt Kontext-Hash und Prompt-Hash. Der Kontext des letzten Laufs liegt als `context-latest.json` daneben.

## Hardware und Modellwahl

Anforderungen an das Modell: Instruct-Modell, gute JSON-Treue, permissive Lizenz (Apache 2.0), läuft in vLLM 0.29. Alle Modelle unten stehen unter Apache 2.0 und sind auf Hugging Face nicht gated (geprüft 15.09.2026). Hybrid-Thinking-Modelle (Qwen3, Qwen3.5, Gemma 4) laufen mit `enable_thinking=false`; der Advisor schickt das bei jedem Aufruf mit.

Faustregel für den Speicherbedarf: Gewichte in GB ungefähr Parameter in Milliarden x 2 (BF16), x 1 (FP8), x 0,55 (AWQ 4 Bit). Dazu kommen 1 bis 3 GB für KV-Cache bei `--max-model-len 8192` und Laufzeit-Overhead. FP8 braucht eine GPU mit Ada- oder Hopper-Architektur (RTX 40xx, L4, L40S, H100); auf Ampere (RTX 30xx, A10, A100) nimm AWQ oder BF16.

| VRAM | Empfehlung (`VLLM_MODEL`) | Alternative | Anmerkung |
|---|---|---|---|
| 12 GB (RTX 3060 12 GB, RTX 4070) | `Qwen/Qwen3-8B-AWQ` | `Qwen/Qwen3-8B-FP8` (nur Ada/Hopper, knapp) | 8B ist die Untergrenze für stabile JSON-Antworten mit Begründung. |
| 16 GB (RTX 4060 Ti 16 GB, RTX 4080, A4000) | `Qwen/Qwen3-14B-AWQ` | `Qwen/Qwen3.5-9B` (BF16, ca. 18 GB, passt nicht ohne Quantisierung; AWQ-Varianten von Drittanbietern prüfen) | 14B AWQ belegt ca. 10 GB, KV-Cache passt bequem. |
| 24 GB (RTX 3090, RTX 4090, A10, L4) | `Qwen/Qwen3-14B-FP8` (Ada/Hopper) oder `Qwen/Qwen3-14B` (BF16 auf Ampere, ca. 30 GB, passt nicht; dann AWQ) | `Qwen/Qwen3-32B-AWQ` (ca. 19 GB Gewichte, `VLLM_GPU_MEM=0.95`, knapp) | Für Ampere-Karten bleibt `Qwen/Qwen3-14B-AWQ` die sichere Wahl. |
| 48 GB (RTX 6000 Ada, A6000, L40S, 2 x 24 GB mit `VLLM_GPU_COUNT=2` und `--tensor-parallel-size 2`) | `Qwen/Qwen3-32B-FP8` | `Qwen/Qwen3.5-27B-FP8`, `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` (MoE, schnell), `RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8`, `google/gemma-4-12B-it` (BF16, ca. 24 GB) | 32B dicht ist die beste Qualität in dieser Klasse. |

Hinweise zu den Familien:

- **Qwen3** (`Qwen/Qwen3-8B`, `-14B`, `-32B` jeweils mit `-FP8` und `-AWQ` vom Hersteller): Apache 2.0, 32k Kontext nativ, sehr gute JSON-Treue, hybrides Thinking (abschaltbar). Die von Alibaba selbst veröffentlichten Quantisierungen sind die verlässlichste Wahl.
- **Qwen3.5** (`Qwen/Qwen3.5-9B`, `-27B`, `-35B-A3B`, `-122B-A10B`; FP8 vom Hersteller für 27B und 35B-A3B): Apache 2.0, multimodal, 262k Kontext. Thinking ist standardmäßig an und wird per `chat_template_kwargs` abgeschaltet. Bei Problemen mit dem Chat-Template in vLLM zurück auf Qwen3.
- **Gemma 4** (`google/gemma-4-12B-it`, `google/gemma-4-26B-A4B-it`, `google/gemma-4-31B-it`): seit 2026 Apache 2.0, gute Sprachqualität, unified multimodal. Es gibt keine FP8/AWQ-Varianten von Google selbst; Community-Quantisierungen vor dem Einsatz mit dem Test-Aufruf unten prüfen.
- **Mistral Small 3.2** (`mistralai/Mistral-Small-3.2-24B-Instruct-2506`, FP8 von `RedHatAI`): Apache 2.0, kein Thinking, solide JSON-Treue. Braucht in vLLM meist `--tokenizer-mode mistral --config-format mistral --load-format mistral` (siehe Modellkarte). `mistralai/Magistral-Small-2509` ist die Reasoning-Variante und für diesen Zweck nicht nötig.

Nicht empfohlen: Llama-Modelle (eigene Lizenz mit Auflagen), Modelle unter 8B (JSON oft brauchbar, Begründungen nicht), Reasoning-only-Modelle (langsam, Thinking frisst das Token-Budget von 400).

## Start des Servers

Alles liegt in `deploy/vllm/`:

```bash
cd deploy/vllm
cp .env.example .env
$EDITOR .env            # VLLM_MODEL und VLLM_API_KEY setzen
docker compose up -d
docker compose logs -f vllm
```

Der erste Start lädt die Gewichte in `HF_CACHE_DIR`. Danach `HF_HUB_OFFLINE=1` setzen, dann startet der Container ohne Hub-Zugriff.

Wichtige Flags in der Compose-Datei (vLLM 0.29):

- `--max-model-len 8192`: Der Advisor-Prompt hat rund 5.000 Token (gemessen: System-Prompt 2.800 Zeichen, Kontext 5.600 Zeichen mit 2.400 Ziffern; Qwen-Tokenizer zählen jede Ziffer als eigenes Token), die Antwort maximal 400. 8k reicht mit Reserve. Wenn du das Kerzenfenster vergrößerst oder ein Modell mit anderem Tokenizer nimmst, prüfe `usage.prompt_tokens` in `decisions.jsonl`. Überschreitet der Prompt das Limit, antwortet vLLM mit HTTP 400 (`maximum context length`), und jeder Lauf scheitert.
- Antwortlänge: `max_tokens 400` begrenzt die Antwort. Wird sie abgeschnitten (`finish_reason == "length"`), ist das JSON unvollständig. Der Advisor meldet dann `answer truncated at max_tokens=400` im Log und in `decisions.jsonl`; `decision.json` bleibt unverändert.
- API-Key: Der Server verlangt `Authorization: Bearer <Key>`. Der Key kommt als Umgebungsvariable `VLLM_API_KEY` in den Container, vLLM liest sie selbst. Kein `--api-key` auf der Kommandozeile, sonst steht der Key in `ps aux` und `docker inspect`. Derselbe Wert steht auf dem Trading-Host in `ADVISOR_API_KEY`.
- `--seed 42` und `temperature 0` im Request: reproduzierbar, soweit das die GPU zulässt. Kleine Abweichungen zwischen Läufen sind normal (Batching, Kernel-Auswahl).
- `--served-model-name advisor`: Der Advisor spricht das Modell als `advisor` an (`ADVISOR_MODEL=advisor`). Beim Modellwechsel musst du nur `.env` auf dem GPU-Host ändern.
- `--structured-outputs-config.backend auto`: Standard in vLLM 0.29. `auto` wählt je nach Schema `xgrammar` oder `guidance`. Bei Problemen explizit `guidance` setzen.
- `--reasoning-parser qwen3`: nur relevant, wenn Thinking an ist. Für Qwen3/Qwen3.5 korrekt, für Mistral ohne Wirkung.

Netzwerk: Der Port ist auf `127.0.0.1` gebunden. Wenn der Trading-Container auf einem anderen Host läuft, setze `VLLM_BIND` auf die Tailscale-IP des GPU-Hosts und `ADVISOR_BASE_URL=http://<tailscale-ip>:8000/v1`. Nie ins Internet öffnen.

Achtung beim Bind auf die Tailscale-IP: Docker bindet den Port beim Containerstart. Nach einem Reboot startet `dockerd` den Container oft, bevor `tailscale0` da ist. Dann scheitert der Bind (`cannot assign requested address` in `docker compose logs vllm`), Docker versucht es nicht erneut, und der Advisor bekommt bis zum nächsten `docker compose up -d` nur `connection refused`. Abhilfe, eine davon reicht:

- Drop-in für Docker: `sudo systemctl edit docker.service` mit `[Unit]`, `After=tailscaled.service` und `Wants=tailscaled.service`.
- `VLLM_BIND=0.0.0.0` lassen und Port 8000 per Firewall auf `tailscale0` beschränken, z. B. `ufw allow in on tailscale0 to any port 8000` plus `ufw deny 8000`.
- Port lokal lassen und per SSH-Tunnel vom Trading-Container aus erreichen.

## Test mit curl

Health und Modellliste:

```bash
curl -fsS http://127.0.0.1:8000/health && echo OK
curl -sS -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

Strukturierte Antwort mit `response_format` (so ruft der Advisor auf):

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "advisor",
    "messages": [{"role": "user", "content": "Classify: price 8% above SMA200, vol 45%, 7d return -2%. Answer as JSON."}],
    "temperature": 0, "seed": 42, "max_tokens": 200,
    "chat_template_kwargs": {"enable_thinking": false},
    "response_format": {"type": "json_schema", "json_schema": {"name": "regime_decision", "strict": true,
      "schema": {"type": "object", "additionalProperties": false,
        "properties": {"regime": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
                       "confidence": {"type": "number"}},
        "required": ["regime", "confidence"]}}}
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])'
```

Erwartet: ein reines JSON-Objekt mit `regime` und `confidence`, ohne Text davor oder danach.

Vom Trading-Host aus, ohne den Server aufzurufen, den echten Request ansehen:

```bash
btctrader-advisor run --dry-run          # holt den Kontext von Bitvavo, druckt Request und Fallback-Body
btctrader-advisor context --no-bot       # nur den Marktkontext
btctrader-advisor run                    # echter Lauf: schreibt decision.json und decisions.jsonl
```

## Wie der Request aufgebaut ist

`POST {ADVISOR_BASE_URL}/chat/completions` mit `model`, `messages` (System-Prompt aus `btctrader/advisor/prompts/system.md`, dann der Kontext als JSON), `temperature 0`, `seed 42`, `max_tokens 400`, `chat_template_kwargs.enable_thinking=false` und `response_format` vom Typ `json_schema` mit `strict: true`.

Fallback: Antwortet der Server mit HTTP 400 und die Meldung nennt `response_format`, `guided` oder `structured`, wiederholt der Advisor den Aufruf einmal ohne `response_format` und mit dem Feld `guided_json` auf oberster Ebene des Bodys (das ist, was das OpenAI-SDK aus `extra_body.guided_json` macht; alte vLLM-Versionen vor 0.12). vLLM 0.29 kennt `guided_json` nicht mehr, nimmt das unbekannte Feld dank `extra="allow"` trotzdem an, ignoriert es und antwortet ohne Grammatik-Zwang. Die Antwort wird in jedem Fall mit Pydantic geprüft. Ob der Fallback genutzt wurde, steht in `decisions.jsonl` unter `used_guided_json_fallback`.

Das exportierte JSON-Schema enthält Enum, Typen, `required` und `additionalProperties: false`, aber keine Zahlenbereiche (die vertragen nicht alle Backends). Bereiche wie `confidence` 0 bis 1 und `horizon_days` 1 bis 30 prüft der Code. Fällt die Prüfung durch, zählt das als Fehler.

## Schattenmodus

`ADVISOR_MODE=shadow` ist der Default. Der systemd-Timer `btctrader-advisor.timer` ruft `btctrader-advisor run` stündlich auf. Der Advisor:

1. holt 400 Tageskerzen und 25 4h-Kerzen BTC-EUR von der öffentlichen Bitvavo-API. Die jeweils neueste Kerze ist bei Bitvavo noch offen; sie wird abgetrennt und nur als `open_candle_1d` (mit Live-Preis `price_eur`) mitgegeben,
2. berechnet auf den abgeschlossenen Tageskerzen SMA50/200 mit Abstand in Prozent (zum letzten Tagesschluss `last_close_eur`, wie die Strategie), realisierte 30-Tage-Volatilität (annualisiert), Returns über 1/7/30/90 Tage, Drawdown vom 365-Tage-Hoch und den Volumen-Trend (7 Tage gegen 30 Tage davor). Ohne diese Trennung würde die halb gefüllte Tageskerze um 00:07 UTC den Volumen-Trend systematisch nach `falling` ziehen,
3. fragt optional die Freqtrade-API nach offenen Positionen (Fehler dort werden übersprungen, `bot.available=false`),
4. schreibt den Kontext nach `ADVISOR_DIR/context-latest.json`,
5. ruft das Modell auf, prüft die Antwort,
6. schreibt `decision.json` atomar (tmp-Datei plus `os.replace`) und hängt danach eine Zeile an `decisions.jsonl`. Scheitert das Schreiben von `decision.json`, trägt die Zeile `error` und die Entscheidung gilt nicht.

`decision.json` enthält `valid_until = created_at + 2 x ADVISOR_INTERVAL_HOURS`. Die Strategie `BtcAdvisorGated` liest die Datei nur bei `mode == "gate"` und nur, solange `valid_until` nicht abgelaufen ist. Im Schattenmodus passiert also nichts, egal was das Modell sagt.

Fehlerfall (Server nicht erreichbar, Timeout, ungültiges JSON, Wert außerhalb des Bereichs): Exit-Code 1, Log-Zeile mit `error=...`, eine Zeile mit `error` in `decisions.jsonl`, `decision.json` bleibt unverändert. Kein Alarm bei Einzelfehlern. Im Gate-Modus alarmiert der Guard, wenn `decision.json` älter als 3 x Intervall ist.

Wechsel in den Gate-Modus: `ADVISOR_MODE=gate` in `/etc/freqtrade/btctrader.env` setzen, dann `sudo systemctl start btctrader-advisor.service`. Der Service liest die Env-Datei bei jedem Start; der Timer muss nicht neu gestartet werden, er löst aber auch keinen Lauf aus. Ohne den manuellen Start gilt die Änderung erst beim nächsten stündlichen Lauf, weil das Feld `mode` beim Schreiben in `decision.json` kopiert wird und die Strategie nur dieses Feld liest. Dann Strategie `BtcAdvisorGated` im Dry-Run laufen lassen. Erst nach Wochen im Dry-Run mit Auswertung an Live denken.

Gate sofort abschalten: `ADVISOR_MODE=shadow` setzen und `sudo systemctl start btctrader-advisor.service`. Ist vLLM gerade nicht erreichbar, bleibt die alte Datei mit `mode: gate` bis `valid_until` (2 x Intervall) wirksam. Dann `rm /srv/trading/advisor/decision.json`: Die Strategie verhält sich ohne Datei wie `BtcTrend` (fail open, siehe `evaluate_decision` in `btctrend_lib.py`). Der Guard prüft die Frische von `decision.json` nur bei `ADVISOR_MODE=gate`.

## decisions.jsonl lesen

Eine Zeile pro Aufruf, JSON. Erfolgreiche Zeilen haben alle Felder von `decision.json` plus `latency_ms`, `usage` (Token), `raw_response` (auf 4.000 Zeichen gekürzt), `used_guided_json_fallback` und `error: null`. Fehlerzeilen haben `error` gesetzt und in der Regel keine Regime-Felder. Ausnahme: Konnte `decision.json` nicht geschrieben werden, stehen die Regime-Felder mit in der Zeile, aber `error` sagt, dass die Entscheidung nicht aktiv wurde. `error: null` bedeutet immer: `decision.json` wurde geschrieben.

```bash
# letzte fünf Entscheidungen kompakt
tail -n 5 /srv/trading/advisor/decisions.jsonl | python3 -c '
import json,sys
for l in sys.stdin:
    d=json.loads(l); print(d["created_at"], d.get("regime"), d.get("confidence"), d.get("horizon_days"), d.get("error") or "")'

# Verteilung der Regime
python3 -c '
import json,collections
c=collections.Counter(json.loads(l).get("regime") for l in open("/srv/trading/advisor/decisions.jsonl"))
print(c)'
```

Die Datei bleibt 10 Jahre erhalten (Backup wie das Ledger). `prompt_hash` erlaubt es, Entscheidungen mit verschiedenen Prompts getrennt auszuwerten. `context_hash` ist der SHA-256 über den kompletten Kontext inklusive `as_of` und verbindet die Entscheidung mit `context-latest.json`; er ist eine Integritätsprüfung, kein Archiv. Nur der Kontext des letzten Laufs wird aufbewahrt. Willst du alte Entscheidungen mit einem anderen Modell nachrechnen, musst du die Kontexte selbst sichern, z. B. mit einem Cron-Job, der `context-latest.json` nach jedem Lauf unter `decision_id` kopiert.

## Auswertung mit evaluate

```bash
btctrader-advisor evaluate --since 2026-09-01
btctrader-advisor evaluate --since 2026-09-01 --json
```

Der Befehl liest `decisions.jsonl`, holt die abgeschlossenen Tageskerzen ab dem ersten Eintrag (seitenweise, weil Bitvavo pro Aufruf höchstens 1440 Kerzen liefert) und verbindet jede Entscheidung mit der Folge-Rendite über 1 Tag, 7 Tage und den vom Modell genannten Horizont (`own`). Gemessen wird vom Tagesschluss des Entscheidungstages (UTC) zum Tagesschluss `h` Tage später, nicht vom Zeitpunkt des Aufrufs. Die noch offene Kerze von heute zählt nicht. Fehlt für eine Entscheidung eine Kerze, bleibt sie `pending`; reichen die Kerzen der Börse nicht bis zum ersten Eintrag zurück, gibt es eine Warnung im Log. Treffer-Definition:

- `risk_on`: Folge-Rendite > 0
- `risk_off`: Folge-Rendite < 0
- `neutral`: Betrag der Folge-Rendite <= 3 %

Beispielausgabe:

```
decisions: 412  (without complete forward window: 9)
regime    horizon      n  hit rate   mean fwd return
risk_on   1d         180     52.2%            +0.11%
risk_on   7d         176     55.1%            +0.84%
risk_on   own        176     54.0%            +0.79%
neutral   1d         160     71.3%            -0.02%
...
baseline share of positive forward returns: 1d: 51.0%, 7d: 53.4%, own: 53.0%
neutral counts as hit if |return| <= 3%
```

So liest du das:

- Vergleiche die Trefferquote von `risk_on` mit der Basisrate positiver Tage. Liegt sie nicht klar darüber (mindestens 10 Prozentpunkte über Monate, bei über 100 Entscheidungen je Regime), hat das Modell keinen Vorteil.
- `neutral` hat bei ruhigen Märkten von Natur aus hohe Trefferquoten. Das ist kein Können.
- Stündliche Entscheidungen am selben Tag teilen dieselbe Folge-Rendite. Die Stichprobe ist also kleiner, als `n` suggeriert. Rechne grob mit `n / 24` unabhängigen Beobachtungen.
- Der Schattenmodus hat keinen Einfluss auf die Trades. Die Auswertung sagt nichts über Gewinn oder Verlust der Strategie aus.

## Regeln

- Der Advisor bekommt nie Börsen-Keys, weder View-only noch Trade. Der GPU-Host kennt nur den vLLM-API-Key.
- Kein Cloud-LLM im Live-Pfad. Fällt der GPU-Host aus, läuft die Strategie wie `BtcTrend` weiter.
- Prompt-Änderungen ändern `prompt_hash`. Werte Entscheidungen mit verschiedenem Prompt-Hash getrennt aus.
- Modellwechsel: `VLLM_MODEL` in `deploy/vllm/.env` ändern, `docker compose up -d`, dann `btctrader-advisor run --dry-run` und einen echten Lauf prüfen. Das Feld `model` in `decision.json` zeigt den Namen aus `ADVISOR_MODEL` (bei `--served-model-name advisor` also `advisor`); trage den echten Modellnamen im Betriebslog ein.
