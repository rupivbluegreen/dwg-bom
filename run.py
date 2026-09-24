#!/usr/bin/env python3
"""DWG -> draft bill of materials.

    python run.py drawing.dwg                      # full pipeline, local LLM for leftover notes
    python run.py drawing.dwg --no-llm             # rules only (fast, fully deterministic)
    python run.py drawings/*.dwg --out out/        # batch
    python run.py drawing.dxf                      # skip conversion
    python run.py tests/sample_extraction.json     # re-run from a saved extraction
    python run.py drawing.dwg --provider openai --base-url http://localhost:1234/v1 --model auto   # LM Studio

Stages: convert (LibreDWG) -> extract (ezdxf) -> rules -> LLM (optional) -> assemble -> export
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import config
from dwgbom import bom


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help=".dwg, .dxf or extraction .json files")
    ap.add_argument("--out", type=Path, default=Path("out"), help="output folder (default: out/)")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM stage")
    ap.add_argument("--model", help="model id on the LLM server (default from config.py)")
    ap.add_argument("--provider", choices=["ollama", "openai"], help="LLM server type (default from config.py)")
    ap.add_argument("--base-url", help="LLM server URL (default from config.py)")
    ap.add_argument("--converter", choices=["libredwg", "oda"], help="DWG converter (default from config.py)")
    args = ap.parse_args(argv)

    llm_cfg = dict(config.LLM)
    if args.model:
        llm_cfg["model"] = args.model
    if args.base_url:
        llm_cfg["base_url"] = args.base_url
    if args.provider:
        llm_cfg["provider"] = args.provider
    use_llm = llm_cfg.get("enabled", True) and not args.no_llm

    failures = 0
    for path in args.inputs:
        t0 = time.time()
        print(f"\n== {path.name}")
        try:
            stats = run_one(path, args.out, args.converter or config.CONVERTER, llm_cfg if use_llm else None)
            stats["total_s"] = round(time.time() - t0, 1)
            print(f"   done in {stats['total_s']}s")
            log_run(args.out, path, stats)
        except Exception as exc:  # keep going with the other drawings
            failures += 1
            print(f"   FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


def log_run(out_dir: Path, path: Path, stats: dict) -> None:
    """Append one line per run to out/runs.csv - the prototype's measurement log."""
    log = out_dir / "runs.csv"
    new = not log.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(log, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["when", "drawing", "notes", "dimensions", "rule_lines", "sent_to_llm", "unverified",
                "flags", "errors", "bom_rows", "model", "llm_s", "total_s"]
        if new:
            w.writerow(cols)
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), path.name] + [stats.get(c, "") for c in cols[2:]])


def run_one(path: Path, out_dir: Path, converter: str, llm_cfg: dict | None) -> dict:
    warnings: list[str] = []
    stats: dict = {}
    suffix = path.suffix.lower()

    # 1-2. convert + extract
    if suffix == ".json":
        extraction = json.loads(path.read_text(encoding="utf-8"))
        stem = path.stem
        extraction_path = path
    else:
        from dwgbom.extract import extract          # imported here so .json runs need no ezdxf
        dxf = path
        if suffix == ".dwg":
            from dwgbom.convert import dwg_to_dxf
            dxf, warnings = dwg_to_dxf(path, Path(config.DXF_CACHE_DIR), converter)
            print(f"   converted with {converter} -> {dxf}")
        extraction = extract(dxf, length_layers=config.LENGTH_LAYERS, ignore_layers=config.IGNORE_LAYERS,
                             max_depth=config.MAX_BLOCK_DEPTH, source_file=path)
        stem = path.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        extraction_path = out_dir / f"{stem}_extraction.json"
        extraction_path.write_text(
            json.dumps(extraction, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   extracted {len(extraction.get('texts', []))} notes, "
          f"{len(extraction.get('dimensions', []))} dimensions, {len(extraction.get('blocks', []))} block types")

    # 3. rules
    result, items = bom.classify_notes(extraction, tol_mm=config.UNIT_CHECK_TOLERANCE_MM,
                                       tol_pct=config.UNIT_CHECK_TOLERANCE_PCT)
    print(f"   rules: {len(items)} material lines, {len(result.unmatched)} notes left over, "
          f"{len(result.flags)} flags")

    stats.update(notes=len(extraction.get("texts", [])), dimensions=len(extraction.get("dimensions", [])),
                 rule_lines=len(items), sent_to_llm=len(result.unmatched) if llm_cfg else 0)

    # 4. LLM for the leftovers only
    if llm_cfg and result.unmatched:
        from dwgbom.llm import LocalLLM, LLMError
        try:
            t_llm = time.time()
            client = LocalLLM(**llm_cfg)
            answers = client.classify(result.unmatched, batch_size=llm_cfg.get("batch_size", 30),
                                      log=lambda m: print("  " + m))
            bom.apply_llm(result, items, answers)
            stats.update(model=client.model, llm_s=round(time.time() - t_llm, 1),
                         unverified=sum(1 for a in answers if not a["verified"]))
            print(f"   LLM stage: {stats['llm_s']}s")
        except LLMError as exc:
            warnings.append(f"LLM stage skipped: {exc}")
            print(f"   LLM stage skipped: {exc}")

    # 5-6. assemble + export
    bom.finish(result, items, extraction, reference_categories=config.REFERENCE_CATEGORIES)
    from dwgbom.export import write_outputs
    xlsx, js = write_outputs(result, extraction, out_dir, stem, warnings)
    errors = sum(1 for f in result.flags if f["severity"] == "error")
    print(f"   BOM: {len(result.rows)} rows, {errors} errors to fix -> {xlsx}")
    stats.update(flags=len(result.flags), errors=errors, bom_rows=len(result.rows),
                 extraction_json=str(extraction_path), bom_xlsx=str(xlsx), bom_json=str(js))
    return stats


if __name__ == "__main__":
    sys.exit(main())
