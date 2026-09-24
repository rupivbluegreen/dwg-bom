"""Stage 1 - DWG -> DXF.

DWG is a closed binary format, so nothing downstream reads it directly. We convert
to DXF (an open, text-based CAD format) and let ezdxf read that.

Default: GNU LibreDWG's `dwg2dxf` (GPL-3, `brew install libredwg`).
Fallback: ODA File Converter (free download, not open source) through ezdxf's
odafc add-on. Use it when LibreDWG skips objects you need.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ConversionError(RuntimeError):
    pass


def dwg_to_dxf(dwg: Path, cache_dir: Path, converter: str = "libredwg") -> tuple[Path, list[str]]:
    """Convert `dwg` to DXF in `cache_dir`. Returns (dxf_path, warnings)."""
    dwg = Path(dwg)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dxf = cache_dir / f"{dwg.stem}.{converter}.dxf"

    if dxf.exists() and dxf.stat().st_mtime >= dwg.stat().st_mtime and dxf.stat().st_size > 0:
        return dxf, [f"using cached {dxf.name}"]

    if converter == "libredwg":
        return dxf, _libredwg(dwg, dxf)
    if converter == "oda":
        return dxf, _oda(dwg, dxf)
    raise ConversionError(f"unknown converter {converter!r} (use 'libredwg' or 'oda')")


def _libredwg(dwg: Path, dxf: Path) -> list[str]:
    exe = shutil.which("dwg2dxf")
    if exe is None:
        raise ConversionError("dwg2dxf not found. Install LibreDWG: `brew install libredwg`")

    proc = subprocess.run(
        [exe, "-y", "-o", str(dxf), str(dwg)],
        capture_output=True, text=True, timeout=600,
    )
    # dwg2dxf can exit non-zero while still writing a usable file (it skips objects it
    # cannot decode), so judge by the output file and keep its messages as warnings.
    warnings = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()][:50]
    if not dxf.exists() or dxf.stat().st_size == 0:
        raise ConversionError(f"dwg2dxf produced no output (exit {proc.returncode}):\n" + "\n".join(warnings[:20]))
    if proc.returncode != 0:
        warnings.insert(0, f"dwg2dxf exited with code {proc.returncode}; output kept, check the Flags sheet")
    return warnings


def _oda(dwg: Path, dxf: Path) -> list[str]:
    try:
        from ezdxf.addons import odafc
    except ImportError as exc:  # pragma: no cover
        raise ConversionError("ezdxf is not installed") from exc
    if hasattr(odafc, "is_installed") and not odafc.is_installed():
        raise ConversionError(
            "ODA File Converter not found. Install it from opendesign.com "
            "(on macOS it is expected in /Applications/ODAFileConverter.app)."
        )
    odafc.convert(str(dwg), str(dxf), version="R2018", replace=True)
    return []
