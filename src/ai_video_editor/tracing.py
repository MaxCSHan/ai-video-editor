"""LLM call tracing — token usage, cost estimation, and timing for every API call.

Records traces to library/<project>/traces.jsonl (append-only).
Provides cost estimation for dry-run planning and post-run analysis.
"""

import json
import random
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

MAX_LLM_RETRIES = 3
BASE_RETRY_DELAY_SEC = 2.0


def _is_retryable_gemini(exc: Exception) -> bool:
    """Check if a Gemini API error is transient and worth retrying."""
    name = type(exc).__name__
    if name in (
        "TooManyRequests",
        "ResourceExhausted",
        "ServiceUnavailable",
        "InternalServerError",
        "DeadlineExceeded",
    ):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code in (429, 500, 502, 503)


def _is_retryable_anthropic(exc: Exception) -> bool:
    """Check if an Anthropic API error is transient and worth retrying."""
    name = type(exc).__name__
    if name in (
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "OverloadedError",
    ):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    code = getattr(exc, "status_code", None)
    return code in (429, 500, 502, 503, 529)


# ---------------------------------------------------------------------------
# Cost table (per 1M tokens, USD)
# ---------------------------------------------------------------------------


class CostLimitExceeded(Exception):
    """Raised when cumulative LLM cost exceeds the configured limit."""

    pass


COST_PER_1M_TOKENS = {
    # Gemini 3.x (2026-04 pricing)
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini-3.1-flash-lite-preview": {"input": 0.25, "output": 1.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    # Gemini 2.5
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
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
    retries: int = 0
    validation_warnings: list[str] = field(default_factory=list)
    validation_retried: bool = False


_warned_models: set[str] = set()


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
        if model not in _warned_models:
            _warned_models.add(model)
            print(
                f"  WARN: Unknown model '{model}' for cost estimation — update COST_PER_1M_TOKENS"
            )
        return 0.0
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


# ---------------------------------------------------------------------------
# Trace storage
# ---------------------------------------------------------------------------


class ProjectTracer:
    """Collects traces for a project run and writes to traces.jsonl."""

    def __init__(self, project_root: Path, max_cost_usd: float | None = None):
        self.project_root = project_root
        self.traces: list[LLMCallTrace] = []
        self.traces_path = project_root / "traces.jsonl"
        self.max_cost_usd = max_cost_usd

    def record(self, trace: LLMCallTrace):
        """Record a trace in memory and append to disk. Raises CostLimitExceeded if over budget."""
        self.traces.append(trace)
        with open(self.traces_path, "a") as f:
            f.write(json.dumps(asdict(trace)) + "\n")

        if self.max_cost_usd is not None:
            cumulative = sum(t.estimated_cost_usd for t in self.traces)
            if cumulative >= self.max_cost_usd:
                raise CostLimitExceeded(
                    f"LLM cost ${cumulative:.4f} exceeds limit ${self.max_cost_usd:.2f}. "
                    f"Use --max-cost to increase or --dry-run to estimate first."
                )

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
# Spinner — elapsed-time feedback for long-running LLM calls
# ---------------------------------------------------------------------------


class LLMSpinner:
    """Context manager that prints an updating elapsed-time line during LLM calls.

    Usage::

        with LLMSpinner("Generating visual monologue", provider="gemini"):
            response = client.models.generate_content(...)
    """

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str, *, provider: str = "", detail: str = ""):
        self.label = label
        parts = [p for p in (provider, detail) if p]
        self.suffix = f" ({', '.join(parts)})" if parts else ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time = 0.0

    def __enter__(self):
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        elapsed = time.time() - self._start_time
        # Clear the spinner line and print final status
        sys.stdout.write("\r\033[K")
        if exc_type is None:
            sys.stdout.write(f"  {self.label}{self.suffix} — done ({elapsed:.1f}s)\n")
        else:
            sys.stdout.write(f"  {self.label}{self.suffix} — failed ({elapsed:.1f}s)\n")
        sys.stdout.flush()
        return False

    def _spin(self):
        idx = 0
        while not self._stop.is_set():
            elapsed = time.time() - self._start_time
            frame = self.FRAMES[idx % len(self.FRAMES)]
            sys.stdout.write(f"\r\033[K  {frame} {self.label}{self.suffix}... {elapsed:.0f}s")
            sys.stdout.flush()
            idx += 1
            self._stop.wait(0.15)


# ---------------------------------------------------------------------------
# Phoenix observability (standalone server, dev-only, optional)
#
# Usage:
#   Terminal 1: vx trace              (starts Phoenix server, stays running)
#   Terminal 2: vx analyze my-trip    (auto-connects if server is reachable)
# ---------------------------------------------------------------------------

DEFAULT_TRACE_URL = "http://localhost:6006"

_phoenix_connected = False
_phoenix_url: str | None = None


def _probe_phoenix(url: str, timeout: float = 0.15) -> bool:
    """Check if a Phoenix server is reachable. Uses stdlib only (no extra deps)."""
    import urllib.request

    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def connect_phoenix(url: str | None = None) -> bool:
    """Connect to a running Phoenix server and auto-instrument the Gemini SDK.

    Returns True if connected, False if server unreachable or deps not installed.
    Safe to call multiple times (idempotent).
    """
    import os

    global _phoenix_connected, _phoenix_url
    if _phoenix_connected:
        return True

    url = url or os.environ.get("VX_TRACE_URL", DEFAULT_TRACE_URL)

    if not _probe_phoenix(url):
        return False

    try:
        import logging

        # Suppress verbose OpenTelemetry registration logs
        otel_logger = logging.getLogger("phoenix.otel")
        prev_level = otel_logger.level
        otel_logger.setLevel(logging.WARNING)

        from phoenix.otel import register

        register(
            project_name="vx-pipeline",
            endpoint=f"{url}/v1/traces",
            verbose=False,
        )
        otel_logger.setLevel(prev_level)

        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

        GoogleGenAIInstrumentor().instrument()

        try:
            from openinference.instrumentation.anthropic import AnthropicInstrumentor

            AnthropicInstrumentor().instrument()
        except ImportError:
            pass  # anthropic instrumentor not installed

        _phoenix_connected = True
        _phoenix_url = url
        return True
    except ImportError:
        return False


def start_phoenix_server(port: int = 6006, storage_dir: Path | None = None) -> None:
    """Start Phoenix as a foreground server. Used by `vx trace`. Blocks until Ctrl+C."""
    import os
    import signal
    import threading
    import warnings

    # Suppress SQLAlchemy SAWarning from Phoenix's internal DB schema reflection
    warnings.filterwarnings("ignore", message=".*Skipped unsupported reflection.*")

    try:
        import phoenix as px
    except (ImportError, ModuleNotFoundError) as e:
        raise SystemExit(
            f"\n  Phoenix failed to import: {e}\n\n"
            "  Try reinstalling the tracing extras:\n"
            '    uv pip install -e ".[tracing]" --force-reinstall --no-deps\n'
            "    uv pip install -e \".[tracing]\"\n"
        ) from None

    storage = storage_dir or Path.home() / ".vx" / "phoenix"
    storage.mkdir(parents=True, exist_ok=True)
    os.environ["PHOENIX_WORKING_DIR"] = str(storage)
    os.environ["PHOENIX_PORT"] = str(port)

    # Launch in background thread, then block the main thread until interrupted.
    # use_temp_dir=False ensures Phoenix uses PHOENIX_WORKING_DIR for persistent SQLite.
    session = px.launch_app(run_in_thread=True, use_temp_dir=False)
    if not session:
        raise RuntimeError("Phoenix failed to start")

    # Block until SIGINT (Ctrl+C) or SIGTERM
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()


def get_phoenix_status() -> tuple[bool, str | None]:
    """Return (connected, url) for display in CLI/TUI status lines."""
    return _phoenix_connected, _phoenix_url


# ---------------------------------------------------------------------------
# OTel span helpers for agent tracing
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def otel_session_span(name: str, session_id: str, attributes: dict | None = None):
    """Create an OTel span with session_id for Phoenix Sessions grouping.

    All Gemini API calls within this context will inherit the session_id via
    OpenInference's ``using_session``, making them appear as a single
    conversation in the Phoenix Sessions tab.

    No-op (yields None) when Phoenix is not connected.
    """
    if not _phoenix_connected:
        yield None
        return

    try:
        from openinference.instrumentation import using_session
        from openinference.semconv.trace import SpanAttributes
        from opentelemetry import trace

        tracer = trace.get_tracer("vx-pipeline")
        span_attrs = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: "agent",
            SpanAttributes.SESSION_ID: session_id,
        }
        if attributes:
            span_attrs.update(attributes)

        with tracer.start_as_current_span(name, attributes=span_attrs) as span:
            with using_session(session_id):
                yield span
    except Exception:
        # Tracing should never break the pipeline
        yield None


@contextmanager
def otel_tool_span(tool_name: str, tool_args: dict | None = None):
    """Create a child span for a director tool execution.

    Records tool name, arguments, and (when set by caller) result attributes.
    No-op when Phoenix is not connected.
    """
    if not _phoenix_connected:
        yield None
        return

    try:
        from openinference.semconv.trace import SpanAttributes
        from opentelemetry import trace

        tracer = trace.get_tracer("vx-pipeline")
        attrs = {
            SpanAttributes.OPENINFERENCE_SPAN_KIND: "tool",
            SpanAttributes.TOOL_NAME: tool_name,
        }
        if tool_args:
            attrs[SpanAttributes.INPUT_VALUE] = str(tool_args)[:500]

        with tracer.start_as_current_span(f"tool:{tool_name}", attributes=attrs) as span:
            yield span
    except Exception:
        yield None


@contextmanager
def otel_phase_span(
    phase: str,
    *,
    stage: str | None = None,
    project_name: str | None = None,
    pipeline_run_id: str | None = None,
    clip_id: str | None = None,
    provider: str | None = None,
    call: str | None = None,
    extra_tags: list[str] | None = None,
):
    """Tag all auto-instrumented LLM spans within this block with pipeline metadata.

    Uses OpenInference ``using_attributes`` so that Gemini/Anthropic spans created
    by their respective instrumentors automatically inherit ``metadata`` and ``tags``.

    No-op when Phoenix is not connected.

    Example::

        with otel_phase_span("phase1", stage="review", provider="gemini", clip_id="C0073"):
            response = traced_gemini_generate(...)
        # In Phoenix: metadata.phase="phase1", tags=["phase:phase1", "clip:C0073", ...]
    """
    if not _phoenix_connected:
        yield None
        return

    try:
        from openinference.instrumentation import using_attributes

        metadata: dict[str, str] = {"phase": phase}
        tags: list[str] = [f"phase:{phase}"]

        if stage:
            metadata["stage"] = stage
            tags.append(f"stage:{stage}")
        if project_name:
            metadata["project_name"] = project_name
            tags.append(f"project:{project_name}")
        if pipeline_run_id:
            metadata["pipeline_run_id"] = pipeline_run_id
        if clip_id:
            metadata["clip_id"] = clip_id
            tags.append(f"clip:{clip_id}")
        if provider:
            metadata["provider"] = provider
            tags.append(f"provider:{provider}")
        if call:
            metadata["call"] = call
            tags.append(f"call:{call}")
        if extra_tags:
            tags.extend(extra_tags)

        with using_attributes(metadata=metadata, tags=tags):
            yield
    except Exception:
        # Tracing should never break the pipeline
        yield None


@contextmanager
def otel_pipeline_span(project_name: str, pipeline_run_id: str):
    """Wrap an entire pipeline run so all LLM spans share a session and project tag.

    Uses ``session_id`` for Phoenix session grouping and ``metadata`` for filtering.
    No-op when Phoenix is not connected.
    """
    if not _phoenix_connected:
        yield None
        return

    try:
        from openinference.instrumentation import using_attributes

        with using_attributes(
            session_id=pipeline_run_id,
            metadata={"project_name": project_name, "pipeline_run_id": pipeline_run_id},
            tags=[f"project:{project_name}"],
        ):
            yield
    except Exception:
        yield None


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
    """Wrapper around client.models.generate_content with retry and tracing."""
    start = time.time()
    trace = LLMCallTrace(
        phase=phase,
        provider="gemini",
        model=model,
        clip_id=clip_id,
        num_video_files=num_video_files,
        prompt_chars=prompt_chars,
    )

    last_exc = None
    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            trace.duration_sec = round(time.time() - start, 2)
            trace.retries = attempt

            # Extract token usage from response metadata
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                um = response.usage_metadata
                trace.input_tokens = getattr(um, "prompt_token_count", 0) or 0
                trace.output_tokens = getattr(um, "candidates_token_count", 0) or 0
                trace.total_tokens = getattr(um, "total_token_count", 0) or 0
            trace.estimated_cost_usd = estimate_cost(model, trace.input_tokens, trace.output_tokens)
            trace.success = True

            if tracer:
                tracer.record(trace)
            return response

        except Exception as e:
            last_exc = e
            if attempt < MAX_LLM_RETRIES and _is_retryable_gemini(e):
                delay = BASE_RETRY_DELAY_SEC * (2**attempt) + random.uniform(0, 1)
                print(
                    f"  Retryable error (attempt {attempt + 1}/{MAX_LLM_RETRIES}):"
                    f" {type(e).__name__}"
                )
                print(f"  Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue

            # Non-retryable or retries exhausted
            trace.duration_sec = round(time.time() - start, 2)
            trace.success = False
            trace.error = str(e)
            trace.retries = attempt
            if tracer:
                tracer.record(trace)
            raise

    # Safety net (should not reach here)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Claude traced wrapper
# ---------------------------------------------------------------------------


def traced_claude_generate(
    client,
    *,
    model: str,
    messages: list,
    max_tokens: int,
    temperature: float = 0.2,
    phase: str,
    clip_id: str | None = None,
    tracer: "ProjectTracer | None" = None,
    prompt_chars: int = 0,
):
    """Wrapper around client.messages.create with retry and tracing.

    Mirrors ``traced_gemini_generate`` for the Anthropic API. Returns the full
    ``anthropic.types.Message`` response — callers extract ``.content[0].text``.
    """
    start = time.time()
    trace = LLMCallTrace(
        phase=phase,
        provider="claude",
        model=model,
        clip_id=clip_id,
        prompt_chars=prompt_chars,
    )

    last_exc = None
    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            trace.duration_sec = round(time.time() - start, 2)
            trace.retries = attempt

            # Extract token usage from Anthropic response
            if hasattr(response, "usage") and response.usage:
                trace.input_tokens = response.usage.input_tokens or 0
                trace.output_tokens = response.usage.output_tokens or 0
                trace.total_tokens = trace.input_tokens + trace.output_tokens
            trace.estimated_cost_usd = estimate_cost(model, trace.input_tokens, trace.output_tokens)
            trace.success = True

            if tracer:
                tracer.record(trace)
            return response

        except Exception as e:
            last_exc = e
            if attempt < MAX_LLM_RETRIES and _is_retryable_anthropic(e):
                delay = BASE_RETRY_DELAY_SEC * (2**attempt) + random.uniform(0, 1)
                print(
                    f"  Retryable error (attempt {attempt + 1}/{MAX_LLM_RETRIES}):"
                    f" {type(e).__name__}"
                )
                print(f"  Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue

            # Non-retryable or retries exhausted
            trace.duration_sec = round(time.time() - start, 2)
            trace.success = False
            trace.error = str(e)
            trace.retries = attempt
            if tracer:
                tracer.record(trace)
            raise

    # Safety net (should not reach here)
    raise last_exc  # type: ignore[misc]


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
