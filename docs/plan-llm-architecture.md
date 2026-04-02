# Plan: LLM Invocation Architecture (Quality + Cost)

**Status:** Planned (execute after codebase hardening batch 1-5)
**Priority:** High — quality and cost are the primary bottlenecks
**Created:** 2026-04-03
**Depends on:** `plan-codebase-hardening.md` (Batches 1-5)

## Context

The current LLM integration works but has structural issues that make result quality hard to improve and costs hard to control:

1. **No observability** — `traces.jsonl` records tokens/cost/timing per call, but there's no way to evaluate output quality, compare across runs, or debug why a particular storyboard was poor. The tracing is hand-rolled and limited.
2. **No retry for transient failures** — a single 429 or network timeout kills the call. The only retry mechanism is the interactive `_retry_failed_phase1()` which requires manual confirmation.
3. **No response validation** — LLM outputs are parsed and immediately accepted. Bad timestamps, hallucinated clip IDs, empty segments all pass through.
4. **No cost guardrails** — no spending limit, no budget alerts, estimates disconnected from actuals.
5. **Client constructed per-call** — `genai.Client()` instantiated 6+ times across the codebase.

## Key Decision: Adopt LLM Observability Platform

The hand-rolled `tracing.py` should be replaced or supplemented with a proper LLM observability platform. This gives us structured tracing, evaluation, cost tracking, and debugging out of the box.

### Options to Evaluate

| Platform | Pricing | Key Features | Integration |
|----------|---------|-------------|-------------|
| **LangSmith** | Free tier (5K traces/mo), $39/seat/mo | Tracing, eval datasets, prompt playground, annotation queues | LangChain SDK or direct API |
| **Langfuse** | Self-hosted (free) or cloud ($0 open-source tier) | Tracing, scoring, prompt management, cost tracking | OpenTelemetry-compatible, Python SDK |
| **Braintrust** | Free tier (1K logs/mo) | Evals, scoring, prompt playground, dataset management | Python SDK, simple decorator-based |
| **Phoenix (Arize)** | Open-source, self-hosted free | Tracing, eval, LLM-as-judge | OpenTelemetry-based |
| **Weights & Biases Weave** | Free tier available | Tracing, eval, versioning | Python SDK |

### Evaluation Criteria

1. **Free/open-source option** — self-hosted or generous free tier (this is a personal project)
2. **Gemini + Claude support** — must trace both providers
3. **Structured output evaluation** — can score JSON responses against schemas
4. **Minimal integration overhead** — decorator or context manager, not framework lock-in
5. **Cost tracking built-in** — per-model cost aggregation
6. **Eval datasets** — ability to build golden datasets for regression testing
7. **Prompt versioning** — track prompt changes and their quality impact

### Recommendation

**Langfuse** is the strongest fit: open-source, self-hosted option (Docker Compose), generous free cloud tier, OpenTelemetry-compatible, works with any LLM provider, has cost tracking and evaluation built in. No framework lock-in.

**LangSmith** is the most mature but requires LangChain ecosystem buy-in for full benefit.

**Action:** Evaluate Langfuse and LangSmith side-by-side with a small proof-of-concept (trace one Phase 1 call through each) before committing.

---

## Task List

### Phase A: Observability Platform Integration

- [ ] **A.1** Evaluate Langfuse vs LangSmith — trace one Phase 1 + one Phase 2 call through each, compare DX
- [ ] **A.2** Integrate chosen platform into `traced_gemini_generate()` as primary tracing backend
- [ ] **A.3** Add Claude call tracing (currently untraced — `client.messages.create()` calls in `editorial_agent.py` lines 615, 769, 921)
- [ ] **A.4** Migrate cost tracking from hand-rolled `COST_PER_1M_TOKENS` to platform's built-in cost
- [ ] **A.5** Keep `traces.jsonl` as local fallback (offline mode) but make platform the primary

### Phase B: Retry & Resilience

- [ ] **B.1** Add automatic retry with exponential backoff to `traced_gemini_generate()`
- [ ] **B.2** Distinguish retryable (429, 500, 502, 503, network timeout) from permanent errors
- [ ] **B.3** Add retry wrapper for Claude `client.messages.create()` calls
- [ ] **B.4** Shared LLM client factory — replace 6+ `genai.Client()` instantiations with `_get_gemini_client()`
- [ ] **B.5** Rate limiting — respect Gemini's QPM limits in parallel Phase 1 workers

### Phase C: Response Quality Validation

- [ ] **C.1** Phase 1 validation: timestamps within clip duration, in_sec < out_sec, non-empty segments
- [ ] **C.2** Phase 2 validation: clip IDs exist, segments have valid timestamps, total duration sanity
- [ ] **C.3** Transcription validation: timestamps within clip duration, non-empty for clips with known speech
- [ ] **C.4** Auto-retry on validation failure — inject validation feedback into retry prompt
- [ ] **C.5** Log validation results to observability platform as quality scores

### Phase D: Cost Management

- [ ] **D.1** `--max-cost` flag for `vx analyze` — abort if cumulative cost exceeds threshold
- [ ] **D.2** Running cost display after each phase completion
- [ ] **D.3** Estimate vs actual comparison printed after pipeline completion
- [ ] **D.4** Per-project cost history accessible via `vx status`

### Phase E: Evaluation & Regression Testing

- [ ] **E.1** Build golden dataset: 3-5 diverse projects with manually scored storyboards
- [ ] **E.2** Define quality metrics: timestamp accuracy, segment coverage, narrative coherence
- [ ] **E.3** Automated eval pipeline: run analysis on golden dataset, score against baselines
- [ ] **E.4** Prompt regression testing: detect when prompt changes degrade output quality
- [ ] **E.5** Provider comparison: run same footage through Gemini vs Claude, compare scores

---

## Detailed Design

### Phase A: Observability Platform Integration

#### Current tracing architecture

```
editorial_agent.py ──→ traced_gemini_generate() ──→ traces.jsonl (append-only)
                                                  ──→ ProjectTracer (in-memory)
                                                  ──→ print_summary()
```

#### Target architecture

```
editorial_agent.py ──→ traced_gemini_generate() ──→ Langfuse/LangSmith (primary)
                                                  ──→ traces.jsonl (offline fallback)
                                                  ──→ ProjectTracer (in-memory summary)

Claude calls ──→ traced_claude_generate() ──→ same backends
```

**Key changes to `tracing.py`:**
- Add `traced_claude_generate()` — currently Claude calls are completely untraced
- Both wrappers send traces to observability platform when available
- Fallback to `traces.jsonl` when platform unavailable (offline mode)
- Cost tracking delegated to platform (more accurate, auto-updated pricing)
- Quality scores attached to traces after validation (Phase C)

**Integration approach (Langfuse example):**
```python
from langfuse import Langfuse

_langfuse = None

def _get_langfuse() -> Langfuse | None:
    global _langfuse
    if _langfuse is None:
        try:
            _langfuse = Langfuse()  # reads LANGFUSE_* env vars
        except Exception:
            return None
    return _langfuse

def traced_gemini_generate(...):
    lf = _get_langfuse()
    generation = lf.generation(name=phase, model=model, ...) if lf else None
    try:
        response = client.models.generate_content(...)
        if generation:
            generation.end(output=response.text, usage={...})
        # ... existing local trace recording ...
        return response
    except Exception as e:
        if generation:
            generation.end(status_message=str(e), level="ERROR")
        raise
```

### Phase B: Retry with Exponential Backoff

```python
RETRYABLE_EXCEPTIONS = (
    # google.api_core.exceptions
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
When validation finds critical issues, retry with feedback injected:
```python
if is_critical and attempt < max_validation_retries:
    feedback = "\n".join(f"- {w}" for w in warnings)
    prompt += f"\n\nYour previous response had these issues:\n{feedback}\nPlease fix them."
    # retry LLM call
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
Phase A (Observability) ──→ Phase C.5 (quality scores to platform)
                         ──→ Phase E (eval datasets in platform)

Phase B (Retry)         ──→ Phase C.4 (auto-retry on validation failure)

Phase C (Validation)    ──→ independent core, but enhanced by A and B

Phase D (Cost)          ──→ independent

Phase E (Eval)          ──→ requires A + C
```

Suggested execution order: **B → A → C → D → E**
- B (retry) is quickest win, no external deps
- A (observability) enables everything else
- C (validation) is the biggest quality lever
- D (cost) is independent, slot in anytime
- E (eval) requires A+C foundation

---

## Success Criteria

1. **Quality:** Storyboard validation catches >80% of invalid timestamps before reaching the user
2. **Cost:** Actual cost within 20% of estimate; `--max-cost` prevents overspend
3. **Reliability:** Transient API failures auto-recovered without user intervention
4. **Observability:** Every LLM call traced with input/output/cost/quality in platform dashboard
5. **Eval:** Can compare quality across prompt versions and providers on golden dataset
