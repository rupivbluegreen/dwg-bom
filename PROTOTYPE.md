# Prototype plan: dwg-bom on a MacBook Pro M1 Pro, 32 GB

The goal is to prove, on your own machine and drawings, that the pipeline:

1. reads real DWGs completely,
2. never puts made-up data in the BOM, and
3. produces a draft that saves an estimator time.

Budget: about half a day for phases 0-3, then one to two days of real drawings.

## Memory budget (32 GB)

| What | Approx. memory |
|---|---|
| macOS + everyday apps | 6-8 GB (estimate; close browsers with many tabs) |
| Qwen3.8-27B in Ollama (`qwen3.8:27b-mlx`, 4-bit + vision encoder) | ~18 GB |
| Model context at 8K tokens | well under 1 GB (this model's hybrid design keeps it small) |
| This pipeline (Python, one drawing) | < 1 GB |
| **Total** | **~25-27 GB** |

By default macOS lets the GPU use about 21 GB on a 32 GB Mac. The model plus its
context (~19 GB) fits under that, but with little room to spare. `start_ollama.sh`
and `doctor.py` both check that the model is 100% in GPU memory. Keep **Activity
Monitor -> Memory** open during phases 2-3. If *Memory Pressure* turns red, see
Troubleshooting.

---

## Phase 0 - Setup (~30 min)

```bash
unzip dwg-bom.zip && cd dwg-bom
./setup_mac.sh
```

The script installs LibreDWG and Python through Homebrew, creates `.venv`, installs
ezdxf and openpyxl, runs the unit tests, and builds a BOM from the bundled sample.

**Done when:** the script ends with "Setup complete" and `out/sample_extraction_BOM.xlsx` opens in Excel or Numbers.

## Phase 1 - Real extraction, no AI (~1 h)

This is the most important test: the DXF reader has not yet been run on a real file.

```bash
source .venv/bin/activate
python tests/validate_a05.py "/path/to/architectural_-_annotation_scaling_and_multileaders.dwg"
```

This compares what the pipeline pulls out of your DWG with what the file is known
to contain (24 key notes, dimension values, profile blocks C250x23 / W250x33,
viewport scales, the two OSB unit errors).

**Done when:** `0 failed`. WARN lines are acceptable.

**If something fails:**

1. Send me `out/validate_a05_report.txt`. It lists every note that was extracted, so I can see what the reader missed and fix it.
2. Try the other converter. Install ODA File Converter, set `CONVERTER = "oda"` in `config.py`, and re-run. If ODA passes and LibreDWG doesn't, it's a LibreDWG gap rather than a bug in our code.

## Phase 2 - Local model with Ollama (~1 h, mostly the 18 GB download)

1. Install **Ollama** from https://ollama.com/download (the Mac app) and open it once.
   A llama icon appears in the menu bar; that means its server is running.
2. Close memory-hungry apps. Then, in the `dwg-bom` folder:
   ```bash
   ./start_ollama.sh
   .venv/bin/python3 doctor.py
   ```

`start_ollama.sh` does three things:

- downloads `qwen3.8:27b-mlx`, the Apple Silicon build (first time only, ~18 GB)
- stops any other loaded models
- loads this one with an 8K context and shows `ollama ps`

The PROCESSOR column there should read **100% GPU**.

`doctor.py` then checks the machine, the converter and Ollama. It measures load time
and generation speed, confirms the model is fully in GPU memory, and runs the real
task on two A-05 notes (`W12`, `CABLES`), guard included.

The pipeline talks to Ollama's native API so that it can:

- switch Qwen3.8's thinking mode **off** (it is on by default and can think for minutes on a trivial prompt)
- constrain replies to the BOM's JSON schema
- keep the model loaded for 30 minutes between runs

**Done when:** `doctor.py` shows no FAIL lines, speed is at least 4 tokens/s, the model is 100% in GPU memory, and the classification step answers 2/2.

When you're finished for the day, `ollama stop qwen3.8:27b-mlx` frees the ~18 GB immediately.

<details><summary>Using LM Studio instead</summary>

Download Qwen3.8 27B (MLX 4-bit) in LM Studio. Then run `./start_model.sh <model-key>`,
using the key shown by `lms ls`. In `config.py`, set:

```python
"provider": "openai", "base_url": "http://localhost:1234/v1", "model": "auto"
```
</details>

## Phase 3 - End to end on A-05 (~15 min)

```bash
python run.py "/path/to/architectural_-_annotation_scaling_and_multileaders.dwg"
open out/*_BOM.xlsx
```

**Done when:**

- [ ] about 21 rule lines + 2 LLM lines
- [ ] OSB row shows `FIX unit error`
- [ ] `W410` shows `needs mass/m`
- [ ] `C250` / `W250` show an inferred mass with `confirm mass`
- [ ] no row contains a number that isn't on the drawing
- [ ] total run under 3 minutes (see `out/runs.csv`; the first run also includes model loading)

## Phase 4 - Your drawings (1-2 days)

Pick 5-10 real drawings, as a mix: sections and details, a plan, and ideally one
sheet with a schedule. Run them as a batch:

```bash
python run.py ~/Projects/some-job/*.dwg --out out/trial1
```

`out/trial1/runs.csv` logs counts and timings for every drawing automatically.
For each sheet, also have the estimator note:

| Drawing | Notes missed | Wrong materials | Wrong flags | Minutes to finish BOM (with tool) | Minutes (manual, estimate) |
|---|---|---|---|---|---|

Most early problems will be notation the rules don't recognise yet: your office's
abbreviations, other steel standards, product names. Those notes show up in the
*Notes* sheet as `LLM ...` or `unmatched`. Collect them. Each one becomes a small
rule plus a test in `rules.py`, and the LLM share shrinks over time.

## Phase 5 - Decide

| Criterion | Target |
|---|---|
| Notes extracted | >= 95% of notes on the sheet |
| Invented data in BOM | 0 (the guard must catch every case) |
| Unit/notation errors caught | all that the estimator finds by hand |
| Time per sheet on the M1 Pro | < 5 min |
| Estimator verdict | the draft is faster than starting from zero |

If these hold, likely next steps are:

- rules for your notation
- reading schedules and tables, which often contain real quantities
- merging several sheets into one project BOM
- measuring member lengths from plan views

## Troubleshooting

| Symptom | Fix |
|---|---|
| Notes missing, or `dwg2dxf` warnings | `CONVERTER = "oda"` in `config.py`; send the validate report |
| `ollama ps` shows a CPU/GPU split, or memory pressure is red | close apps and re-run `./start_ollama.sh`. Still split? Try in turn: `CTX=4096 ./start_ollama.sh` with `"context_length": 4096` in `config.py`; the smaller model `./start_ollama.sh qwen3:14b` with `"model": "qwen3:14b"` in `config.py`; as a last resort `sudo sysctl iogpu.wired_limit_mb=24576` (resets on reboot) |
| "cannot reach http://localhost:11434" | open the Ollama app (menu-bar llama), or run `./start_ollama.sh` |
| "model ... is not downloaded" | `./start_ollama.sh <model>`, or fix `"model"` in `config.py` to match `ollama list` |
| First run of the day is slow | normal: loading 18 GB takes 20-60 s. `keep_alive` keeps it loaded for 30 min after |
| LLM stage times out | lower `batch_size` in `config.py` to 10; check `ollama ps` shows 100% GPU |
| "model did not return JSON" | keep `temperature` at 0; update Ollama (older versions ignore the thinking switch) |
| Mac feels slow after you're done | `ollama stop qwen3.8:27b-mlx` |
