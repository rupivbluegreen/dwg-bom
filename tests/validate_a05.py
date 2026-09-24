#!/usr/bin/env python3
"""Prototype acceptance test: run the real A-05 drawing through convert + extract
and compare with what the file is known to contain (decoded independently).

    python tests/validate_a05.py "architectural_-_annotation_scaling_and_multileaders.dwg"

Also accepts a .dxf, or an extraction .json. Writes out/validate_a05_report.txt;
if checks fail, send that file back for diagnosis.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from dwgbom import bom, rules  # noqa: E402

# Notes known to be on sheet A-05 (from decoding the DWG directly).
EXPECTED_NOTES = [
    "EPDM ROOFING MEMBRANE", "T & G PLYWOOD SHEATHING", "TJI ROOF JOISTS", "VAPOR BARRIER",
    "SUSPENDED ACOUSTIC TILE", "WALL FRAMING", "O.S.B SHEATHING", "GYPSUM BOARD",
    "CHECKER PLATE PANEL", "RAILING (TYP.)", "CABLES (TYP.)", "POSTS (TYP.)", "HOLE",
    "S.S. BOLT AND ACORN NUT", "PLATE-WELDED", "CRIMPED-ON STUD END", "GRADE BEAM BELOW",
    "W250", "W410", "C250", "350 OWSJ", "W310", "TOP OF LANDING", "TOP OF SECOND FLR",
]
EXPECTED_DIMENSION_TEXT = ["[2743]", "[9906]", "[6909]", "[3581]", "[1791]"]
EXPECTED_BLOCKS = ["C250x23", "W250x33"]
EXPECTED_SCALES = [4, 24, 48, 48]


def load(path: Path) -> tuple[dict, list[str]]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8")), []
    from dwgbom.extract import extract
    warnings: list[str] = []
    dxf = path
    if path.suffix.lower() == ".dwg":
        from dwgbom.convert import dwg_to_dxf
        dxf, warnings = dwg_to_dxf(path, ROOT / config.DXF_CACHE_DIR, config.CONVERTER)
    ex = extract(dxf, length_layers=config.LENGTH_LAYERS, ignore_layers=config.IGNORE_LAYERS,
                 max_depth=config.MAX_BLOCK_DEPTH, source_file=path)
    return ex, warnings


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1]).expanduser()
    ex, warnings = load(path)
    lines: list[str] = []
    fails = 0

    def check(ok: bool, name: str, detail: str = "", warn_only: bool = False) -> None:
        nonlocal fails
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        fails += status == "FAIL"
        lines.append(f"{status:4}  {name:44} {detail}")

    model_text = " | ".join(rules.flat(t.get("raw") or t["text"]) for t in ex["texts"] if t["space"] == "model")
    missing = [n for n in EXPECTED_NOTES if n not in model_text]
    check(not missing, f"Notes found ({len(EXPECTED_NOTES) - len(missing)}/{len(EXPECTED_NOTES)})",
          "missing: " + ", ".join(missing) if missing else "")
    kinds = sorted({t["kind"] for t in ex["texts"]})
    check("MULTILEADER" in kinds, "Multileaders read as MULTILEADER", f"kinds seen: {kinds}", warn_only=True)

    shown = " | ".join(d.get("shown") or d.get("override") or "" for d in ex["dimensions"])
    miss_d = [d for d in EXPECTED_DIMENSION_TEXT if d not in shown]
    check(len(ex["dimensions"]) >= 20 and not miss_d, f"Dimensions ({len(ex['dimensions'])} found)",
          "missing text: " + ", ".join(miss_d) if miss_d else "")
    with_measure = sum(1 for d in ex["dimensions"] if d.get("measurement"))
    check(with_measure > 0, "Dimension measurements computed", f"{with_measure}/{len(ex['dimensions'])}", warn_only=True)

    names = [b["name"] for b in ex["blocks"]]
    miss_b = [b for b in EXPECTED_BLOCKS if b not in names]
    check(not miss_b, "Profile blocks found", "missing: " + ", ".join(miss_b) if miss_b else
          ", ".join(f'{b["name"]} x{b["count"]}' for b in ex["blocks"] if b["name"] in EXPECTED_BLOCKS))

    scales = sorted(round(v["scale_factor"]) for v in ex["viewports"] if v.get("scale_factor"))
    check(scales == EXPECTED_SCALES, "Viewport scales 1:48, 1:48, 1:24, 1:4", f"found {scales}", warn_only=True)

    tb = json.dumps(ex.get("title_block", {}))
    check("A-05" in tb or "STAIR SECTIONS" in tb, "Title block attributes", "", warn_only=True)

    res, items = bom.classify_notes(ex)
    bom.finish(res, items, ex, reference_categories=config.REFERENCE_CATEGORIES)
    osb = [f for f in res.flags if "O.S.B" in f["text"].upper()]
    check(len(osb) == 2, "OSB 3/4\" [30] error flagged twice", f"{len(osb)} flags")
    false_dim_flags = [f for f in res.flags if f["type"] == "dimension_unit_mismatch"]
    check(not false_dim_flags, "No false dimension flags", "; ".join(f["message"] for f in false_dim_flags))
    check(len(res.unmatched) <= 4, "Notes left for the LLM", f"{len(res.unmatched)}: " +
          ", ".join(n["text"] for n in res.unmatched)[:120])
    check(len(res.rows) >= 18, "BOM rows", str(len(res.rows)))

    lines.append("")
    lines.append(f"{fails} failed")
    if warnings:
        lines.append("\nConverter messages:\n  " + "\n  ".join(warnings[:20]))
    lines.append("\nAll model-space notes as extracted:")
    lines += [f'  {t["id"]:>6} {t["kind"]:11} {t["layer"][:18]:18} {rules.flat(t.get("raw") or t["text"])[:90]}'
              for t in ex["texts"] if t["space"] == "model"]

    report = "\n".join(lines)
    print(report)
    out = ROOT / "out" / "validate_a05_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
