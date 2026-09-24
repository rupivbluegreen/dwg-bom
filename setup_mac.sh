#!/usr/bin/env bash
# One-time setup for the dwg-bom prototype on an Apple Silicon Mac.
# Safe to re-run: steps that are already done are skipped.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "This script is for macOS."
[[ "$(uname -m)" == "arm64" ]]  || echo "Warning: this is not an Apple Silicon Mac; the model advice assumes one."

say "Checking Homebrew"
command -v brew >/dev/null || fail "Homebrew not found. Install it from https://brew.sh and re-run this script."

say "Installing LibreDWG and Python (skipped if already installed)"
brew list libredwg >/dev/null 2>&1 || brew install libredwg
brew list python    >/dev/null 2>&1 || brew install python
PY="$(brew --prefix)/bin/python3"
command -v dwg2dxf >/dev/null || fail "dwg2dxf is not on PATH after installing libredwg."
echo "dwg2dxf: $(dwg2dxf --version 2>&1 | head -1)"
echo "python:  $("$PY" --version)"

say "Creating virtual environment in .venv"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
.venv/bin/python -c "import ezdxf, openpyxl; print('ezdxf', ezdxf.__version__, '| openpyxl', openpyxl.__version__)"

say "Running unit tests"
.venv/bin/python tests/test_rules.py

say "Running the pipeline on the bundled sample (no model needed)"
.venv/bin/python run.py tests/sample_extraction.json --no-llm --out out

say "Setup complete"
cat <<'NEXT'
Next:
  source .venv/bin/activate
  python tests/validate_a05.py "/path/to/architectural_-_annotation_scaling_and_multileaders.dwg"
  ./start_ollama.sh                   # needs the Ollama app: https://ollama.com/download
  python doctor.py
NEXT
