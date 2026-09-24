"""Pipeline settings. Edit here, or override the common ones with command-line flags (see run.py --help)."""

# --- DWG -> DXF conversion -------------------------------------------------
CONVERTER = "libredwg"          # "libredwg" (open source, `brew install libredwg`) or "oda" (ODA File Converter, free but closed)
DXF_CACHE_DIR = "out/dxf"       # converted DXF files are cached here and reused if newer than the DWG

# --- Local LLM -------------------------------------------------------------
# Default: Ollama (open source, MIT). To use LM Studio instead:
#   "provider": "openai", "base_url": "http://localhost:1234/v1", "model": "auto"
LLM = {
    "enabled": True,
    "provider": "ollama",                     # "ollama" (native API) or "openai" (LM Studio, mlx-lm, llama.cpp)
    "base_url": "http://localhost:11434",     # Ollama's default address
    "model": "qwen3.8:27b-mlx",               # Apple Silicon build, ~18 GB. Smaller fallback: "qwen3:14b"
    "api_key": "local",                       # local servers ignore this
    "temperature": 0.0,
    "max_tokens": 4000,
    "context_length": 8192,                   # Ollama: tokens of context; keep equal to CTX in start_ollama.sh
    "keep_alive": "30m",                      # Ollama: keep the model loaded 30 min after the last request
    "timeout_s": 900,                         # first request includes loading the model; be patient
    "batch_size": 30,                         # notes per request
}

# --- Extraction --------------------------------------------------------------
# Layers whose linework lengths are totalled (glob patterns, case-insensitive).
LENGTH_LAYERS = ["Struc*", "Structural*", "*Handrail*", "*Stair*"]
# Layers ignored when collecting notes and linework.
IGNORE_LAYERS = ["Defpoints", "PS_Viewport", "TB_*", "Title*"]
# How deep to follow blocks nested inside blocks.
MAX_BLOCK_DEPTH = 8

# --- Rules -------------------------------------------------------------------
# Imperial/metric pairs such as 3/4" [19] are checked; a pair is flagged when the
# difference is larger than both of these.
UNIT_CHECK_TOLERANCE_MM = 1.5
UNIT_CHECK_TOLERANCE_PCT = 0.02

# Categories listed here are kept, but marked "reference" in the BOM because on
# stair/detail sheets they usually describe surrounding construction.
REFERENCE_CATEGORIES = ["Roofing", "Wood framing & finishes"]
