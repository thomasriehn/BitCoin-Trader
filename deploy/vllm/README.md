# vLLM auf dem GPU-Host

Kurzfassung. Die ausführliche Anleitung mit Modellwahl, Schattenmodus und Auswertung steht in `docs/VLLM_ADVISOR.md`.

## Voraussetzungen

- Linux-Host mit NVIDIA-GPU, aktuellem Treiber und Docker samt NVIDIA Container Toolkit (`nvidia-smi` und `docker run --gpus all nvidia/cuda:12.9.0-base-ubuntu24.04 nvidia-smi` funktionieren).
- Platz für die Gewichte unter `HF_CACHE_DIR` (14B AWQ: ca. 10 GB, 14B FP8: ca. 16 GB, 32B FP8: ca. 34 GB).
- Geprüft mit `vllm/vllm-openai:v0.29.0` (Stand 15.09.2026: neueste Version auf PyPI, Docker-Tag vorhanden).

## Start

```bash
cd deploy/vllm
cp .env.example .env
sed -i "s/^VLLM_API_KEY=.*/VLLM_API_KEY=$(openssl rand -hex 32)/" .env
$EDITOR .env                     # VLLM_MODEL prüfen
grep -q CHANGE_ME .env && echo "VLLM_API_KEY ist noch der Platzhalter, erst setzen"
docker compose up -d
docker compose logs -f vllm      # erster Start lädt die Gewichte, das dauert
curl -fsS http://127.0.0.1:8000/health && echo OK
```

## Test-Aufruf

```bash
source .env
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"advisor","messages":[{"role":"user","content":"Answer with JSON: {\"ok\": true}"}],
       "max_tokens":50,"temperature":0,"seed":42,
       "chat_template_kwargs":{"enable_thinking":false},
       "response_format":{"type":"json_schema","json_schema":{"name":"t","schema":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}}}}'
```

Die Antwort muss in `choices[0].message.content` genau `{"ok": true}` enthalten.

## Hardware-Kurzliste

| VRAM | Modell (`VLLM_MODEL`) |
|---|---|
| 12 GB | `Qwen/Qwen3-8B-AWQ` |
| 16 GB | `Qwen/Qwen3-14B-AWQ` |
| 24 GB | `Qwen/Qwen3-14B-FP8` (entspannt) oder `Qwen/Qwen3-32B-AWQ` (knapp) |
| 48 GB | `Qwen/Qwen3-32B-FP8` oder `Qwen/Qwen3.5-27B-FP8` |

Details und Alternativen (Gemma 4, Mistral Small 3.2): `docs/VLLM_ADVISOR.md`.

## Sicherheit

- Der Port ist standardmäßig nur auf `127.0.0.1` gebunden. Zugriff aus dem Trading-Container über Tailscale (`VLLM_BIND` auf die Tailscale-IP setzen) oder über einen SSH-Tunnel.
- Der API-Key schützt nur vor Zufallszugriffen. Öffne den Port nie ins Internet. Der Key geht als Umgebungsvariable `VLLM_API_KEY` in den Container, nicht als `--api-key` auf der Kommandozeile (die steht in `ps aux` und `docker inspect`). Compose prüft nur, dass der Wert nicht leer ist; mit dem Platzhalter `CHANGE_ME...` würde der Server mit einem öffentlich bekannten Key laufen. Darum die `grep`-Zeile im Start-Abschnitt.

## Tailscale und Reboot

Wenn `VLLM_BIND` die Tailscale-IP ist: Docker bindet den Port beim Containerstart, und nach einem Reboot ist `tailscale0` oft noch nicht da, wenn `dockerd` den Container hochfährt. Symptom: `cannot assign requested address` in `docker compose logs vllm`, der Advisor meldet stündlich `connection refused`, bis jemand `docker compose up -d` ausführt. Eine der drei Abhilfen reicht:

1. `sudo systemctl edit docker.service` und eintragen: `[Unit]`, `After=tailscaled.service`, `Wants=tailscaled.service`.
2. `VLLM_BIND=0.0.0.0` lassen und Port 8000 per Firewall auf das Tailscale-Interface beschränken: `ufw allow in on tailscale0 to any port 8000` und `ufw deny 8000`.
3. Port lokal lassen und vom Trading-Container aus einen SSH-Tunnel nutzen.
- Der GPU-Host bekommt keine Börsen-Keys. Er sieht nur Kerzendaten.
