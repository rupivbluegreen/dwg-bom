#!/usr/bin/env bash
# Download (first time only) and load the model in Ollama for the pipeline.
#   ./start_ollama.sh                     # qwen3.8:27b-mlx with an 8K context
#   ./start_ollama.sh qwen3:14b           # smaller fallback model (then set it in config.py too)
#   CTX=4096 ./start_ollama.sh            # smaller context if memory is tight (match config.py)
# Stops any other loaded models first: two will not fit in 32 GB.
set -euo pipefail
MODEL="${1:-qwen3.8:27b-mlx}"
CTX="${CTX:-8192}"
URL="http://localhost:11434"

if ! command -v ollama >/dev/null; then
  echo "Ollama not found. Install the app from https://ollama.com/download, open it once, then re-run."
  exit 1
fi

if ! curl -fsS "$URL/api/version" >/dev/null 2>&1; then
  echo "Starting Ollama..."
  open -a Ollama 2>/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
  for _ in $(seq 1 30); do curl -fsS "$URL/api/version" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -fsS "$URL/api/version" >/dev/null 2>&1 || { echo "Ollama is not responding on $URL"; exit 1; }
echo "Ollama $(curl -fsS "$URL/api/version" | sed 's/.*"version":"\([^"]*\)".*/\1/')"

if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
  echo "Downloading $MODEL (about 18 GB for the default model; first time only)..."
  ollama pull "$MODEL"
fi

for m in $(ollama ps | awk 'NR>1 {print $1}'); do
  if [[ "$m" != "$MODEL" ]]; then echo "Stopping $m to free memory"; ollama stop "$m"; fi
done

echo "Loading $MODEL with a $CTX-token context (can take a minute)..."
curl -fsS "$URL/api/generate" \
  -d "{\"model\":\"$MODEL\",\"keep_alive\":\"30m\",\"options\":{\"num_ctx\":$CTX}}" >/dev/null
echo
ollama ps
echo
echo "PROCESSOR should read '100% GPU'. If it shows a CPU/GPU split, memory is too tight."
echo "Next: .venv/bin/python3 doctor.py"
echo "Free the memory when done: ollama stop $MODEL"
