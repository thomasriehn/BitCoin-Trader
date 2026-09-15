# Advisor mit lokalem vLLM

Stand: 15.09.2026. Geprüft gegen vLLM **v0.29.0** (Release vom 09.09.2026, Docker-Image `vllm/vllm-openai:v0.29.0`). Modell-Lizenzen und Hugging-Face-IDs wurden am selben Tag über die Hugging-Face-API geprüft.

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
- Der Nutzen des Experiments liegt in der sauberen Protokollierung: Jede Entscheidung ist mit Kontext-Hash und Prompt-Hash reproduzierbar.

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

- `--max-model-len 8192`: Der Advisor-Prompt hat rund 3.000 Token (System-Prompt plus 30 Tages- und 24 4h-Kerzen), die Antwort maximal 400. 8k ist reichlich und spart KV-Cache.
- `--api-key`: Der Server verlangt `Authorization: Bearer <Key>`. Derselbe Wert steht auf dem Trading-Host in `ADVISOR_API_KEY`.
- `--seed 42` und `temperature 0` im Request: reproduzierbar, soweit das die GPU zulässt. Kleine Abweichungen zwischen Läufen sind normal (Batching, Kernel-Auswahl).
- `--served-model-name advisor`: Der Advisor spricht das Modell als `advisor` an (`ADVISOR_MODEL=advisor`). Beim Modellwechsel musst du nur `.env` auf dem GPU-Host ändern.
- `--structured-outputs-config.backend auto`: Standard in vLLM 0.29. `auto` wählt je nach Schema `xgrammar` oder `guidance`. Bei Problemen explizit `guidance` setzen.
- `--reasoning-parser qwen3`: nur relevant, wenn Thinking an ist. Für Qwen3/Qwen3.5 korrekt, für Mistral ohne Wirkung.

Netzwerk: Der Port ist auf `127.0.0.1` gebunden. Wenn der Trading-Container auf einem anderen Host läuft, setze `VLLM_BIND` auf die Tailscale-IP des GPU-Hosts und `ADVISOR_BASE_URL=http://<tailscale-ip>:8000/v1`. Nie ins Internet öffnen.

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

Fallback: Antwortet der Server mit HTTP 400 und die Meldung nennt `response_format`, `guided` oder `structured`, wiederholt der Advisor den Aufruf einmal ohne `response_format` und mit dem Feld `guided_json` (alte vLLM-Versionen vor 0.12). vLLM 0.29 kennt `guided_json` nicht mehr, ignoriert das Feld und antwortet ohne Grammatik-Zwang. Die Antwort wird in jedem Fall mit Pydantic geprüft. Ob der Fallback genutzt wurde, steht in `decisions.jsonl` unter `used_guided_json_fallback`.

Das exportierte JSON-Schema enthält Enum, Typen, `required` und `additionalProperties: false`, aber keine Zahlenbereiche (die vertragen nicht alle Backends). Bereiche wie `confidence` 0 bis 1 und `horizon_days` 1 bis 30 prüft der Code. Fällt die Prüfung durch, zählt das als Fehler.

## Schattenmodus

`ADVISOR_MODE=shadow` ist der Default. Der systemd-Timer `btctrader-advisor.timer` ruft `btctrader-advisor run` stündlich auf. Der Advisor:

1. holt 400 Tageskerzen und 24 4h-Kerzen BTC-EUR von der öffentlichen Bitvavo-API,
2. berechnet SMA50/200 mit Abstand in Prozent, realisierte 30-Tage-Volatilität (annualisiert), Returns über 1/7/30/90 Tage, Drawdown vom 365-Tage-Hoch und den Volumen-Trend (7 Tage gegen 30 Tage davor),
3. fragt optional die Freqtrade-API nach offenen Positionen (Fehler dort werden übersprungen, `bot.available=false`),
4. schreibt den Kontext nach `ADVISOR_DIR/context-latest.json`,
5. ruft das Modell auf, prüft die Antwort,
6. hängt eine Zeile an `decisions.jsonl` und schreibt `decision.json` atomar (tmp-Datei plus `os.replace`).

`decision.json` enthält `valid_until = created_at + 2 x ADVISOR_INTERVAL_HOURS`. Die Strategie `BtcAdvisorGated` liest die Datei nur bei `mode == "gate"` und nur, solange `valid_until` nicht abgelaufen ist. Im Schattenmodus passiert also nichts, egal was das Modell sagt.

Fehlerfall (Server nicht erreichbar, Timeout, ungültiges JSON, Wert außerhalb des Bereichs): Exit-Code 1, Log-Zeile mit `error=...`, eine Zeile mit `error` in `decisions.jsonl`, `decision.json` bleibt unverändert. Kein Alarm bei Einzelfehlern. Im Gate-Modus alarmiert der Guard, wenn `decision.json` älter als 3 x Intervall ist.

Wechsel in den Gate-Modus: `ADVISOR_MODE=gate` in `/etc/freqtrade/btctrader.env`, Timer neu starten, Strategie `BtcAdvisorGated` im Dry-Run laufen lassen. Erst nach Wochen im Dry-Run mit Auswertung an Live denken.

## decisions.jsonl lesen

Eine Zeile pro Aufruf, JSON. Erfolgreiche Zeilen haben alle Felder von `decision.json` plus `latency_ms`, `usage` (Token), `raw_response` (auf 4.000 Zeichen gekürzt), `used_guided_json_fallback` und `error: null`. Fehlerzeilen haben `error` gesetzt und keine Regime-Felder.

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

Die Datei bleibt 10 Jahre erhalten (Backup wie das Ledger). `context_hash` und `prompt_hash` erlauben es später, denselben Kontext mit einem anderen Modell erneut zu bewerten.

## Auswertung mit evaluate

```bash
btctrader-advisor evaluate --since 2026-09-01
btctrader-advisor evaluate --since 2026-09-01 --json
```

Der Befehl liest `decisions.jsonl`, holt die Tageskerzen ab dem ersten Eintrag und verbindet jede Entscheidung mit der Folge-Rendite (Schluss zu Schluss) über 1 Tag, 7 Tage und den vom Modell genannten Horizont (`own`). Treffer-Definition:

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
