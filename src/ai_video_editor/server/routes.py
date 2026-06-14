"""REST + WebSocket routes for the VX sidecar.

Read endpoints serve on-disk artifacts under ``library/``. Mutating endpoints
dispatch to the SAME pipeline functions the CLI uses, wrapped as background jobs.
Heavy imports (the pipeline, which pulls google/anthropic SDKs) are deferred to
job execution so the server boots instantly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..config import Config
from ..versioning import resolve_versioned_path
from .jobs import REGISTRY
from .schemas import (
    AnalyzeRequest,
    CostSummary,
    CreateProjectRequest,
    CutRequest,
    JobInfo,
    ProjectDetail,
    ProjectSummary,
)

router = APIRouter()
CFG = Config()


# --------------------------------------------------------------------------
# Disk helpers (mirror cli.py conventions; reuse versioning resolution)
# --------------------------------------------------------------------------
def _meta_path(name: str) -> Path:
    return CFG.library_dir / name / "project.json"


def _read_meta(name: str) -> dict | None:
    p = _meta_path(name)
    return json.loads(p.read_text()) if p.exists() else None


def _write_meta(name: str, meta: dict):
    _meta_path(name).write_text(json.dumps(meta, indent=2))


def find_storyboard_json(ep) -> Path | None:
    sd = ep.storyboard
    candidates = [sd / "editorial_gemini_latest.json", sd / "editorial_claude_latest.json"]
    if sd.exists():
        candidates.extend(sorted(sd.glob("editorial_*_v*.json"), reverse=True))
    for c in candidates:
        if c.exists():
            return resolve_versioned_path(c)
    return None


def find_rough_cut(ep) -> Path | None:
    exports = ep.exports
    if not exports.exists():
        return None
    cuts = sorted(exports.glob("v*/rough_cut.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cuts[0] if cuts else None


def find_proxy(ep, clip_id: str) -> Path | None:
    pp = ep.clip_paths(clip_id)
    if not pp.proxy.exists():
        return None
    proxies = sorted(pp.proxy.glob("*.mp4"))
    return proxies[0] if proxies else None


def _summary(name: str, meta: dict) -> ProjectSummary:
    ep = CFG.editorial_project(name)
    sb = find_storyboard_json(ep)
    latest_version = None
    if sb:
        # editorial_gemini_v4.json → 4
        stem = sb.stem
        if "_v" in stem:
            try:
                latest_version = int(stem.rsplit("_v", 1)[1])
            except ValueError:
                latest_version = None
    return ProjectSummary(
        id=name,
        name=meta.get("name", name),
        type=meta.get("type", "editorial"),
        provider=meta.get("provider", "gemini"),
        style=meta.get("style"),
        mode=meta.get("mode", "story"),
        clip_count=meta.get("clip_count", 0),
        created_at=meta.get("created_at"),
        style_preset=meta.get("style_preset"),
        has_storyboard=sb is not None,
        has_rough_cut=find_rough_cut(ep) is not None,
        latest_version=latest_version,
    )


def _cost(name: str) -> CostSummary:
    from ..tracing import load_all_traces, summarize_traces

    s = summarize_traces(load_all_traces(CFG.library_dir / name))
    return CostSummary(**{k: s.get(k, 0) for k in CostSummary.model_fields})


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------
@router.get("/health")
def health():
    return {"ok": True, "library": str(CFG.library_dir.resolve())}


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    lib = CFG.library_dir
    if not lib.exists():
        return []
    out = []
    for d in sorted(lib.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_meta(d.name)
        if meta:
            out.append(_summary(d.name, meta))
    return out


@router.get("/projects/{name}", response_model=ProjectDetail)
def get_project(name: str):
    meta = _read_meta(name)
    if not meta:
        raise HTTPException(404, f"Project '{name}' not found")
    ep = CFG.editorial_project(name)
    base = _summary(name, meta).model_dump()
    sb = find_storyboard_json(ep)
    rc = find_rough_cut(ep)
    return ProjectDetail(
        **base,
        versions=meta.get("versions", {}),
        clips=ep.discover_clips(),
        storyboard_path=str(sb) if sb else None,
        rough_cut_path=str(rc) if rc else None,
    )


@router.get("/projects/{name}/storyboard")
def get_storyboard(name: str):
    ep = CFG.editorial_project(name)
    sb = find_storyboard_json(ep)
    if not sb:
        raise HTTPException(404, "No storyboard yet — run analyze")
    return json.loads(sb.read_text())


@router.get("/projects/{name}/cost", response_model=CostSummary)
def get_cost(name: str):
    if not _read_meta(name):
        raise HTTPException(404, f"Project '{name}' not found")
    return _cost(name)


@router.get("/projects/{name}/clips")
def get_clips(name: str):
    ep = CFG.editorial_project(name)
    clips = ep.discover_clips()
    return [{"clip_id": c, "has_proxy": find_proxy(ep, c) is not None} for c in clips]


@router.get("/projects/{name}/timings")
def get_timings(name: str):
    """Per-stage wall-clock timings (job durations, etc.) for honest perf display."""
    from ..tracing import load_stage_timings

    if not _read_meta(name):
        raise HTTPException(404, f"Project '{name}' not found")
    return load_stage_timings(CFG.library_dir / name)


# --------------------------------------------------------------------------
# Media streaming (for the AVPlayer in the editor)
# --------------------------------------------------------------------------
@router.get("/media/proxy/{name}/{clip_id}")
def media_proxy(name: str, clip_id: str):
    ep = CFG.editorial_project(name)
    p = find_proxy(ep, clip_id)
    if not p:
        raise HTTPException(404, "No proxy for clip")
    return FileResponse(p, media_type="video/mp4")


@router.get("/media/roughcut/{name}")
def media_roughcut(name: str):
    ep = CFG.editorial_project(name)
    rc = find_rough_cut(ep)
    if not rc:
        raise HTTPException(404, "No rough cut yet — run cut")
    return FileResponse(rc, media_type="video/mp4")


# --------------------------------------------------------------------------
# Mutating endpoints → background jobs
# --------------------------------------------------------------------------
def _do_create(req: CreateProjectRequest) -> dict:
    from ..editorial_agent import build_master_manifest, discover_source_clips, preprocess_all_clips

    source = Path(req.source_dir).expanduser()
    clips = discover_source_clips(source)
    if req.included_clips:
        keep = set(req.included_clips)
        clips = [c for c in clips if c.stem in keep]
    if not clips:
        raise RuntimeError(f"No video files found in {source}")
    ep = CFG.editorial_project(req.name)
    ep.ensure_dirs()
    _write_meta(
        req.name,
        {
            "name": req.name,
            "type": "editorial",
            "provider": req.provider,
            "style": req.style,
            "source_dir": str(source),
            "clip_count": len(clips),
            "mode": "story",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"[1/1] Preprocessing {len(clips)} clips...")
    clip_metadata = preprocess_all_clips(clips, ep, CFG.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, req.name)
    return {"clips": len(clips), "duration_sec": manifest.get("total_duration_sec", 0)}


def _do_analyze(name: str, req: AnalyzeRequest) -> dict:
    from ..config import Config as _Config
    from ..editorial_agent import run_editorial_pipeline

    meta = _read_meta(name)
    if not meta:
        raise RuntimeError(f"Project '{name}' not found")
    cfg = _Config()
    if req.timeline:
        cfg.gemini.use_timeline_mode = True
    provider = req.provider or meta.get("provider", "gemini")
    style = meta.get("style", "vlog")
    source = Path(meta["source_dir"]).expanduser()
    sb_path = run_editorial_pipeline(
        source,
        name,
        provider=provider,
        style=style,
        cfg=cfg,
        force=req.force,
        interactive=False,
        visual=req.visual,
        max_cost=req.max_cost,
    )
    meta["mode"] = "timeline" if req.timeline else meta.get("mode", "story")
    _write_meta(name, meta)
    return {"storyboard": str(sb_path)}


def _do_cut(name: str, req: CutRequest) -> dict:
    from ..rough_cut import run_rough_cut

    ep = CFG.editorial_project(name)
    if req.storyboard_version is not None:
        matches = list(ep.storyboard.glob(f"editorial_*_v{req.storyboard_version}.json"))
        sb_path = matches[0] if matches else None
    else:
        sb_path = find_storyboard_json(ep)
    if not sb_path:
        raise RuntimeError("No storyboard found — run analyze first")
    result = run_rough_cut(sb_path, ep, proxy_mode=req.proxy_mode)
    return result if isinstance(result, dict) else {"ok": True}


def _job_to_info(job) -> JobInfo:
    snap = job.snapshot()
    cost = None
    if job.project_root is not None:
        try:
            cost = _cost(job.project)
        except Exception:
            cost = None
    return JobInfo(
        id=snap["id"], kind=snap["kind"], project=snap["project"], status=snap["status"],
        stage=snap["stage"], progress=snap["progress"], error=snap["error"],
        result=snap["result"], cost=cost, log_tail=snap["log_tail"],
        duration_sec=snap.get("duration_sec"),
    )


@router.post("/projects", response_model=JobInfo)
def create_project(req: CreateProjectRequest):
    root = CFG.library_dir / req.name
    job = REGISTRY.submit("create", req.name, root, lambda: _do_create(req))
    return _job_to_info(job)


@router.post("/projects/{name}/analyze", response_model=JobInfo)
def analyze_project(name: str, req: AnalyzeRequest):
    if not _read_meta(name):
        raise HTTPException(404, f"Project '{name}' not found")
    root = CFG.library_dir / name
    job = REGISTRY.submit("analyze", name, root, lambda: _do_analyze(name, req))
    return _job_to_info(job)


@router.post("/projects/{name}/cut", response_model=JobInfo)
def cut_project(name: str, req: CutRequest):
    if not _read_meta(name):
        raise HTTPException(404, f"Project '{name}' not found")
    root = CFG.library_dir / name
    job = REGISTRY.submit("cut", name, root, lambda: _do_cut(name, req))
    return _job_to_info(job)


@router.get("/jobs", response_model=list[JobInfo])
def list_jobs():
    return [_job_to_info(j) for j in REGISTRY.all()]


@router.get("/jobs/{job_id}", response_model=JobInfo)
def get_job(job_id: str):
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_to_info(job)


@router.websocket("/jobs/{job_id}/ws")
async def job_ws(ws: WebSocket, job_id: str):
    await ws.accept()
    job = REGISTRY.get(job_id)
    if not job:
        await ws.send_json({"error": "job not found"})
        await ws.close()
        return
    q = REGISTRY.subscribe(job_id)
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                snap = await loop.run_in_executor(None, q.get, True, 1.0)
            except Exception:
                snap = REGISTRY.get(job_id).snapshot() if REGISTRY.get(job_id) else None
            if snap is None:
                continue
            payload = dict(snap)
            try:
                payload["cost"] = _cost(job.project).model_dump()
            except Exception:
                payload["cost"] = None
            await ws.send_json(payload)
            if snap.get("status") in ("completed", "failed"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        REGISTRY.unsubscribe(job_id, q)
        try:
            await ws.close()
        except Exception:
            pass
