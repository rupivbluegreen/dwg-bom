#!/usr/bin/env python3
"""Local web UI for the DWG -> BOM pipeline.

    python app.py

Serves http://localhost:5000. Upload a .dwg or .dxf, run the pipeline (the same
`run_one()` that run.py uses), and download the BOM. Makes no external network
call - stays a fully local tool, like the rest of the project.
"""
from __future__ import annotations

import io
import shutil
import threading
import uuid
from contextlib import redirect_stdout
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

import config
from dwgbom.convert import ConversionError
from run import run_one

UPLOAD_ROOT = Path("instance/uploads")
CLEANUP_AFTER_S = 15 * 60  # auto-deleted after 15 min if unclaimed

DOWNLOAD_KINDS = {
    "bom_xlsx": ("BOM workbook (.xlsx)",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "extraction_json": ("Raw extraction (.json)", "application/json"),
    "bom_json": ("BOM data (.json)", "application/json"),
}

app = FastAPI()
templates = Jinja2Templates(directory="templates")
_runs: dict[str, dict] = {}   # run_id -> run state
_lock = threading.Lock()


def _cleanup(run_id: str) -> None:
    with _lock:
        run_state = _runs.pop(run_id, None)
    if run_state:
        shutil.rmtree(run_state["dir"], ignore_errors=True)


def _schedule_cleanup(run_id: str) -> None:
    timer = threading.Timer(CLEANUP_AFTER_S, _cleanup, args=(run_id,))
    timer.daemon = True
    timer.start()
    _runs[run_id]["timer"] = timer


def _base_context() -> dict:
    return {"llm_default": config.LLM.get("enabled", True), "kinds": DOWNLOAD_KINDS}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _base_context())


@app.post("/run", response_class=HTMLResponse)
async def run(request: Request, drawing: UploadFile | None = File(None), use_llm: str | None = Form(None)):
    context = _base_context()
    if not drawing or not drawing.filename:
        context["error"] = "Choose a .dwg or .dxf file first."
        return templates.TemplateResponse(request, "index.html", context, status_code=400)

    suffix = Path(drawing.filename).suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        context["error"] = f"Unsupported file type '{suffix}'. Upload a .dwg or .dxf drawing."
        return templates.TemplateResponse(request, "index.html", context, status_code=400)

    run_id = uuid.uuid4().hex[:12]
    run_dir = UPLOAD_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    upload_path = run_dir / Path(drawing.filename).name
    upload_path.write_bytes(await drawing.read())

    use_llm_flag = config.LLM.get("enabled", True) and use_llm == "on"
    llm_cfg = dict(config.LLM) if use_llm_flag else None

    log = io.StringIO()
    try:
        with redirect_stdout(log):
            stats = run_one(upload_path, run_dir, config.CONVERTER, llm_cfg)
    except ConversionError as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        context["error"] = f"Could not convert the drawing: {exc}"
        context["log"] = log.getvalue()
        return templates.TemplateResponse(request, "index.html", context, status_code=400)
    except Exception as exc:  # don't leak a stack trace
        shutil.rmtree(run_dir, ignore_errors=True)
        context["error"] = f"The pipeline failed: {exc}"
        context["log"] = log.getvalue()
        return templates.TemplateResponse(request, "index.html", context, status_code=500)
    finally:
        upload_path.unlink(missing_ok=True)  # drawing no longer needed

    files = {kind: Path(stats[kind]).name for kind in DOWNLOAD_KINDS if stats.get(kind)}
    with _lock:
        _runs[run_id] = {"dir": run_dir, "files": files, "downloaded": set()}
    _schedule_cleanup(run_id)

    context.update(log=log.getvalue(), run_id=run_id, files=files, stats=stats)
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/download/{run_id}/{kind}")
async def download(run_id: str, kind: str):
    with _lock:
        run_state = _runs.get(run_id)
    if not run_state or kind not in run_state["files"]:
        raise HTTPException(status_code=404)
    path = run_state["dir"] / run_state["files"][kind]
    if not path.exists():
        raise HTTPException(status_code=404)

    data = path.read_bytes()  # read first, avoids race with cleanup
    _, mimetype = DOWNLOAD_KINDS[kind]

    with _lock:
        run_state["downloaded"].add(kind)
        all_downloaded = run_state["downloaded"] >= set(run_state["files"])
    if all_downloaded:
        run_state["timer"].cancel()
        _cleanup(run_id)

    headers = {"Content-Disposition": f'attachment; filename="{path.name}"'}
    return Response(content=data, media_type=mimetype, headers=headers)


if __name__ == "__main__":
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=5000)
