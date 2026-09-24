"""Stage 2 - DXF -> extraction.json (deterministic, no AI).

Pulls every fact the BOM can be built from, each tagged with the entity handle it
came from so any BOM row can be traced back to the drawing:

  texts        TEXT, MTEXT, MULTILEADER and ATTRIB content (plain text)
  dimensions   measured value + the text actually shown on the sheet
  blocks       block insert counts, following nested blocks and resolving
               dynamic-block names (*U123 -> real name)
  linework     total length of lines/arcs/polylines per layer
  viewports    paper-space viewports and their scales
  title_block  attribute values from title-block inserts
"""
from __future__ import annotations

import fnmatch
import hashlib
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import ezdxf
from ezdxf import recover
from ezdxf import path as ezpath
from ezdxf.math import Vec3
from ezdxf.tools.text import plain_mtext

TEXT_TYPES = "TEXT MTEXT MULTILEADER"
CURVE_TYPES = "LINE ARC CIRCLE LWPOLYLINE POLYLINE SPLINE ELLIPSE"

# $INSUNITS -> metres per drawing unit
_UNIT_TO_M = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}
_UNIT_NAME = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}


def extract(dxf_path: Path, *, length_layers: list[str], ignore_layers: list[str],
            max_depth: int = 8, source_file: Path | None = None) -> dict:
    doc, auditor = recover.readfile(str(dxf_path))
    insunits = int(doc.header.get("$INSUNITS", 0))
    ex = _Extractor(doc, length_layers, ignore_layers, max_depth)
    ex.run()

    src = Path(source_file or dxf_path)
    return {
        "schema": "dwg-bom/extraction@1",
        "source": {
            "file": src.name,
            "sha256": _sha256(src) if src.exists() else None,
            "dxf": Path(dxf_path).name,
            "dxfversion": doc.dxfversion,
            "units": _UNIT_NAME.get(insunits, f"code {insunits}"),
            "metres_per_unit": _UNIT_TO_M.get(insunits),
            "audit_issues": [getattr(e, "message", str(e)) for e in auditor.errors][:50],
        },
        "layouts": [lay.name for lay in doc.layouts],
        "layers": sorted(layer.dxf.name for layer in doc.layers),
        "title_block": ex.title_block,
        "viewports": ex.viewports,
        "texts": ex.texts,
        "dimensions": ex.dimensions,
        "blocks": ex.block_summary(),
        "linework": ex.linework_summary(_UNIT_TO_M.get(insunits)),
    }


class _Extractor:
    def __init__(self, doc, length_layers, ignore_layers, max_depth):
        self.doc = doc
        self.length_layers = [p.lower() for p in length_layers]
        self.ignore_layers = [p.lower() for p in ignore_layers]
        self.max_depth = max_depth
        self.texts: list[dict] = []
        self.dimensions: list[dict] = []
        self.viewports: list[dict] = []
        self.title_block: dict[str, dict[str, str]] = {}
        self._blocks = defaultdict(lambda: {"count": 0, "layers": set(), "spaces": set()})
        self._lengths = defaultdict(lambda: {"length": 0.0, "entities": 0})
        self._visited_block_defs: set[str] = set()

    # ------------------------------------------------------------------ walk
    def run(self):
        for layout in self.doc.layouts:
            space = "model" if layout.is_modelspace else f"paper:{layout.name}"
            self._scan(layout, space=space, block=None, mult=1, depth=0)
            if not layout.is_modelspace:
                self._viewports(layout)

    def _scan(self, container, *, space, block, mult, depth):
        for e in container:
            t = e.dxftype()
            layer = e.dxf.get("layer", "0")
            if self._ignored(layer) and t != "INSERT":
                continue
            if t in ("TEXT", "MTEXT", "MULTILEADER"):
                self._text(e, space, block)
            elif t == "DIMENSION":
                self._dimension(e, space, block)
            elif t in CURVE_TYPES.split() and space == "model":
                self._length(e, layer, mult)
            elif t == "INSERT":
                self._insert(e, space=space, mult=mult, depth=depth)

    def _insert(self, ins, *, space, mult, depth):
        name = self._effective_name(ins)
        n = mult * max(1, int(getattr(ins, "mcount", 1) or 1))
        rec = self._blocks[name]
        rec["count"] += n
        rec["layers"].add(ins.dxf.get("layer", "0"))
        rec["spaces"].add(space)

        if ins.attribs:
            self.title_block.setdefault(name, {})
            for a in ins.attribs:
                self.title_block[name][a.dxf.tag] = _plain(a)

        blk = self.doc.blocks.get(ins.dxf.name)
        if blk is None or depth >= self.max_depth or _is_xref(blk):
            return
        # Text inside a block definition is collected once, not once per insert.
        collect_text = ins.dxf.name not in self._visited_block_defs
        self._visited_block_defs.add(ins.dxf.name)
        for e in blk:
            t = e.dxftype()
            if t == "INSERT":
                self._insert(e, space=space, mult=n, depth=depth + 1)
            elif t in CURVE_TYPES.split() and space == "model":
                self._length(e, e.dxf.get("layer", "0"), n)
            elif collect_text and t in ("TEXT", "MTEXT", "MULTILEADER"):
                if not self._ignored(e.dxf.get("layer", "0")):
                    self._text(e, space, name)

    # --------------------------------------------------------------- records
    def _text(self, e, space, block):
        text = _plain(e).strip()
        if not text:
            return
        rec = {
            "id": e.dxf.handle,
            "kind": e.dxftype(),
            "layer": e.dxf.get("layer", "0"),
            "space": space,
            "block": block,
            "text": text,
        }
        raw = _raw(e)
        if raw and raw != text:
            rec["raw"] = raw     # MTEXT codes kept: stacked fractions like 5'-10\S1/2; survive
        pos = _position(e)
        if pos is not None:
            rec["xy"] = [round(pos.x, 3), round(pos.y, 3)]
        self.texts.append(rec)

    def _dimension(self, e, space, block):
        try:
            m = e.get_measurement()
            measurement = round(float(m), 4) if isinstance(m, (int, float)) else None
        except Exception:
            measurement = None
        shown, shown_raw = "", ""
        try:
            geo = e.get_geometry_block()
            if geo is not None:
                parts = [x for x in geo if x.dxftype() in ("TEXT", "MTEXT")]
                shown = " ".join(_plain(x) for x in parts).strip()
                shown_raw = " ".join(_raw(x) or _plain(x) for x in parts).strip()
        except Exception:
            pass
        self.dimensions.append({
            "id": e.dxf.handle,
            "layer": e.dxf.get("layer", "0"),
            "space": space,
            "block": block,
            "measurement": measurement,
            "override": e.dxf.get("text", ""),
            "shown": shown,
            "shown_raw": shown_raw,
        })

    def _length(self, e, layer, mult):
        if not self._length_layer(layer):
            return
        L = _entity_length(e)
        if L > 0:
            rec = self._lengths[layer]
            rec["length"] += L * mult
            rec["entities"] += mult

    def _viewports(self, layout):
        for vp in layout.query("VIEWPORT"):
            if vp.dxf.get("id", 0) == 1:     # the layout's own overall viewport
                continue
            h = vp.dxf.get("height", 0) or 0
            vh = vp.dxf.get("view_height", 0) or 0
            factor = round(vh / h, 4) if h else None
            self.viewports.append({
                "id": vp.dxf.handle,
                "layout": layout.name,
                "scale_factor": factor,
                "scale": _arch_scale(factor),
            })

    # ------------------------------------------------------------- summaries
    def block_summary(self):
        out = []
        for name, r in sorted(self._blocks.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
            if name.upper().startswith(("*D", "*T", "*X", "*PAPER", "*MODEL")):
                continue
            out.append({"name": name, "count": r["count"],
                        "layers": sorted(r["layers"]), "spaces": sorted(r["spaces"])})
        return out

    def linework_summary(self, m_per_unit):
        out = []
        for layer, r in sorted(self._lengths.items()):
            out.append({
                "layer": layer,
                "entities": r["entities"],
                "length_units": round(r["length"], 3),
                "length_m": round(r["length"] * m_per_unit, 3) if m_per_unit else None,
            })
        return out

    # --------------------------------------------------------------- helpers
    def _ignored(self, layer):
        return any(fnmatch.fnmatch(layer.lower(), p) for p in self.ignore_layers)

    def _length_layer(self, layer):
        return any(fnmatch.fnmatch(layer.lower(), p) for p in self.length_layers)

    def _effective_name(self, ins):
        """Dynamic blocks are stored as anonymous *U blocks; recover the real name."""
        name = ins.dxf.name
        if not name.startswith("*U"):
            return name
        try:
            br = self.doc.blocks.get(name).block_record
            if br.has_xdata("AcDbBlockRepBTag"):
                for tag in br.get_xdata("AcDbBlockRepBTag"):
                    if tag.code == 1005:
                        orig = self.doc.entitydb.get(tag.value)
                        if orig is not None:
                            return orig.dxf.name
        except Exception:
            pass
        return name


# ------------------------------------------------------------------ utilities
def _plain(e) -> str:
    t = e.dxftype()
    try:
        if t == "MTEXT":
            s = e.plain_text(split=False)
        elif t == "MULTILEADER":
            ctx = e.context
            if ctx.mtext is not None:
                s = plain_mtext(ctx.mtext.default_content)
            else:  # block-content leader (balloon/tag): use its attribute values
                s = " ".join(a.text for a in getattr(e, "block_attribs", []) if a.text)
        else:  # TEXT, ATTRIB
            s = e.plain_text() if hasattr(e, "plain_text") else e.dxf.get("text", "")
    except Exception:
        s = e.dxf.get("text", "") if e.dxf.hasattr("text") else ""
    return _special_chars(s)


def _raw(e) -> str:
    try:
        if e.dxftype() == "MTEXT":
            return e.text
        if e.dxftype() == "MULTILEADER" and e.context.mtext is not None:
            return e.context.mtext.default_content
    except Exception:
        pass
    return ""


def _special_chars(s: str) -> str:
    for code, ch in (("%%c", "Ø"), ("%%C", "Ø"), ("%%d", "°"), ("%%D", "°"), ("%%p", "±"), ("%%P", "±")):
        s = s.replace(code, ch)
    return s


def _position(e):
    try:
        if e.dxftype() == "MULTILEADER":
            return Vec3(e.context.mtext.insert) if e.context.mtext is not None else None
        return Vec3(e.dxf.insert)
    except Exception:
        return None


def _entity_length(e) -> float:
    try:
        if e.dxftype() == "LINE":
            return (Vec3(e.dxf.end) - Vec3(e.dxf.start)).magnitude
        pts = list(ezpath.make_path(e).flattening(distance=0.01))
        return sum((b - a).magnitude for a, b in zip(pts, pts[1:]))
    except Exception:
        return 0.0


def _is_xref(blk) -> bool:
    try:
        return bool(blk.block.dxf.get("flags", 0) & (4 | 8))
    except Exception:
        return False


def _arch_scale(factor: float | None) -> str | None:
    """48 -> 1/4" = 1'-0"; factors that are not an architectural scale -> 1:N."""
    if not factor:
        return None
    paper_in_per_ft = Fraction(12 / factor).limit_denominator(128)
    exact = abs(float(paper_in_per_ft) - 12 / factor) < 1e-6
    if exact and paper_in_per_ft.denominator in (1, 2, 4, 8, 16, 32, 64, 128):
        whole, frac = divmod(paper_in_per_ft, 1)
        txt = (f"{whole} " if whole else "") + (f"{frac}" if frac else "")
        return f'{txt.strip()}" = 1\'-0"  (1:{factor:g})'
    return f"1:{factor:g}"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
