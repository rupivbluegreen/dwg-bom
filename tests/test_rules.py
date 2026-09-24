"""Run with:  python -m pytest tests   (or simply: python tests/test_rules.py)"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dwgbom import bom, llm, rules  # noqa: E402

SAMPLE = Path(__file__).with_name("sample_extraction.json")


def test_clean_mtext_and_stacked_fractions():
    assert rules.flat("\\A1;ELEV. 5'-10\\H0.7x;\\S1/2;\" TOP OF LANDING") == "ELEV. 5'-10 1/2\" TOP OF LANDING"
    assert rules.flat("\\A1;1-{\\H0.7x;\\S1/4;}\"\\P[33]") == '1-1/4" [33]'
    assert rules.flat("{\\W1.15;9.5%%C CABLES (TYP.)}") == "9.5Ø CABLES (TYP.)"


def test_unit_check_catches_wrong_metric():
    assert rules.check_units("n", '3/4"[30] O.S.B SHEATHING')          # 3/4" is 19 mm
    assert not rules.check_units("n", '3/4"[19] T & G PLYWOOD')
    assert not rules.check_units("n", '2"x6" [38x140] WALL FRAMING')    # nominal lumber is fine
    assert not rules.check_units("n", 'HSS 1-1/2"x1-1/2" [38x38]')
    assert not rules.check_units("n", "5'-10 1/2\" [1791]")
    assert rules.check_units("n", "5'-10\" [1900]")


def test_steel_designations():
    m = {i.designation: i for i in rules.match_materials("W310x39") + rules.match_materials("C250")}
    assert m["W310x39"].mass_kg_m == 39
    assert m["C250"].mass_kg_m is None
    assert rules.match_materials("W12") == []                          # ambiguous: left for the LLM
    assert rules.match_materials("CONNECT C/W BOLTS") == []            # C/W is not a channel


def test_sample_drawing_end_to_end():
    ex = json.loads(SAMPLE.read_text(encoding="utf-8"))
    res, items = bom.classify_notes(ex)
    bom.finish(res, items, ex)
    rows = {(r["description"], r["designation"]): r for r in res.rows}
    assert rows[("Channel", "C250")]["mass_kg_m"] == 23               # from block C250x23
    assert rows[("Wide-flange beam", "W410")]["mass_kg_m"] is None
    assert "FIX unit error" in rows[("OSB sheathing", '3/4" [30]')]["status"]
    assert {n["text"] for n in res.unmatched} == {"CABLES", "W12"}
    assert all(r["status"].endswith("needs quantity") for r in res.rows)  # nothing invented


def test_detail_callouts_are_references():
    for tag in ("2 A-05", "1 A-05", "3/S-201", "A A5.01"):
        assert rules.classify_info(tag) == "detail callout", tag
    for note in ("W12", "CABLES", "350 OWSJ", "4 W12"):
        assert rules.classify_info(note) != "detail callout", note


def test_llm_guard_rejects_invented_numbers():
    batch = [{"id": "N1", "text": "W12"}]
    out = llm.guard(batch, [{"id": "N1", "kind": "material", "category": "Structural steel",
                             "description": "Wide-flange beam", "designation": "W12x26", "question": ""},
                            {"id": "XX", "kind": "material"}])
    assert len(out) == 1 and out[0]["verified"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
