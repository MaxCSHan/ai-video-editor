# Plan: LLM Invocation Architecture (Quality + Cost)

**Status:** Planned (execute after codebase hardening batch 1-5)
**Priority:** High — quality and cost are the primary bottlenecks
**Created:** 2026-04-03
**Updated:** 2026-04-03 — platform decision, dev-only design, agent readiness
**Depends on:** `plan-codebase-hardening.md` (Batches 1-5) — **complete**

## Context

The current LLM integration works but has structural issues that make result quality hard to improve and costs hard to control:

1. **No observability** — `traces.jsonl` records tokens/cost/timing per call, but there's no way to evaluate output quality, compare across runs, or debug why a particular storyboard was poor. The tracing is hand-rolled and limited.
2. **No retry for transient failures** — a single 429 or network timeout kills the call. The only retry mechanism is the interactive `_retry_failed_phase1()` which requires manual confirmation.
3. **No response validation** — LLM outputs are parsed and immediately accepted. Bad timestamps, hallucinated clip IDs, empty segments all pass through.
4. **No cost guardrails** — no spending limit, no budget alerts, estimates disconnected from actuals.
5. **Client constructed per-call** — `genai.Client()` instantiated 6+ times across the codebase.

---

## Platform Decision: Arize Phoenix

### Why Phoenix

After evaluating 10 platforms (LangSmith, Langfuse, Opik, Phoenix, Braintrust, Helicone, Lunary, AgentOps, OpenLIT, Parea), **Arize Phoenix** is the best fit for VX based on these criteria:

| Criterion | Phoenix | Why it wins |
|---|---|---|
| **Dev-only / optional** | `pip install arize-phoenix` — no Docker, no accounts, no API keys | Lowest-friction optional dep. Users who just run `vx` never see it. |
| **CLI debuggability** | Zero-auth local access, returns **pandas DataFrames** | Claude Code can fetch+analyze traces with one-liner Python. No web UI needed. |
| **Gemini support** | Auto-instrumentation via `openinference-instrumentation-google-genai` | 2 lines of setup, all `generate_content()` calls traced automatically. |
| **Claude support** | Auto-instrumentation via `openinference-instrumentation-anthropic` | Same pattern for Claude `messages.create()`. |
| **Agent-ready** | OpenTelemetry spans are inherently hierarchical | Any future agent framework (LangGraph, custom loops) works with OTel nesting. |
| **Cost** | Apache 2.0, fully free, self-hosted, no feature gates | No limits, no cloud dependency. |
| **Maturity** | ~8K GitHub stars, active development | Solid community, backed by Arize AI. |

### Alternatives Considered

| Platform | Why not primary | When to reconsider |
|---|---|---|
| **Langfuse** (22K stars, MIT) | Requires Docker + Postgres for self-host. Can't filter traces by arbitrary metadata via API — only tags. Needs API keys even locally. | If we need cloud-hosted dashboards for sharing with collaborators. |
| **Opik** (4K stars, Apache 2.0) | Best metadata filtering (`filter_string`), uniquely captures video attachments in traces. But requires Docker for self-host. | If video-in-trace debugging becomes critical for diagnosing Phase 1 quality. |
| **LangSmith** | Most mature, but LangChain ecosystem lock-in. Proprietary. | If we adopt LangGraph as agent framework. |

### Design Principle: Dev-Only Tracing

**Users who just run `vx` should never need to install or configure tracing.**

Tracing is a dev/debugging tool. The integration must be:
1. **Optional dependency** — not in core `[project.dependencies]`
2. **Graceful degradation** — if Phoenix not installed, everything works exactly as before
3. **Zero config for users** — no env vars, no accounts, no Docker
4. **Lazy initialization** — Phoenix only starts when a dev explicitly enables it

```toml
# pyproject.toml
[project.optional-dependencies]
tracing = [
    "arize-phoenix>=8.0",
    "openinference-instrumentation-google-genai",
    "openinference-instrumentation-anthropic",
]
dev = ["ruff", "pytest", "arize-phoenix", "openinference-instrumentation-google-genai", "openinference-instrumentation-anthropic"]
```

```python
# tracing.py — initialization (dev-only, no-op for users)
_phoenix_initialized = False

def _init_phoenix():
    """Start Phoenix tracing if installed. No-op otherwise."""
    global _phoenix_initialized
    if _phoenix_initialized:
        return
    _phoenix_initialized = True
    try:
        import phoenix as px
        from phoenix.otel import register
        px.launch_app(run_in_thread=True)  # background, non-blocking
        register(project_name="vx-pipeline")

        # Auto-instrument Gemini
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        GoogleGenAIInstrumentor().instrument()

        # Auto-instrument Claude
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
    except ImportError:
        pass  # Phoenix not installed — user mode, skip silently
```

### CLI Debuggability — How Claude Code Uses Traces

The key advantage of Phoenix for this project: **I (Claude Code) can programmatically fetch and analyze traces during a coding session** without needing a browser or web UI.

```python
# Example: debug why Phase 2 produced bad timestamps
import phoenix as px
client = px.Client()

# Get all LLM spans from the last run
spans = client.get_spans(project_name="vx-pipeline")
llm_spans = spans[spans["span_kind"] == "LLM"]

# Find the Phase 2 call
phase2 = llm_spans[llm_spans["name"].str.contains("phase2")]

# See what prompt was sent
print(phase2["attributes.llm.input_messages"].values[0])

# See what the LLM returned
print(phase2["attributes.llm.output_messages"].values[0])

# Check token usage and cost
print(f"Tokens: {phase2['attributes.llm.token_count.total'].values[0]}")

# Get all spans for a specific clip
clip_spans = spans[spans["attributes"].apply(
    lambda a: a.get("clip_id") == "20260330_C0059"
)]

# Export for deeper analysis
spans.to_json("debug_traces.jsonl", orient="records", lines=True)
```

This means when you say "the storyboard for my-trip has bad timestamps, debug it", I can:
1. Fetch the Phase 2 trace
2. Read the exact prompt and response
3. Identify where timestamps went wrong
4. Suggest prompt or validation fixes

### Agent Framework Readiness

Phoenix uses OpenTelemetry spans, which are inherently hierarchical. This means:

**Current state** — each LLM call is a flat span:
```
trace: vx-analyze
  └─ span: phase1/clip_C0059 (LLM call)
  └─ span: phase1/clip_C0073 (LLM call)
  └─ span: phase2 (LLM call)
```

**Future agent state** — self-correcting loops nest naturally:
```
trace: vx-analyze
  └─ span: self_correcting_phase2
      └─ span: attempt_0
      │   └─ span: call_gemini (LLM)
      │   └─ span: validate_storyboard
      └─ span: attempt_1
      │   └─ span: call_gemini (LLM, with feedback)
      │   └─ span: validate_storyboard
      └─ span: attempt_2 (success)
          └─ span: call_gemini (LLM, with feedback)
          └─ span: validate_storyboard
```

No framework needed — just OTel context managers on existing functions:
```python
from opentelemetry import trace
tracer = trace.get_tracer("vx.editorial")

def self_correcting_phase2(reviews, ...):
    with tracer.start_as_current_span("self_correcting_phase2") as span:
        for attempt in range(3):
            with tracer.start_as_current_span(f"attempt_{attempt}"):
                storyboard = call_gemini_phase2(reviews, ...)  # auto-traced by instrumentor
                with tracer.start_as_current_span("validate"):
                    warnings = validate_storyboard(storyboard, reviews)
                if not warnings:
                    span.set_attribute("attempts", attempt + 1)
                    return storyboard
```

If we later adopt a framework (LangGraph, Instructor, etc.), it plugs into the same OTel pipeline. Phoenix traces it all.

---

## Task List

### Phase A: Phoenix Integration (Dev-Only)

- [ ] **A.1** Add `arize-phoenix` + OpenInference instrumentors to `[project.optional-dependencies]` in `pyproject.toml`
- [ ] **A.2** Add `_init_phoenix()` to `tracing.py` — lazy init, graceful ImportError fallback
- [ ] **A.3** Call `_init_phoenix()` at pipeline start (`run_editorial_pipeline()`) — only when `VX_TRACING=1` env var is set
- [ ] **A.4** Add `traced_claude_generate()` wrapper (currently Claude calls are untraced)
- [ ] **A.5** Keep `traces.jsonl` + `ProjectTracer` as always-on local fallback — Phoenix supplements, doesn't replace
- [ ] **A.6** Add `vx debug-traces` CLI command — thin wrapper around `px.Client().get_spans()` for quick terminal inspection

### Phase B: Retry & Resilience

- [ ] **B.1** Add automatic retry with exponential backoff to `traced_gemini_generate()`
- [ ] **B.2** Distinguish retryable (429, 500, 502, 503, network timeout) from permanent errors
- [ ] **B.3** Add retry wrapper for Claude `client.messages.create()` calls
- [ ] **B.4** Shared LLM client factory — replace `genai.Client()` instantiations with `_get_gemini_client()`
- [ ] **B.5** Rate limiting — respect Gemini's QPM limits in parallel Phase 1 workers

### Phase C: Response Quality Validation

- [ ] **C.1** Phase 1 validation: timestamps within clip duration, in_sec < out_sec, non-empty segments
- [ ] **C.2** Phase 2 validation: clip IDs exist, segments have valid timestamps, total duration sanity
- [ ] **C.3** Transcription validation: timestamps within clip duration, non-empty for clips with known speech
- [ ] **C.4** Auto-retry on validation failure — inject validation feedback into retry prompt
- [ ] **C.5** Log validation results as OTel span attributes (visible in Phoenix traces)

### Phase D: Cost Management

- [ ] **D.1** `--max-cost` flag for `vx analyze` — abort if cumulative cost exceeds threshold
- [ ] **D.2** Running cost display after each phase completion
- [ ] **D.3** Estimate vs actual comparison printed after pipeline completion
- [ ] **D.4** Per-project cost history accessible via `vx status`

### Phase E: Evaluation & Regression Testing

- [ ] **E.1** Build golden dataset: 3-5 diverse projects with manually scored storyboards
- [ ] **E.2** Define quality metrics: timestamp accuracy, segment coverage, narrative coherence
- [ ] **E.3** Automated eval pipeline: run analysis on golden dataset, score against baselines (Phoenix evals)
- [ ] **E.4** Prompt regression testing: detect when prompt changes degrade output quality
- [ ] **E.5** Provider comparison: run same footage through Gemini vs Claude, compare scores

---

## Detailed Design

### Phase A: Phoenix Integration

#### Current tracing architecture

```
editorial_agent.py ──→ traced_gemini_generate() ──→ traces.jsonl (append-only, always)
                                                  ──→ ProjectTracer (in-memory summary)
                                                  ──→ print_summary()

Claude calls ──→ untraced
```

#### Target architecture

```
tracing.py: _init_phoenix()  ←── only when VX_TRACING=1
    │
    ├── GoogleGenAIInstrumentor()  ←── auto-traces all Gemini calls
    └── AnthropicInstrumentor()    ←── auto-traces all Claude calls
                │
                ▼
editorial_agent.py ──→ traced_gemini_generate() ──→ traces.jsonl (always, unchanged)
                   │                             ──→ ProjectTracer (always, unchanged)
                   │                             ──→ Phoenix (when available)
                   │
                   ──→ traced_claude_generate()  ──→ same three backends
                   │
                   ──→ OTel spans for pipeline   ──→ Phoenix (hierarchical view)
                       stages (preprocess,
                       Phase 1, Phase 2, etc.)

vx debug-traces ──→ px.Client().get_spans() ──→ terminal output (DataFrame)
```

**Key design decisions:**
- `traces.jsonl` + `ProjectTracer` remain **always-on** — they work without Phoenix for end users
- Phoenix auto-instrumentors handle Gemini/Claude tracing transparently — no changes to existing `generate_content()` calls
- `VX_TRACING=1` env var gates Phoenix initialization — off by default
- `vx debug-traces` is a dev CLI command for terminal-based trace inspection
- Future agent spans use OTel `tracer.start_as_current_span()` — nests under Phoenix traces automatically

#### Activation flow

```
User (normal):     vx analyze my-trip          → no Phoenix, traces.jsonl only
Dev (debugging):   VX_TRACING=1 vx analyze ... → Phoenix starts, full tracing
Dev (reviewing):   vx debug-traces my-trip     → fetch traces from Phoenix
Claude Code:       python -c "import phoenix..." → programmatic trace access
```

### Phase B: Retry with Exponential Backoff

```python
RETRYABLE_EXCEPTIONS = (
    "TooManyRequests",       # 429
    "ServiceUnavailable",    # 503
    "InternalServerError",   # 500
    "DeadlineExceeded",      # timeout
)
MAX_RETRIES = 3
BASE_DELAY_SEC = 2.0

def traced_gemini_generate(..., max_retries=MAX_RETRIES):
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(...)
            return response
        except Exception as e:
            is_retryable = any(exc_name in type(e).__name__ for exc_name in RETRYABLE_EXCEPTIONS)
            if attempt < max_retries and is_retryable:
                delay = BASE_DELAY_SEC * (2 ** attempt) + random.uniform(0, 1)
                print(f"  Retryable error (attempt {attempt+1}): {type(e).__name__}")
                time.sleep(delay)
                continue
            raise
```

### Phase C: Response Quality Validation

**Phase 1 review validation:**
```python
def validate_clip_review(review: dict, clip_info: dict) -> tuple[list[str], bool]:
    """Returns (warnings, is_critical). Critical = should retry."""
    warnings = []
    dur = clip_info["duration_sec"]

    for seg in review.get("usable_segments", []):
        if seg.get("in_sec", 0) >= seg.get("out_sec", 0):
            warnings.append(f"Segment in_sec >= out_sec: {seg}")
        if seg.get("out_sec", 0) > dur + 1.0:
            warnings.append(f"out_sec {seg['out_sec']:.1f} > clip duration {dur:.1f}")

    if not review.get("usable_segments") and not review.get("discard_segments"):
        warnings.append("No segments identified at all")

    is_critical = len(warnings) > len(review.get("usable_segments", []))
    return warnings, is_critical
```

**Phase 2 storyboard validation:**
```python
def validate_storyboard(sb: EditorialStoryboard, reviews: list[dict]) -> tuple[list[str], bool]:
    warnings = []
    known = {r["clip_id"] for r in reviews}
    dur_map = {r["clip_id"]: r.get("duration_sec", 0) for r in reviews}

    for seg in sb.segments:
        if seg.clip_id not in known:
            warnings.append(f"Seg {seg.index}: unknown clip '{seg.clip_id}'")
        if seg.in_sec >= seg.out_sec:
            warnings.append(f"Seg {seg.index}: in_sec >= out_sec")
        max_dur = dur_map.get(seg.clip_id, 0)
        if max_dur and seg.out_sec > max_dur + 1.0:
            warnings.append(f"Seg {seg.index}: out_sec {seg.out_sec:.1f} > clip duration {max_dur:.1f}")

    if not sb.segments:
        warnings.append("Empty storyboard — no segments")

    is_critical = not sb.segments or sum(1 for w in warnings if "unknown clip" in w) > 0
    return warnings, is_critical
```

**Auto-retry with feedback:**
```python
if is_critical and attempt < max_validation_retries:
    feedback = "\n".join(f"- {w}" for w in warnings)
    prompt += f"\n\nYour previous response had these issues:\n{feedback}\nPlease fix them."
    # retry LLM call — Phoenix shows this as a new child span under the attempt
```

### Phase D: Cost Guard

```python
class CostLimitExceeded(Exception):
    pass

class ProjectTracer:
    def __init__(self, project_root, max_cost_usd=None):
        self.max_cost_usd = max_cost_usd
        ...

    def record(self, trace):
        self.traces.append(trace)
        # ... existing disk write ...
        if self.max_cost_usd:
            cumulative = sum(t.estimated_cost_usd for t in self.traces)
            if cumulative > self.max_cost_usd:
                raise CostLimitExceeded(
                    f"Cost ${cumulative:.2f} exceeds limit ${self.max_cost_usd:.2f}"
                )
```

CLI: `vx analyze my-trip --max-cost 1.00`

---

## Dependency Order

```
Phase B (Retry)         ──→ no external deps, quickest win
Phase A (Phoenix)       ──→ enables C.5 and E
Phase C (Validation)    ──→ uses B for auto-retry, uses A for trace logging
Phase D (Cost)          ──→ independent, slot in anytime
Phase E (Eval)          ──→ requires A + C
```

Suggested execution order: **B → A → C → D → E**

---

## Future: Agent Self-Correction

When we add agent loops (self-reviewing, timestamp correction), the architecture is already ready:

1. **Tracing** — Phoenix OTel spans nest agent steps automatically
2. **Framework** — No heavy framework needed. Options if we want one:
   - **Instructor** — structured output + Pydantic validation + auto-retry (lightest, matches our existing Pydantic models)
   - **Tenacity** — retry library, already covers 80% of what we need
   - **LangGraph** — if we need complex multi-agent workflows later (overkill for now)
3. **Debugging** — Claude Code can inspect each retry attempt's prompt/response via `px.Client().get_spans()`

---

## Success Criteria

1. **Quality:** Storyboard validation catches >80% of invalid timestamps before reaching the user
2. **Cost:** Actual cost within 20% of estimate; `--max-cost` prevents overspend
3. **Reliability:** Transient API failures auto-recovered without user intervention
4. **Observability:** Every LLM call traced with input/output/cost/quality (Phoenix when enabled, traces.jsonl always)
5. **Dev ergonomics:** Claude Code can diagnose LLM quality issues from terminal in <30 seconds
6. **User transparency:** End users never see or need to install tracing dependencies
