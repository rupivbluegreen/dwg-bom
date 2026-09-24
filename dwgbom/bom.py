"""Stage 5 - assemble the BOM.

Merges rule matches and (optional) LLM classifications into BOM rows, then adds
what the drawing itself can prove:

  * steel mass per metre from the designation (W310x39 -> 39 kg/m), or inferred from
    a profile block with a fuller name (note "W250" + block "W250x33"), clearly marked
  * how many times each item is called out on the sheet (NOT a quantity)
  * a status per row saying exactly what is still missing

Quantities stay empty unless the drawing states them. The spreadsheet leaves those
cells for a person to fill in, and computes totals from them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import rules
from .rules import CATEGORY_ORDER, Item


@dataclass
class BomResult:
    rows: list[dict] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)     # every note with how it was classified
    unmatched: list[dict] = field(default_factory=list)  # notes the rules could not place


def classify_notes(extraction: dict, note_spaces=("model",), tol_mm=1.5, tol_pct=0.02) -> tuple[BomResult, dict]:
    """Rules pass. Returns the partial result and the merged rule items."""
    res = BomResult()
    items: dict[str, Item] = {}

    for t in extraction.get("texts", []):
        if t.get("space") not in note_spaces:
            continue
        src = t.get("raw") or t["text"]
        for f in rules.check_units(t["id"], src, tol_mm, tol_pct):
            res.flags.append(vars(f))

        mats = rules.match_materials(src)
        if mats:
            for m in mats:
                _merge(items, m, t["id"])
            status = "material: " + "; ".join(sorted({m.description for m in mats}))
        elif (info := rules.classify_info(src)):
            status = f"info: {info}"
        else:
            status = "unmatched"
            res.unmatched.append({"id": t["id"], "text": rules.clean(src)})
        res.notes.append({"id": t["id"], "layer": t.get("layer"), "space": t.get("space"),
                          "block": t.get("block"), "text": rules.clean(src), "classified_as": status})

    for d in extraction.get("dimensions", []):
        shown = d.get("shown_raw") or d.get("shown") or d.get("override") or ""
        for f in rules.check_units(d["id"], shown, tol_mm, tol_pct):
            f.type = "dimension_unit_mismatch"
            res.flags.append(vars(f))
    return res, items


def apply_llm(res: BomResult, items: dict[str, Item], answers: list[dict]) -> None:
    by_id = {a["id"]: a for a in answers}
    for note in res.notes:
        a = by_id.get(note["id"])
        if not a:
            continue
        note["classified_as"] = f'LLM {a["kind"]}: {a["description"]}' + ("" if a["verified"] else " (unverified)")
        if a["kind"] == "material":
            if not a["verified"]:
                # never show numbers the model made up: fall back to the note's own wording
                a = {**a, "description": note["text"], "designation": ""}
            key = "llm:" + re.sub(r"\W+", "-", f'{a["category"]}-{a["description"]}-{a["designation"]}'.lower())
            item = Item(key, a["category"], a["description"] or note["text"], a["designation"],
                        unit="", origin="llm" if a["verified"] else "llm-unverified",
                        remarks=[a["question"]] if a["question"] else [])
            _merge(items, item, note["id"])
        elif a["kind"] == "unclear":
            res.flags.append({"source_id": note["id"], "text": note["text"], "severity": "review",
                              "type": "unclear_note", "message": a["question"] or "meaning unclear"})


def finish(res: BomResult, items: dict[str, Item], extraction: dict, reference_categories=()) -> BomResult:
    block_names = [b["name"] for b in extraction.get("blocks", [])]
    block_counts = {b["name"]: b.get("count") for b in extraction.get("blocks", [])}

    rows = []
    for it in items.values():
        evidence, remarks = [], list(dict.fromkeys(r for r in it.remarks if r))

        # Steel: mass per metre, possibly from a profile block name.
        if it.category == "Structural steel" and it.mass_kg_m is None and re.fullmatch(r"[WC]\d{3,4}", it.designation):
            fuller = sorted({n for n in block_names
                             if re.fullmatch(rf"{it.designation}\s?[xX]\s?(\d+(?:\.\d+)?)", n)})
            if len(fuller) == 1:
                it.mass_kg_m = float(re.split(r"[xX]", fuller[0])[1])
                remarks.append(f'mass inferred from profile block "{fuller[0]}" - confirm with engineer')
            else:
                remarks.append("mass per metre not given")
        for n in block_names:
            if it.designation and n.replace(" ", "").lower().startswith(it.designation.replace(" ", "").lower()):
                c = block_counts.get(n)
                evidence.append(f'profile block "{n}" ' + (f"inserted {c}x (drawn symbols, not pieces)" if c else "present"))

        status = []
        if any(f["source_id"] in it.source_ids and f["severity"] == "error" for f in res.flags):
            status.append("FIX unit error (see Flags)")
        if it.category == "Structural steel" and it.mass_kg_m is None and it.unit == "m":
            status.append("needs mass/m")
        if any("inferred" in r for r in remarks):
            status.append("confirm mass")
        if any(("not given" in r or "not on sheet" in r) and "mass per metre" not in r for r in remarks):
            status.append("needs spec")
        if it.origin == "llm":
            status.append("check LLM wording")
        elif it.origin == "llm-unverified":
            status.append("LLM invented data - check note")
        status.append("needs quantity")

        rows.append({
            "category": it.category,
            "description": it.description,
            "designation": it.designation,
            "mass_kg_m": it.mass_kg_m,
            "unit": it.unit,
            "callouts": len(it.source_ids),
            "evidence": "; ".join(evidence),
            "remarks": "; ".join(remarks),
            "status": ", ".join(status),
            "reference_only": it.category in reference_categories,
            "sources": ", ".join(it.source_ids),
            "origin": it.origin,
        })

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    rows.sort(key=lambda r: (r["reference_only"], order.get(r["category"], 99), r["description"], r["designation"]))
    for i, r in enumerate(rows, 1):
        r["item"] = i
    res.rows = rows
    return res


def _merge(items: dict[str, Item], new: Item, source_id: str) -> None:
    it = items.get(new.key)
    if it is None:
        it = items[new.key] = Item(new.key, new.category, new.description, new.designation,
                                   new.unit, new.mass_kg_m, [], list(new.remarks), new.origin)
    else:
        # "Cable" + "Stainless steel cable" of the same size: keep the more specific wording
        if len(new.description) > len(it.description):
            it.description = new.description
        it.remarks.extend(r for r in new.remarks if r not in it.remarks)
    if source_id not in it.source_ids:
        it.source_ids.append(source_id)
