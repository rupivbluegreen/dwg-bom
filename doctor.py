#!/usr/bin/env python3
"""Check that this Mac is ready for the pipeline, and measure the local model.

    python doctor.py              # everything
    python doctor.py --no-llm     # skip the model checks
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import time

import config

RESULTS: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str = "") -> None:
    RESULTS.append((status, check, detail))
    colour = {"PASS": "32", "WARN": "33", "FAIL": "31", "INFO": "36"}[status]
    print(f"\033[{colour}m{status:4}\033[0m  {check:34} {detail}")


def sysctl(name: str) -> str | None:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def check_machine() -> None:
    mac = platform.system() == "Darwin"
    arm = platform.machine() == "arm64"
    report("PASS" if mac and arm else "WARN", "Apple Silicon Mac", f"{platform.system()} {platform.machine()}")
    chip = sysctl("machdep.cpu.brand_string")
    if chip:
        report("INFO", "Chip", chip)
    mem = sysctl("hw.memsize")
    if mem:
        gb = int(mem) / 2**30
        status = "PASS" if gb >= 32 else "WARN"
        report(status, "Unified memory", f"{gb:.0f} GB" + ("" if gb >= 32 else " - use Qwen3 14B instead of 27B"))
    limit = sysctl("iogpu.wired_limit_mb")
    if limit is not None:
        detail = "macOS default (about 2/3 of RAM)" if limit == "0" else f"{limit} MB (raised manually)"
        report("INFO", "GPU memory limit", detail)


def check_python() -> None:
    ok = sys.version_info >= (3, 9)
    report("PASS" if ok else "FAIL", "Python >= 3.9", platform.python_version())
    for mod in ("ezdxf", "openpyxl"):
        try:
            m = __import__(mod)
            report("PASS", f"{mod} installed", getattr(m, "__version__", "?"))
        except ImportError:
            report("FAIL", f"{mod} installed", "run: pip install -r requirements.txt")


def check_converter() -> None:
    exe = shutil.which("dwg2dxf")
    if not exe:
        report("FAIL", "LibreDWG dwg2dxf", "run: brew install libredwg")
        return
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    report("PASS", "LibreDWG dwg2dxf", (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else exe)


def check_llm() -> None:
    from dwgbom.llm import LLMError, LocalLLM

    cfg = dict(config.LLM)
    try:
        client = LocalLLM(**cfg)
    except LLMError as exc:
        report("FAIL", "LLM server + model", str(exc))
        return
    report("PASS", "LLM server", f'{cfg.get("provider", "openai")} at {client.base_url}')
    report("PASS", "Model available", client.model)

    # 1) speed: a fixed-length answer, timed (the first call also loads the model)
    try:
        b = client.benchmark("Count from 1 to 60, separated by spaces. Output only the numbers.")
    except LLMError as exc:
        report("FAIL", "Generation", str(exc))
        return
    if b["load_s"] > 1:
        report("INFO", "Model load time", f'{b["load_s"]:.0f}s (only when the model was not yet in memory)')
    if b["tokens"] and b["gen_s"] > 0:
        tps = b["tokens"] / b["gen_s"]
        report("PASS" if tps >= 4 else "WARN", "Generation speed", f'{tps:.1f} tokens/s ({b["tokens"]} tokens)')
    else:
        report("INFO", "Generation time", f'{b["wall_s"]:.1f}s (server did not report token counts)')

    share = client.gpu_share()
    if share is not None:
        if share >= 0.99:
            report("PASS", "Model fully in GPU memory", "100%")
        else:
            report("WARN", "Model fully in GPU memory",
                   f"only {share:.0%} - part runs on CPU (slow). Close apps, or use CTX=4096 / a smaller model")

    # 2) the real task on two notes from sheet A-05, including the guard
    notes = [{"id": "N1", "text": "W12"}, {"id": "N2", "text": "CABLES"}]
    t0 = time.time()
    try:
        answers = client.classify(notes, log=lambda m: None)
    except Exception as exc:
        report("FAIL", "Note classification", str(exc))
        return
    dt = time.time() - t0
    bad = [a["id"] for a in answers if not a["verified"]]
    detail = f"{len(answers)}/2 answered in {dt:.1f}s; " + \
        ("all numbers traceable to the notes" if not bad else f"guard caught invented numbers in {bad}")
    report("PASS" if len(answers) == 2 else "WARN", "Note classification + guard", detail)
    for a in answers:
        report("INFO", f"  {a['id']} -> {a['kind']}", f"{a['description']} {a['designation']}".strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    print("dwg-bom doctor\n")
    check_machine()
    check_python()
    check_converter()
    if not args.no_llm:
        check_llm()
    fails = sum(1 for s, *_ in RESULTS if s == "FAIL")
    warns = sum(1 for s, *_ in RESULTS if s == "WARN")
    print(f"\n{fails} failed, {warns} warnings")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
