# vLLM auf dem GPU-Host

Kurzfassung. Die ausführliche Anleitung mit Modellwahl, Schattenmodus und Auswertung steht in `docs/VLLM_ADVISOR.md`.

## Voraussetzungen

- Linux-Host mit NVIDIA-GPU, aktuellem Treiber und Docker samt NVIDIA Container Toolkit (`nvidia-smi` und `docker run --gpus all nvidia/cuda:12.9.0-base-ubuntu24.04 nvidia-smi` funktionieren).
- Platz für die Gewichte unter `HF_CACHE_DIR` (14B AWQ: ca. 10 GB, 14B FP8: ca. 16 GB, 32B FP8: ca. 34 GB).
- Geprüft mit `vllm/vllm-openai:v0.29.0` (Stand 15.09.2026).

## Start

```bash
cd deploy/vllm
cp .env.example .env
# VLLM_MODEL und VLLM_API_KEY setzen
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
- Der API-Key schützt nur vor Zufallszugriffen. Öffne den Port nie ins Internet.
- Der GPU-Host bekommt keine Börsen-Keys. Er sieht nur Kerzendaten.
