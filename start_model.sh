#!/usr/bin/env bash
# Start LM Studio's local server and load one model for the pipeline.
#   ./start_model.sh <model-key>              # keys: lms ls
#   CTX=16384 ./start_model.sh <model-key>    # larger context (uses more memory)
# Unloads any other loaded models first: on a 32 GB Mac two models will not fit.
set -euo pipefail
MODEL_KEY="${1:-}"
CTX="${CTX:-8192}"

if ! command -v lms >/dev/null; then
  echo "lms (LM Studio's CLI) not found. Open LM Studio once; the CLI is usually at ~/.lmstudio/bin/lms."
  exit 1
fi
if [[ -z "$MODEL_KEY" ]]; then
  echo "Usage: $0 <model-key>"; echo; echo "Downloaded models:"; lms ls; exit 1
fi

lms server start
lms unload --all || true
lms load "$MODEL_KEY" --gpu max --context-length "$CTX" --identifier bom-model
lms ps
echo
echo "Loaded as 'bom-model' (context $CTX) at http://localhost:1234/v1"
echo "Check it with: python doctor.py"
