"""Stage 3 - deterministic rules (no AI).

  * recognise materials in notes: steel shapes, HSS, joists, plate, cable, fasteners,
    panels, framing, sheathing, roofing
  * recognise non-material notes (levels, references, holes) so they are not sent to the LLM
  * check every imperial/metric pair, e.g. 3/4" [19] passes, 3/4" [30] is flagged

Everything here is plain regex + arithmetic, so it is repeatable and testable.
Notes the rules cannot place are the only thing the LLM stage sees.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- text cleanup
_FORMAT_CODES = re.compile(r"\\[ACHQTWFfp][^;]*;|\\[LlOoKkNX~]")
_STACK = re.compile(r"\\S([^;^/#]*)[\^/#]([^;]*);")


def clean(s: str) -> str:
    """Strip MTEXT formatting, keep line breaks, render stacked fractions (5'-10 1/2")."""
    s = s.replace("\\P", "\n")
    s = _FORMAT_CODES.sub("", s)
    s = s.replace("{", "").replace("}", "")

    def stack(m: re.Match) -> str:
        before = m.string[m.start() - 1] if m.start() else ""
        return (" " if before.isdigit() else "") + f"{m.group(1).strip()}/{m.group(2).strip()}"

    s = _STACK.sub(stack, s)
    for code, ch in (("%%c", "Ø"), ("%%C", "Ø"), ("%%d", "°"), ("%%D", "°"), ("%%p", "±"), ("%%P", "±")):
        s = s.replace(code, ch)
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(ln.strip() for ln in s.splitlines() if ln.strip())


def flat(s: str) -> str:
    """Single-line, upper-case form used for matching."""
    return re.sub(r"\s+", " ", clean(s)).upper().strip()


# ------------------------------------------------------------------------ numbers
_NUM = r"\d+(?:\.\d+)?"
_INCH = r"(?:\d+-\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"       # 1-1/2, 3/4, 2, 0.75


def inches(tok: str) -> float:
    tok = tok.strip()
    if "-" in tok and "/" in tok:
        whole, frac = tok.split("-", 1)
        return float(whole) + inches(frac)
    if "/" in tok:
        a, b = tok.split("/")
        return float(a) / float(b)
    return float(tok)


# Nominal lumber: 2" x 6" is really 38 x 140 mm, so compare against this table.
_LUMBER_MM = {2: 38, 3: 64, 4: 89, 6: 140, 8: 184, 10: 235, 12: 286}


def _mm(v: float) -> str:
    return f"{round(v + 1e-9, 1):g}"


@dataclass
class UnitFlag:
    source_id: str
    text: str
    message: str
    severity: str = "error"
    type: str = "unit_mismatch"


def check_units(src_id: str, text: str, tol_mm: float = 1.5, tol_pct: float = 0.02) -> list[UnitFlag]:
    """Check imperial values against the metric value written next to them in [brackets]."""
    s = flat(text)
    flags: list[UnitFlag] = []

    def bad(expected_mm: float, written_mm: float) -> bool:
        return abs(expected_mm - written_mm) > max(tol_mm, tol_pct * expected_mm)

    def mask(m: re.Match) -> None:
        nonlocal s
        s = s[:m.start()] + " " * (m.end() - m.start()) + s[m.end():]

    # 1) feet-inches: 5'-10 1/2" [1791]
    for m in list(re.finditer(rf"(\d+)'\s*-?\s*(\d+)(?:\s+(\d+)/(\d+))?\"\s*\[({_NUM})\]", s)):
        ft, inch = int(m.group(1)), int(m.group(2))
        frac = int(m.group(3)) / int(m.group(4)) if m.group(3) else 0.0
        exp = (ft * 12 + inch + frac) * 25.4
        if bad(exp, float(m.group(5))):
            flags.append(UnitFlag(src_id, clean(text), f"{m.group(0)}: {exp:.0f} mm expected, note says {m.group(5)}"))
        mask(m)

    # 2) pairs: 2"x6" [38x140], HSS 1-1/2"x1-1/2" [38x38]
    for m in list(re.finditer(rf"({_INCH})\"?\s*X\s*({_INCH})\"\s*\[({_NUM})\s*X\s*({_NUM})\]", s)):
        for tok, mm in ((m.group(1), m.group(3)), (m.group(2), m.group(4))):
            val, mm = inches(tok), float(mm)
            lumber_ok = val.is_integer() and _LUMBER_MM.get(int(val)) == round(mm)
            if bad(val * 25.4, mm) and not lumber_ok:
                flags.append(UnitFlag(src_id, clean(text),
                                      f'{m.group(0)}: {tok}" = {_mm(val * 25.4)} mm, note says {mm:g}'))
        mask(m)

    # 3) single values: 3/4" [19], 1-1/4" [33]
    for m in re.finditer(rf"(?<![\d/\-'.])({_INCH})\"\s*\[({_NUM})\]", s):
        val, mm = inches(m.group(1)), float(m.group(2))
        if bad(val * 25.4, mm):
            flags.append(UnitFlag(src_id, clean(text),
                                  f'{m.group(1)}" = {_mm(val * 25.4)} mm, but the note says [{mm:g}]'))
    return flags


# ---------------------------------------------------------------------- materials
@dataclass
class Item:
    key: str
    category: str
    description: str
    designation: str = ""
    unit: str = "ea"
    mass_kg_m: float | None = None
    source_ids: list[str] = field(default_factory=list)
    remarks: list[str] = field(default_factory=list)
    origin: str = "rule"


CATEGORY_ORDER = [
    "Structural steel", "Railing & guards", "Plates & connections", "Fasteners",
    "Treads, plate & panels", "Wood framing & finishes", "Roofing", "Other",
]

LB_FT_TO_KG_M = 1.48816


def match_materials(text: str) -> list[Item]:
    s = flat(text)
    roof = "ROOF" in s
    out: list[Item] = []

    def add(key, cat, desc, desig="", unit="ea", mass=None, remark=None):
        out.append(Item(key, cat, desc, desig, unit, mass, remarks=[remark] if remark else []))

    # Wide-flange and channel shapes. Metric (W310x39) and imperial (W12x26).
    for shape, name in (("W", "Wide-flange beam"), ("C", "Channel")):
        for m in re.finditer(rf"\b{shape}\s?(\d{{3,4}})(?:\s?X\s?({_NUM}))?\b", s):
            d, kg = m.group(1), m.group(2)
            desig = f"{shape}{d}x{kg}" if kg else f"{shape}{d}"
            add(f"steel:{desig}", "Structural steel", name, desig, "m", float(kg) if kg else None)
        for m in re.finditer(rf"\b{shape}\s?(\d{{1,2}})\s?X\s?({_NUM})\b", s):
            desig = f"{shape}{m.group(1)}x{m.group(2)}"
            add(f"steel:{desig}", "Structural steel", name, desig, "m",
                round(float(m.group(2)) * LB_FT_TO_KG_M, 1), "imperial lb/ft converted to kg/m")

    for m in re.finditer(rf"\bL\s?({_NUM})\s?X\s?({_NUM})\s?X\s?({_NUM})\b", s):
        desig = f"L{m.group(1)}x{m.group(2)}x{m.group(3)}"
        add(f"steel:{desig}", "Structural steel", "Angle", desig, "m")

    for m in re.finditer(r"\b(\d{3,4})\s*OWSJ\b", s):
        add(f"owsj:{m.group(1)}", "Structural steel", f"Open-web steel joist, {m.group(1)} mm deep",
            f"{m.group(1)} OWSJ", "ea", remark="joist designation / span not on sheet")

    # HSS: HSS 1-1/2"x1-1/2" [38x38] ... POSTS / RAILING
    for m in re.finditer(rf"\bHSS\s*({_INCH})\"?\s*X\s*({_INCH})\"?(?:\s*X\s*({_INCH})\"?)?(?:\s*\[({_NUM})X({_NUM})(?:X({_NUM}))?\])?", s):
        a, b, t = m.group(1), m.group(2), m.group(3)
        desig = f'HSS {a}"x{b}"' + (f'x{t}"' if t else "")
        if m.group(4):
            desig += f" [{m.group(4)}x{m.group(5)}" + (f"x{m.group(6)}" if m.group(6) else "") + "]"
        tail = s[m.end():m.end() + 40]
        role = "posts" if "POST" in tail else "rails" if "RAIL" in tail else ""
        cat = "Railing & guards" if role else "Structural steel"
        add(f"hss:{a}x{b}x{t}:{role}", cat, f"HSS {role}".strip(), desig, "m",
            remark=None if t else "wall thickness not given")

    for m in re.finditer(rf"\b({_NUM})\s?X\s?({_NUM})\s?X\s?({_NUM})\s*(?:PL\b|PLATE)", s):
        desig = f"{m.group(1)}x{m.group(2)}x{m.group(3)}"
        painted = " (painted)" if "PAINT" in s else ""
        welded = ", welded to post" if "WELD" in s and "POST" in s else ""
        add(f"plate:{desig}", "Plates & connections", f"Steel plate{welded}{painted}", f"PL {desig}", "ea")

    for m in re.finditer(rf"({_NUM})\s*(?:Ø|MM\s*DIA\.?|DIA\.?)\s*(S\.S\.\s*)?CABLES?\b", s):
        ss = "Stainless steel cable" if m.group(2) else "Cable"
        add(f"cable:{m.group(1)}", "Railing & guards", ss, f"Ø{m.group(1)}", "m")
    if "STUD END" in s:
        add("cable-end:stud", "Railing & guards", "Cable end fitting, crimped-on stud end c/w nut",
            "", "ea", remark="stainless steel" if "S.S." in s else None)

    if re.search(r"\bS\.?S\.?\s+BOLT", s):
        desc = "Stainless steel bolt" + (" c/w acorn nut" if "ACORN" in s else "")
        add("bolt:ss", "Fasteners", desc, "", "ea", remark="bolt size not given")

    if re.search(r"CHECKER\s*PLATE", s):
        mat = "Aluminum" if re.search(r"\bALUM", s) else "Steel"
        fin = ", polished" if "POLISHED" in s else ""
        add(f"checker:{mat}", "Treads, plate & panels", f"{mat} checker plate panel{fin}", "", "ea",
            remark="thickness / panel size not given")

    # Wood framing & finishes (usually reference construction on stair sheets)
    finish_cat = "Roofing" if roof else "Wood framing & finishes"
    for m in re.finditer(r"\b(\d{1,2})\"?\s*X\s*(\d{1,2})\"\s*(?:\[(\d+)X(\d+)\])?\s*(?:WOOD\s+)?(?:WALL|STUD|FRAMING|JOIST|RAFTER)", s):
        mm = f" [{m.group(3)}x{m.group(4)}]" if m.group(3) else ""
        add(f"lumber:{m.group(1)}x{m.group(2)}", "Wood framing & finishes", "Wood framing",
            f'{m.group(1)}"x{m.group(2)}"{mm}', "m")
    for m in re.finditer(rf"({_INCH})\"\s*(?:\[({_NUM})\])?\s*(T\s*&\s*G\s+)?(PLYWOOD|O\.?S\.?B\.?)(?:\s+SHEATHING)?", s):
        kind = "Plywood" if m.group(4) == "PLYWOOD" else "OSB"
        tg = " T&G" if m.group(3) else ""
        mm = f" [{m.group(2)}]" if m.group(2) else ""
        add(f"sheathing:{kind}:{m.group(1)}:{finish_cat}", finish_cat, f"{kind}{tg} sheathing",
            f'{m.group(1)}"{mm}', "m²")
    for m in re.finditer(rf"({_INCH})\"\s*(?:\[({_NUM})\])?\s*GYPSUM\s*BOARD", s):
        mm = f" [{m.group(2)}]" if m.group(2) else ""
        add(f"gypsum:{m.group(1)}:{finish_cat}", finish_cat, "Gypsum board", f'{m.group(1)}"{mm}', "m²")
    if re.search(r"EPDM\s+(ROOFING\s+)?MEMBRANE", s):
        add("roof:epdm", "Roofing", "EPDM roofing membrane", "", "m²")
    if re.search(r"\bTJI\b.*\bJOISTS?\b", s):
        add("roof:tji", finish_cat, "TJI joists", "", "ea", remark="series / depth not given")
    if re.search(r"VAPOU?R\s+(BARRIER|RETARDER)", s):
        add(f"vb:{finish_cat}", finish_cat, "Vapour barrier", "", "m²")
    if re.search(r"ACOUSTIC(AL)?\s+(CEILING\s+)?TILE", s):
        add(f"act:{finish_cat}", finish_cat, "Suspended acoustic ceiling tile", "", "m²")
    return out


# ------------------------------------------------------------ non-material notes
_INFO = [
    ("level", re.compile(r"^ELEV\.?\b|\bT\.O\.(S\.)?\s|\bT/O\b|TOP OF (LANDING|SLAB|STEEL|.*FLR|.*FLOOR)")),
    ("reference", re.compile(r"^SEE\b|\bSEE (DETAIL|DWG|SHEET|STRUCT)|\bOUTSIDE FACE OF\b|\bBELOW$|\bABOVE$")),
    ("fabrication", re.compile(rf"\b{_NUM}\s*Ø\s*HOLES?\b|\bHOLES?\b")),
    ("view label", re.compile(r"^(STAIR |WALL |ROOF )?(SECTION|DETAIL|PLAN|ELEVATION)\b|\b(SECTION|DETAIL) \d+$")),
    # detail/section callout bubbles: "2 A-05", "3/S-201", "A A5.01" (detail id + sheet number)
    ("detail callout", re.compile(r"^[A-Z0-9]{1,3}\s*/?\s*[A-Z]{1,3}(?:-?\d{1,4}\.\d{1,3}|[-.]\d{1,4})$")),
]


def classify_info(text: str) -> str | None:
    s = flat(text)
    for label, rx in _INFO:
        if rx.search(s):
            return label
    return None
