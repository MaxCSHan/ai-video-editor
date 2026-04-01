"""LLM call tracing — token usage, cost estimation, and timing for every API call.

Records traces to library/<project>/traces.jsonl (append-only).
Provides cost estimation for dry-run planning and post-run analysis.
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Cost table (per 1M tokens, USD)
# ---------------------------------------------------------------------------

COST_PER_1M_TOKENS = {
    # Gemini
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-3-flash-preview": {"input": 0.15, "output": 0.60},
    # Claude
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

# Gemini video token estimation: ~263 tokens per second of video at 1fps
GEMINI_VIDEO_TOKENS_PER_SEC = 263


# ---------------------------------------------------------------------------
# Trace data
# ---------------------------------------------------------------------------


@dataclass
class LLMCallTrace:
    call_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str = ""  # "transcribe" | "phase1" | "phase2" | "briefing_scan"
    provider: str = ""  # "gemini" | "claude"
    model: str = ""
    clip_id: str | None = None

    # Token usage
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    # Cost
    estimated_cost_usd: float = 0.0

    # Timing
    duration_sec: float = 0.0

    # Context size
    num_video_files: int = 0
    prompt_chars: int = 0

    # Quality
    success: bool = True
    error: str | None = None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    rates = COST_PER_1M_TOKENS.get(model)
    if not rates:
        # Try prefix matching (e.g., "gemini-2.5-flash-001" → "gemini-2.5-flash")
        for key in COST_PER_1M_TOKENS:
            if model.startswith(key):
                rates = COST_PER_1M_TOKENS[key]
                break
    if not rates:
        return 0.0
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


# ---------------------------------------------------------------------------
# Trace storage
# ---------------------------------------------------------------------------


class ProjectTracer:
    """Collects traces for a project run and writes to traces.jsonl."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.traces: list[LLMCallTrace] = []
        self.traces_path = project_root / "traces.jsonl"

    def record(self, trace: LLMCallTrace):
        """Record a trace in memory and append to disk."""
        self.traces.append(trace)
        with open(self.traces_path, "a") as f:
            f.write(json.dumps(asdict(trace)) + "\n")

    def summary(self) -> dict:
        """Summarize all traces from this run."""
        if not self.traces:
            return {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
        return {
            "calls": len(self.traces),
            "total_tokens": sum(t.total_tokens for t in self.traces),
            "input_tokens": sum(t.input_tokens for t in self.traces),
            "output_tokens": sum(t.output_tokens for t in self.traces),
            "estimated_cost_usd": sum(t.estimated_cost_usd for t in self.traces),
            "total_duration_sec": sum(t.duration_sec for t in self.traces),
            "errors": sum(1 for t in self.traces if not t.success),
        }

    def print_summary(self, label: str = "LLM Usage"):
        """Print a formatted summary line."""
        s = self.summary()
        if s["calls"] == 0:
            return
        cost = s["estimated_cost_usd"]
        tokens = s["total_tokens"]
        dur = s["total_duration_sec"]
        errors = s["errors"]
        parts = [
            f"{s['calls']} calls",
            f"{tokens:,} tokens",
            f"~${cost:.4f}",
            f"{dur:.1f}s",
        ]
        if errors:
            parts.append(f"{errors} errors")
        print(f"  [{label}] {' | '.join(parts)}")


def load_all_traces(project_root: Path) -> list[dict]:
    """Load all historical traces for a project."""
    traces_path = project_root / "traces.jsonl"
    if not traces_path.exists():
        return []
    traces = []
    for line in traces_path.read_text().strip().split("\n"):
        if line:
            traces.append(json.loads(line))
    return traces


def summarize_traces(traces: list[dict]) -> dict:
    """Summarize historical traces."""
    if not traces:
        return {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
    return {
        "calls": len(traces),
        "total_tokens": sum(t.get("total_tokens", 0) for t in traces),
        "input_tokens": sum(t.get("input_tokens", 0) for t in traces),
        "output_tokens": sum(t.get("output_tokens", 0) for t in traces),
        "estimated_cost_usd": sum(t.get("estimated_cost_usd", 0) for t in traces),
        "by_phase": _group_by_phase(traces),
    }


def _group_by_phase(traces: list[dict]) -> dict:
    """Group trace summaries by phase."""
    phases: dict[str, dict] = {}
    for t in traces:
        phase = t.get("phase", "unknown")
        if phase not in phases:
            phases[phase] = {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
        phases[phase]["calls"] += 1
        phases[phase]["total_tokens"] += t.get("total_tokens", 0)
        phases[phase]["estimated_cost_usd"] += t.get("estimated_cost_usd", 0)
    return phases


# ---------------------------------------------------------------------------
# Gemini traced wrapper
# ---------------------------------------------------------------------------


def traced_gemini_generate(
    client,
    *,
    model: str,
    contents,
    config,
    phase: str,
    clip_id: str | None = None,
    tracer: ProjectTracer | None = None,
    num_video_files: int = 0,
    prompt_chars: int = 0,
):
    """Wrapper around client.models.generate_content that records a trace."""
    start = time.time()
    trace = LLMCallTrace(
        phase=phase,
        provider="gemini",
        model=model,
        clip_id=clip_id,
        num_video_files=num_video_files,
        prompt_chars=prompt_chars,
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        trace.duration_sec = round(time.time() - start, 2)

        # Extract token usage from response metadata
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            trace.input_tokens = getattr(um, "prompt_token_count", 0) or 0
            trace.output_tokens = getattr(um, "candidates_token_count", 0) or 0
            trace.total_tokens = getattr(um, "total_token_count", 0) or 0
        trace.estimated_cost_usd = estimate_cost(model, trace.input_tokens, trace.output_tokens)
        trace.success = True

    except Exception as e:
        trace.duration_sec = round(time.time() - start, 2)
        trace.success = False
        trace.error = str(e)
        if tracer:
            tracer.record(trace)
        raise

    if tracer:
        tracer.record(trace)
    return response


# ---------------------------------------------------------------------------
# Dry-run estimation
# ---------------------------------------------------------------------------


def estimate_phase1_cost(
    clip_count: int,
    avg_clip_duration_sec: float,
    model: str = "gemini-3-flash-preview",
) -> dict:
    """Estimate Phase 1 cost (one LLM call per clip with video)."""
    video_tokens = int(avg_clip_duration_sec * GEMINI_VIDEO_TOKENS_PER_SEC)
    prompt_tokens = 2000  # approximate text prompt
    input_per_clip = video_tokens + prompt_tokens
    output_per_clip = 2000  # approximate review JSON

    total_input = input_per_clip * clip_count
    total_output = output_per_clip * clip_count
    cost = estimate_cost(model, total_input, total_output)

    return {
        "calls": clip_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "estimated_cost_usd": cost,
    }


def estimate_phase2_cost(
    clip_count: int,
    reviews_chars: int,
    model: str = "gemini-3-flash-preview",
    visual: bool = False,
    total_video_duration_sec: float = 0,
) -> dict:
    """Estimate Phase 2 cost (one LLM call with all reviews + optional video)."""
    # Text tokens: ~4 chars per token
    text_tokens = reviews_chars // 4
    video_tokens = int(total_video_duration_sec * GEMINI_VIDEO_TOKENS_PER_SEC) if visual else 0
    input_tokens = text_tokens + video_tokens
    output_tokens = 4000  # approximate storyboard JSON

    cost = estimate_cost(model, input_tokens, output_tokens)

    return {
        "calls": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "video_tokens": video_tokens,
        "estimated_cost_usd": cost,
    }


def estimate_transcription_cost(
    clip_count: int,
    avg_clip_duration_sec: float,
    model: str = "gemini-2.5-flash",
) -> dict:
    """Estimate Gemini transcription cost (one call per clip with video)."""
    video_tokens = int(avg_clip_duration_sec * GEMINI_VIDEO_TOKENS_PER_SEC)
    prompt_tokens = 500
    input_per_clip = video_tokens + prompt_tokens
    output_per_clip = 1500  # approximate transcript JSON

    total_input = input_per_clip * clip_count
    total_output = output_per_clip * clip_count
    cost = estimate_cost(model, total_input, total_output)

    return {
        "calls": clip_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "estimated_cost_usd": cost,
    }


def estimate_monologue_cost(
    clip_count: int,
    model: str = "gemini-3-flash-preview",
) -> dict:
    """Estimate Phase 3 (Visual Monologue) cost — single text-only LLM call."""
    # Input: storyboard JSON (~3K tokens) + transcripts (~1K per clip) + prompt (~2K)
    input_tokens = 5000 + clip_count * 1000
    output_tokens = 3000  # approximate monologue plan JSON

    cost = estimate_cost(model, input_tokens, output_tokens)

    return {
        "calls": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost,
    }
