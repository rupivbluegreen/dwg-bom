# Notice: LibreDWG and this project's MIT licence

This project (dwg-bom) is licensed under the MIT licence (see `LICENSE`). It
depends on **LibreDWG**, which is licensed under the **GPL-3**, and this
notice explains why that dependency does not put dwg-bom itself under GPL-3.

## Why they don't mix, and why that's fine here

GPL-3's copyleft applies to works that **link with or incorporate** GPL code.
dwg-bom never does either:

1. **LibreDWG is invoked as a separate program, not a library.**
   `dwgbom/convert.py` calls the `dwg2dxf` command-line tool via
   `subprocess.run(...)`, reading its output file back in. There is no
   linking, no shared process memory, and no LibreDWG source or binary
   embedded in this codebase. Two independent programs communicating through
   a file on disk are not treated as a combined/derivative work under the
   GPL.

2. **LibreDWG is not distributed with dwg-bom.** This repository contains no
   LibreDWG source, headers or compiled binaries. Anyone using dwg-bom
   installs LibreDWG themselves, separately, from its own project
   (`brew install libredwg` or their own OS package). dwg-bom's own
   distribution consists only of MIT-licensed Python code.

3. **The rest of the pipeline has no GPL dependency.** ezdxf, openpyxl,
   FastAPI, Uvicorn, Ollama and Qwen3.8 are MIT / BSD / Apache-2.0. LibreDWG
   is the only GPL component in the stack, and it stays isolated to stage 1
   (convert), behind a subprocess boundary, with the ODA File Converter
   (proprietary freeware) as an alternative for that same stage.

## What this means in practice

- dwg-bom's source code is, and remains, MIT licensed.
- If you fork dwg-bom, modify its Python code, and redistribute it, you are
  bound only by the MIT licence's terms (attribution).
- If you bundle a LibreDWG binary *inside* your own distribution rather than
  asking users to install it separately, that packaging choice would bring
  GPL-3's terms into play for the bundle you're distributing — that's a
  packaging decision on your end, not something dwg-bom's own licence
  imposes.
