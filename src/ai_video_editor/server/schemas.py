"""Response/request DTOs for the VX sidecar.

These are thin views over the on-disk artifacts. The storyboard itself is
returned as the raw ``EditorialStoryboard`` dump (the single source of truth in
``models.py``) — we do not re-model it here.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProjectSummary(BaseModel):
    """One row in the Library grid. Derived from project.json + cache state."""

    id: str  # directory name under library/
    name: str
    type: str  # "editorial" | "descriptive"
    provider: str = "gemini"
    style: str | None = None
    mode: str = "story"  # "story" | "timeline" (editorial only)
    clip_count: int = 0
    created_at: str | None = None
    style_preset: str | None = None
    has_storyboard: bool = False
    has_rough_cut: bool = False
    latest_version: int | None = None


class CacheStatus(BaseModel):
    source: bool = False
    proxy: bool = False
    frames: bool = False
    scenes: bool = False
    audio: bool = False


class ProjectDetail(ProjectSummary):
    """Full project view for the editor header / status panel."""

    versions: dict = {}
    clips: list[str] = []
    storyboard_path: str | None = None
    rough_cut_path: str | None = None


class CostSummary(BaseModel):
    """Live LLM spend, read from traces.jsonl. Powers the editor status bar."""

    calls: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    by_phase: dict = {}


class CreateProjectRequest(BaseModel):
    name: str
    source_dir: str  # absolute path to a folder of clips
    provider: str = "gemini"
    style: str = "vlog"
    included_clips: list[str] | None = None


class AnalyzeRequest(BaseModel):
    provider: str | None = None  # falls back to project.json provider
    force: bool = False
    visual: bool = False
    timeline: bool = False  # Timeline Mode vs Story Mode
    max_cost: float | None = None


class CutRequest(BaseModel):
    proxy_mode: bool = False  # assemble from cached proxies (offline/fast)
    storyboard_version: int | None = None  # default: latest


class JobInfo(BaseModel):
    """A background pipeline run (create / analyze / cut)."""

    id: str
    kind: str  # "create" | "analyze" | "cut"
    project: str
    status: str  # "queued" | "running" | "completed" | "failed"
    stage: str | None = None  # human label, e.g. "Phase 1: Reviewing clips"
    progress: float | None = None  # 0..1 when derivable, else None
    error: str | None = None
    result: dict | None = None
    cost: CostSummary | None = None
    log_tail: list[str] = []
    duration_sec: float | None = None
