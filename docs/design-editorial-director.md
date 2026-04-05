# Editorial Director: AI-Native Feedback Loop System Design

## Context

VX currently operates in a fire-and-forget model: Phase 2 produces a storyboard, validation prints warnings to the console, but nothing acts on them. The user must manually re-run with adjusted context or edit JSON by hand. This is fundamentally un-AI-native — there's no self-review, no targeted correction, no convergence toward quality.

This plan introduces an **Editorial Director** — a tool-using ReAct agent that reviews, critiques, and iteratively refines storyboard outputs. It uses **multimodal review by default** (Gemini Flash already supports images; cost is negligible at $0.0007 per contact strip). The agent has tools to inspect specific segments (screenshots, transcript excerpts, clip reviews) and applies targeted fixes — not full regeneration.

### Key Design Decisions (User Refinements)

1. **Visual review is default, not opt-in.** Gemini Flash is multimodal and we already use it. A contact strip of 18 thumbnails costs 4,644 tokens = $0.0007 with Flash Lite. Negligible. The director always "sees" the storyboard visually.

2. **Tool-based content access.** Instead of pre-loading all context, the director has tools: `screenshot_segment` (extract thumbnails on demand), `get_transcript_excerpt` (fetch transcript for a time range), `get_clip_review` (fetch Phase 1 review for a specific clip). The agent decides what to inspect. This follows the progressive context disclosure pattern (from OpenHarness/learn-claude-code research).

3. **Full picture first, then targeted.** The director starts with an overview pass (contact strip + storyboard summary + transcription highlights) to understand the whole edit. Then it drills into specific segments that look problematic. This is how a real director reviews — watch the whole thing, then go back to fix specific moments.

4. **Transcription quality review.** When the director sees thumbnails alongside transcript text, it can spot inconsistencies (e.g., transcript says "walking on beach" but thumbnail shows an indoor scene; speaker attribution that contradicts who's visible). This cross-modal verification catches errors that neither text-only nor image-only review would find.

5. **Harness design from OpenHarness/learn-claude-code.** Agent loop with model-controlled termination (LLM decides when done, `max_turns` as safety cap). Dict-based tool dispatch. Budget tracking (turns + cost). Micro-compact old tool results after 3 turns to manage context window.

### Product Framework Evaluation

| Dimension | Score | Rationale |
|-----------|:-----:|-----------|
| User Impact | 5 | Directly improves rough cut quality — the #1 priority for casual sharers. Eliminates manual re-run cycles. |
| Strategic Alignment | 5 | Deepens the "AI editorial intelligence" moat. No competitor has self-reviewing storyboard quality loops with multimodal verification. |
| Dependency Position | 4 | Foundation for future conversational editing and FCPXML round-trip review. |
| Technical Feasibility | 4 | Building blocks exist (eval.py, validation, split pipeline, tracing, thumbnail extraction). Gemini Flash is already multimodal. |

**Score: (5×3) + (5×2) + (4×2) + (4×1) = 37** — Highest possible tier.

**Phase placement**: Phase 0.5 — sits between Phase 0 (Quality Foundation) and Phase 1 (B-Roll Lanes). Requires Phase 0's eval baseline as foundation.

---

## A. System Architecture

### Core Principle: "The Model is the Agent. The Code is the Harness."

The director is a **tool-using LLM agent**, not a fixed pipeline. The harness provides tools, enforces budgets, manages context. The LLM drives all decisions: what to inspect, what to fix, when to stop. This follows the OpenHarness/learn-claude-code pattern.

### Component Diagram

```
            EXISTING PIPELINE (unchanged)
            =============================
Phase 1 (clip reviews) ──> Phase 2 (storyboard generation)
                                      │
                                      ▼
                           ┌──────────────────────┐
                           │  Post-Processing      │  ← existing: clip ID resolution,
                           │  (deterministic)      │    timestamp clamping
                           └──────────┬───────────┘
                                      │
               ┌──────────────────────▼───────────────────────┐
               │        Editorial Director Agent (NEW)         │
               │        editorial_director.py                   │
               │                                               │
               │  ┌─────────────────────────────────────────┐ │
               │  │           Agent Loop (ReAct)             │ │
               │  │                                         │ │
               │  │  1. OVERVIEW: Contact strip + summary   │ │
               │  │     + computable scores + transcripts   │ │
               │  │                                         │ │
               │  │  2. INSPECT: Tool calls as needed       │ │
               │  │     screenshot_segment(idx)             │ │
               │  │     get_transcript_excerpt(clip, range) │ │
               │  │     get_clip_review(clip_id)            │ │
               │  │     run_eval_check(dimension)           │ │
               │  │                                         │ │
               │  │  3. FIX: Targeted segment edits         │ │
               │  │     apply_segment_fix(idx, new_segment) │ │
               │  │     delete_segment(idx)                 │ │
               │  │     reorder_segments(new_order)         │ │
               │  │                                         │ │
               │  │  4. FINALIZE: Agent signals done        │ │
               │  │     finalize_review(verdict)            │ │
               │  └─────────────────────────────────────────┘ │
               │                                               │
               │  Harness: budget (turns + cost), micro-compact│
               │  context, oscillation detect, regression guard│
               └──────────────────────┬───────────────────────┘
                                      │
                                      ▼
                           ┌──────────────────────┐
                           │  Version & Save       │  ← existing: versioning.py
                           │  Render HTML/MD       │    render.py
                           └──────────────────────┘
```

### Two-Phase Review Flow

**Phase A: Overview (always runs)**
The agent receives:
- Storyboard summary (title, style, duration, segment count, story arc outline)
- **Contact strip image** — one midpoint thumbnail per segment, composited horizontally (18 images × 258 tokens = 4,644 tokens ≈ $0.0005)
- Computable eval scores (constraint satisfaction, timestamp precision, coverage — free)
- Transcript highlights per segment (first 2 lines of dialogue, any speaker changes)

This gives the agent a "watch the whole thing" overview. The agent sees the visual flow, narrative structure, and any obvious gaps.

**Phase B: Targeted inspection (agent-driven)**
Based on the overview, the agent uses tools to drill into specific problems:
- `screenshot_segment(5)` → 2x2 thumbnail grid of segment 5 (4 frames, 1,032 tokens)
- `get_transcript_excerpt("C0073", 10.0, 25.0)` → full transcript for that time range
- `get_clip_review("C0073")` → Phase 1 review data (usable segments, people, quality)
- `run_eval_check("constraint_satisfaction")` → detailed constraint-by-constraint results

Then applies fixes:
- `apply_segment_fix(5, {...})` → replace segment 5 with corrected version
- `finalize_review({verdict})` → signal done, return final storyboard

### Where It Fits

Inside `_run_phase2_split()` in `editorial_agent.py`, after line ~1208 (post-processing), before versioned save:

```python
# Line ~1208: existing post-processing
_resolve_clip_id_refs(storyboard, known_clip_ids)
_clamp_timestamps(storyboard, reviews_by_id)

# NEW: Editorial Director review (enabled by default)
if review_config and review_config.enabled:
    from .editorial_director import run_editorial_review
    storyboard = run_editorial_review(
        storyboard=storyboard,
        clip_reviews=clip_reviews,
        user_context=user_context,
        clips_dir=clips_dir,
        review_config=review_config,
        tracer=tracer,
        interactive=interactive,
    )

# Existing: version and save
```

### New Files

| File | Purpose |
|------|---------|
| `src/ai_video_editor/editorial_director.py` | Agent harness: `run_editorial_review()` — tool dispatch loop, budget tracking, context management |
| `src/ai_video_editor/director_tools.py` | Tool implementations: screenshot, transcript, clip review, eval check, segment fix, finalize |
| `src/ai_video_editor/director_prompts.py` | System prompt + tool descriptions for the director agent |

### Modified Files

| File | Changes |
|------|---------|
| `models.py` | Add `ReviewConfig`, `ReviewVerdict`, `SegmentIssue`, `ReviewLog` dataclasses |
| `eval.py` | Add `structural_completeness_score()`, `coverage_score()`, `speech_cut_check()` |
| `editorial_agent.py` | Wire `run_editorial_review()` into `_run_phase2_split()` after line ~1208 |
| `cli.py` | Add `--review` / `--no-review`, `--review-budget`, `--review-max-turns` flags |
| `interactive.py` | Add review progress display + human checkpoint prompts |
| `render.py` | Add `generate_contact_strip()`, `generate_segment_grid()` for thumbnail compositing |
| `tracing.py` | Track director agent turns and tool calls in `traces.jsonl` |

---

## B. Review Rubric Design

### Dimensions

The director evaluates across 7 dimensions — 4 computable (free) and 3 agent-judged (multimodal LLM):

| Dimension | Type | How Assessed | Scale | Pass Threshold |
|-----------|------|-------------|-------|----------------|
| `constraint_satisfaction` | Computable | `eval.py` — must-include/exclude check | 0.0–1.0 | **1.0** (hard gate) |
| `timestamp_precision` | Computable | `eval.py` — segments within usable bounds | 0.0–1.0 | ≥ 0.85 |
| `structural_completeness` | Computable | `eval.py` — reasoning, arc, cast, discarded present | 0.0–1.0 | ≥ 0.7 |
| `speech_cut_safety` | Computable | `eval.py` NEW — segment boundaries don't cut mid-word/sentence (cross-ref with transcripts) | 0.0–1.0 | ≥ 0.9 |
| `narrative_flow` | Agent-judged | Overview pass — pacing, arc coherence, visual progression (sees contact strip) | 0 / 1 / 2 | ≥ 1 |
| `segment_coherence` | Agent-judged | Per-segment — audio_note vs transition consistency, purpose vs content match | 0 / 1 / 2 | all ≥ 1 |
| `transcription_coherence` | Agent-judged | Cross-modal — transcript text vs visual content (e.g., "walking on beach" when thumbnail shows indoors; wrong speaker attribution) | 0 / 1 / 2 | ≥ 1 |

**Coarse 0/1/2 scale** for agent-judged dimensions:
- 0 = broken, contradictory, or missing
- 1 = acceptable, functional
- 2 = excellent, well-crafted

### `speech_cut_safety` — New Computable Dimension

This is a critical quality check: **don't cut someone mid-sentence at a confusing point.** Implementation:

```python
def score_speech_cut_safety(storyboard, transcripts_by_clip) -> float:
    """Check that segment boundaries don't fall mid-word/mid-sentence."""
    safe_count = 0
    for seg in storyboard.segments:
        transcript = transcripts_by_clip.get(seg.clip_id)
        if not transcript:
            safe_count += 1  # no transcript = can't check, assume safe
            continue
        # Check out_sec: is there active speech at the cut point?
        speech_at_cut = find_speech_at_time(transcript, seg.out_sec, tolerance=0.3)
        if speech_at_cut is None:
            safe_count += 1  # no speech at cut point = safe
        elif speech_at_cut.is_sentence_end:
            safe_count += 1  # cut at sentence boundary = safe
        # else: mid-sentence cut = unsafe
    return safe_count / len(storyboard.segments) if storyboard.segments else 1.0
```

### `transcription_coherence` — Cross-Modal Agent Check

This is the key insight from the user: when the director sees thumbnails alongside transcript text, it can spot:
- **Misattributed speakers**: Transcript says "Speaker A" but thumbnail shows Speaker B is the one talking
- **Content mismatches**: Transcript says "walking on beach" but thumbnail shows indoor scene
- **Hallucinated dialogue**: Transcript has detailed dialogue but thumbnail shows a distant shot where no one could be heard clearly
- **Timing drift**: Transcript timestamps don't align with visual changes

The agent uses `screenshot_segment(N)` + `get_transcript_excerpt(clip, in, out)` together to verify coherence. This is where multimodal review earns its keep.

### Pass/Fail Logic

```python
def compute_verdict(scores: dict, issues: list[SegmentIssue]) -> bool:
    # Hard gates
    if scores["constraint_satisfaction"] < 1.0:
        return False
    if any(i.severity == "critical" for i in issues):
        return False
    # Soft gate: weighted average
    weights = {
        "timestamp_precision": 0.20,
        "structural_completeness": 0.10,
        "speech_cut_safety": 0.15,
        "narrative_flow": 0.20,
        "segment_coherence": 0.15,
        "transcription_coherence": 0.20,
    }
    weighted = sum(scores.get(k, 0) * w for k, w in weights.items())
    return weighted >= 0.70
```

---

## C. Director Tools

The agent has 8 tools organized into three categories: **inspect**, **fix**, and **control**. Tool dispatch uses a simple dict-based pattern (per learn-claude-code s02).

### Inspect Tools (read-only, no budget cost for fixes)

```python
TOOL_HANDLERS = {
    # ── INSPECT ──
    "screenshot_segment": screenshot_segment,
    "get_transcript_excerpt": get_transcript_excerpt,
    "get_clip_review": get_clip_review,
    "run_eval_check": run_eval_check,

    # ── FIX ──
    "apply_segment_fix": apply_segment_fix,
    "delete_segment": delete_segment,
    "reorder_segments": reorder_segments,

    # ── CONTROL ──
    "finalize_review": finalize_review,
}
```

**`screenshot_segment(segment_index: int) -> Image`**
- Extracts a 2x2 thumbnail grid (4 keyframes) from the segment's source clip at `in_sec`, `in_sec + duration/3`, `in_sec + 2*duration/3`, `out_sec - 0.1`
- Returns as inline image (JPEG, 360px frames, ~40KB)
- Reuses `_extract_thumbnail()` from `render.py:108-128`
- Cost: 4 × 258 tokens = **1,032 tokens** ($0.0001 with Flash Lite)
- **Cached per segment** — if the agent inspects the same segment twice, returns cached image

**`get_transcript_excerpt(clip_id: str, start_sec: float, end_sec: float) -> str`**
- Returns transcript lines within the time range, with speaker labels and timestamps
- Reuses existing transcript loading from `transcribe.py`
- Cost: ~200-500 text tokens (negligible)

**`get_clip_review(clip_id: str) -> str`**
- Returns Phase 1 review data: usable segments, people detected, quality notes, visual summary
- Compact format (~300 tokens per clip)

**`run_eval_check(dimension: str) -> str`**
- Runs a specific computable eval dimension and returns detailed results
- E.g., `run_eval_check("constraint_satisfaction")` → per-constraint pass/fail with evidence
- E.g., `run_eval_check("speech_cut_safety")` → per-segment cut safety with transcript context
- Cost: $0 (computable, no LLM)

### Fix Tools (cost budget for mutations)

**`apply_segment_fix(segment_index: int, updated_fields: dict) -> str`**
- Applies a partial update to a specific segment. Agent specifies only the fields to change.
- E.g., `apply_segment_fix(5, {"out_sec": 23.5, "audio_note": "preserve_dialogue"})`
- Validates: clip_id exists, in_sec < out_sec, timestamps within clip duration
- Returns confirmation + any cascading warnings (e.g., "segment 6 transition may need update")
- **Each fix call costs 1 mutation budget unit**

**`delete_segment(segment_index: int) -> str`**
- Removes a segment, renumbers remaining indices
- Returns confirmation + warning if this creates a gap in the story arc

**`reorder_segments(new_order: list[int]) -> str`**
- Reorders segments by providing new index sequence
- Validates: no duplicates, no missing indices
- Returns confirmation

### Control Tools

**`finalize_review(passed: bool, summary: str) -> str`**
- Agent signals it's done reviewing. `passed` indicates if the storyboard meets quality bar.
- `summary` is the agent's editorial assessment (stored in review log)
- **This is the only way the loop ends normally** (besides budget/timeout)

### Cascading Effect Handling

After any fix tool call, the harness runs a **cascade check**:
1. Verify modified segment's neighbors have valid transitions
2. Re-run `speech_cut_safety` for affected segments
3. If cascading issues found, return them as warnings in the tool result — the agent sees them immediately and can decide whether to fix in the same turn or note for later

---

## D. Visual Review Strategy (Default, Not Opt-In)

### Overview Pass — Contact Strip (Always)

The initial prompt includes a **contact strip** — one midpoint frame per segment, composited into a single image:
- 18 segments × 258 tokens = 4,644 tokens
- Cost: **$0.0005** with Flash Lite, **$0.0014** with Flash
- The agent sees the visual progression of the entire edit at a glance

### On-Demand Deep Inspection — Segment Grids

When the agent spots something in the contact strip (e.g., "segment 8 looks like a duplicate of segment 3"), it uses `screenshot_segment(8)` to get a detailed 2x2 grid:
- 4 frames × 258 tokens = 1,032 tokens
- Cost: **$0.0001** with Flash Lite

### Cross-Modal Transcript Verification

The agent's most powerful review mode combines visual + transcript:
1. `screenshot_segment(5)` → sees who's in frame
2. `get_transcript_excerpt("C0073", 10.0, 25.0)` → reads what was said
3. Agent compares: "Transcript says Speaker A but the person in frame is clearly the woman in the green jacket who was identified as Amy in the briefing"

This catches errors that pure text review or pure image review would miss.

### Escalation Path

If thumbnails are insufficient for a specific issue (e.g., motion quality, camera shake):
1. Agent notes the issue with `finalize_review(passed=False, summary="Cannot assess motion quality from stills for segments 4, 7")
2. The review log captures this as an "unresolved_visual" issue
3. In interactive mode, the user is prompted: "Director couldn't assess motion for segments 4, 7. Preview these in browser? (y/n)"
4. **Full proxy video is never uploaded during review** — keeps costs predictable

### Cost of Visual Review (Always-On)

| Component | Tokens | Cost (Flash Lite) | Cost (Flash) |
|-----------|:------:|:-----------------:|:------------:|
| Contact strip (overview) | 4,644 | $0.0005 | $0.0014 |
| 2 segment deep-inspections | 2,064 | $0.0002 | $0.0006 |
| Text context + response | ~3,000 | $0.0004 | $0.0010 |
| **Total per turn** | ~9,700 | **$0.0011** | **$0.0030** |

For a 3-turn review (overview + 2 inspection turns): **$0.003–0.009**. This is negligible — visual review should always be on.

---

## E. Harness & Guardrails

### ReviewConfig

```python
@dataclass
class ReviewConfig:
    enabled: bool = True
    max_turns: int = 15              # max LLM turns (inspect + fix combined)
    max_fixes: int = 10              # max mutation tool calls
    max_review_cost_usd: float = 0.50
    wall_clock_timeout_sec: float = 180.0   # 3 minutes
    human_checkpoint_on_uncertainty: bool = True
```

### Budget Tracking (per OpenHarness CostTracker pattern)

```python
@dataclass
class ReviewBudget:
    max_turns: int = 15
    max_fixes: int = 10
    max_cost_usd: float = 0.50
    turns_used: int = 0
    fixes_used: int = 0
    cost_used_usd: float = 0.0

    def can_continue(self) -> bool:
        return (self.turns_used < self.max_turns
                and self.fixes_used < self.max_fixes
                and self.cost_used_usd < self.max_cost_usd)

    def remaining_summary(self) -> str:
        """Injected into system prompt so agent sees its budget."""
        return (f"Budget: {self.max_turns - self.turns_used} turns, "
                f"{self.max_fixes - self.fixes_used} fixes, "
                f"${self.max_cost_usd - self.cost_used_usd:.3f} remaining")
```

The agent sees its remaining budget in the system prompt. This is critical — the agent self-regulates because it knows when resources are running low.

### Termination: The Loop Stops When

1. **Agent calls `finalize_review()`** — model-controlled termination (primary mechanism)
2. **Budget exhausted** — `max_turns`, `max_fixes`, or `max_cost_usd` hit
3. **Wall clock timeout** — `wall_clock_timeout_sec` exceeded
4. **No tool calls** — agent responds with text only (no tool use = implicit done)

Unlike the previous plan, there is no programmatic convergence detection or oscillation detection — the agent manages this itself because it sees the full context including previous fixes and their outcomes.

### Regression Protection

After each `apply_segment_fix` call, the harness:
1. Snapshots the storyboard before the fix
2. Runs computable eval scores (free)
3. If scores regressed, **reverts the fix** and returns an error to the agent: "Fix reverted: timestamp_precision dropped from 0.93 to 0.80. Try a different approach."
4. The agent sees this and can decide to try differently or accept the current state

This is implemented in the tool handler, not in the loop — the agent gets immediate feedback on whether its fix helped.

### Context Window Management (per learn-claude-code s06)

**Micro-compact after 3 turns**: Tool results older than 3 turns are replaced with `[result cleared — use tools to re-fetch if needed]`. This prevents the context window from filling with stale screenshot descriptions and transcript excerpts.

The agent can always re-fetch any data via tools, so clearing old results is safe.

### Human Checkpoint Triggers (interactive mode only)

1. Agent calls `finalize_review(passed=False)` — storyboard didn't pass but agent is out of ideas
2. Constraint violation persists after 2 fix attempts
3. Cost approaching budget (>80%)
4. Agent explicitly requests human input (via a note in finalize summary)

In non-interactive mode: accept current storyboard, log all unresolved issues.

---

## F. The Director Agent Loop

```python
def run_editorial_review(
    storyboard: EditorialStoryboard,
    clip_reviews: list[dict],
    user_context: dict | None,
    clips_dir: Path,
    review_config: ReviewConfig,
    tracer: ProjectTracer | None = None,
    interactive: bool = False,
) -> EditorialStoryboard:
    """Tool-using agent loop for editorial review."""

    budget = ReviewBudget(
        max_turns=review_config.max_turns,
        max_fixes=review_config.max_fixes,
        max_cost_usd=review_config.max_review_cost_usd,
    )

    # Pre-compute what we can (free)
    eval_report = score_storyboard(storyboard, clip_reviews, user_context)
    contact_strip = generate_contact_strip(storyboard, clips_dir)

    # Build initial prompt: overview + contact strip + computable scores
    system_prompt = build_director_system_prompt(review_config)
    initial_message = build_director_initial_message(
        storyboard=storyboard,
        eval_report=eval_report,
        contact_strip_image=contact_strip,
        user_context=user_context,
        budget=budget,
    )

    # Tool context (closures over mutable storyboard + project data)
    tool_ctx = DirectorToolContext(
        storyboard=storyboard,
        clip_reviews=clip_reviews,
        clips_dir=clips_dir,
        transcripts=load_transcripts(clips_dir),
        budget=budget,
    )

    messages = [{"role": "user", "content": initial_message}]
    start_time = time.monotonic()

    while budget.can_continue():
        if time.monotonic() - start_time > review_config.wall_clock_timeout_sec:
            log.warning("Review timeout")
            break

        # Call LLM with tools
        response = traced_gemini_generate(
            model=review_config.model or "gemini-2.5-flash",
            contents=messages,
            tools=DIRECTOR_TOOL_DECLARATIONS,
            tracer=tracer,
            phase="editorial_review",
        )
        budget.turns_used += 1
        budget.cost_used_usd += response.cost_usd

        messages.append(assistant_message(response))

        # No tool calls = agent is done
        if not response.tool_calls:
            break

        # Execute tool calls
        tool_results = []
        for call in response.tool_calls:
            handler = TOOL_HANDLERS.get(call.name)
            if not handler:
                tool_results.append(error_result(call, f"Unknown tool: {call.name}"))
                continue

            result = handler(tool_ctx, **call.args)
            tool_results.append(result)

            # Track fix budget
            if call.name in ("apply_segment_fix", "delete_segment", "reorder_segments"):
                budget.fixes_used += 1

            # Check for finalize
            if call.name == "finalize_review":
                # Agent explicitly ended review
                log.info(f"Review finalized: passed={call.args.get('passed')}")
                messages.append(tool_results_message(tool_results))
                # Return potentially modified storyboard
                return tool_ctx.storyboard

        messages.append(tool_results_message(tool_results))

        # Micro-compact: clear old tool results (keep last 3 turns)
        micro_compact(messages, keep_recent=3)

    # Budget/timeout exit — return best storyboard
    return tool_ctx.storyboard
```

---

## G. Cost Model (Multimodal by Default)

### Per-Turn Cost Breakdown (15-clip project, Gemini 2.5 Flash)

| Component | Tokens | Cost |
|-----------|:------:|:----:|
| Contact strip (18 thumbnails, overview turn only) | 4,644 | $0.0014 |
| Storyboard text + eval scores | ~2,000 | $0.0006 |
| Agent response + reasoning | ~1,500 | $0.0038 |
| 1 screenshot_segment tool call | 1,032 | $0.0003 |
| 1 get_transcript_excerpt call | ~400 | $0.0001 |
| **Typical turn (overview)** | **~8,100** | **$0.006** |
| **Typical turn (inspection)** | **~4,500** | **$0.004** |
| **Typical turn (fix)** | **~2,000** | **$0.002** |

### Typical Review Cycles

| Scenario | Turns | Cost | % of Pipeline |
|----------|:-----:|:----:|:-------------:|
| Clean pass (overview → finalize) | 2 | $0.008 | ~2% |
| Minor fixes (overview → 2 inspections → 2 fixes → finalize) | 6 | $0.022 | ~5.5% |
| Moderate fixes (overview → 4 inspections → 3 fixes → re-check → finalize) | 10 | $0.038 | ~9.5% |
| Max budget (15 turns) | 15 | ~$0.060 | ~15% |

### Compared to Alternatives

| Approach | Cost per Project | Notes |
|----------|:----------------:|-------|
| Current (no review) | $0.00 | Issues caught by human |
| Director review (typical) | $0.02–0.04 | Catches issues automatically |
| Manual re-run (full Phase 2) | $0.10–0.20 | Full storyboard regeneration |
| Manual re-run + Phase 1 | $0.30–0.50 | If user changes context |

**Bottom line**: The director adds **$0.02–0.06 per project** (5-15% of pipeline cost) while potentially eliminating $0.10–0.50 of manual re-runs. The multimodal cost (images) is <10% of the review cost — always having visual review is justified.

---

## H. Director System Prompt Design

The system prompt is the soul of the agent. It must make the director an opinionated editorial reviewer, not a generic assistant.

### System Prompt Outline

```
You are an editorial director reviewing a video storyboard for VX.
Your job: watch the edit, spot problems, fix them. You are opinionated
about quality — a rough cut that's "good enough to share" is the bar.

## Your Review Process
1. OVERVIEW: Study the contact strip and eval scores. Get the big picture.
2. INSPECT: Use screenshot_segment and get_transcript_excerpt to drill
   into anything suspicious. Look for:
   - Cuts that interrupt speech mid-sentence
   - Segments where transcript content doesn't match what's visible
   - Pacing problems (too many slow segments in a row, or too jumpy)
   - Missing must-include moments (check constraint scores)
   - Audio/transition contradictions (j_cut with muted audio, etc.)
   - Weak opening hook or abrupt ending
   - Duplicate visual content across segments
3. FIX: Use apply_segment_fix to correct specific problems. Only fix
   what you're confident about. Small precise fixes > sweeping changes.
4. FINALIZE: When the edit is good enough to share, call finalize_review.

## Quality Bar
- Constraints: 100% must be satisfied (hard requirement)
- No mid-sentence cuts (check transcript at cut points)
- Visual flow: contact strip should tell a coherent story
- Pacing: variety of segment durations, not all the same length
- Transitions: audio_note must be compatible with transition type

## Budget
{budget.remaining_summary()}
Be efficient. Don't inspect every segment — focus on problems.
```

### Initial Message Content

The first user message includes:
1. **Storyboard summary**: title, style, duration, segment count, story arc section names
2. **Contact strip image**: midpoint thumbnail per segment (multimodal)
3. **Computable eval scores**: constraint satisfaction, timestamp precision, speech cut safety, structural completeness, coverage — all as a compact table
4. **Transcript highlights per segment**: first 2 dialogue lines + speaker name (compact, ~30 tokens/segment)
5. **User constraints**: must-include / must-exclude items from user_context

This gives the agent the "full picture" to start. It can then use tools to zoom in.

---

## I. CLI/TUI Interface

### CLI Flags

```bash
vx analyze my-trip                      # review enabled by default
vx analyze my-trip --no-review          # skip review loop
vx analyze my-trip --review-budget 0.10 # custom budget ($)
vx analyze my-trip --review-max-turns 8 # custom turn limit
```

No `--visual-review` flag needed — visual review is always on (cost is negligible).

### TUI Progress Display

```
  [Director] Reviewing storyboard (18 segments, 3m24s)...
  [Director] Turn 1: Overview — checking eval scores + visual flow
    Constraints: 3/3 ✓ | Timestamps: 14/15 (93%) | Speech-safe: 17/18
  [Director] Turn 2: Inspecting segment 5 (mid-sentence cut detected)
  [Director] Turn 3: Inspecting segment 12 (transcript mismatch)
  [Director] Turn 4: Fixing segment 5 — adjusted out_sec to sentence end
  [Director] Turn 5: Fixing segment 12 — corrected speaker attribution
  [Director] Turn 6: Finalized — PASSED (6 turns, $0.024, 12.4s)
```

---

## J. Implementation Sequence

| Step | What | New/Modified Files | LLM? | Testable Alone? |
|------|------|--------------------|:----:|:---------------:|
| 1 | Data models: `ReviewConfig`, `ReviewBudget`, `ReviewLog`, `SegmentIssue` | `models.py` | No | Yes |
| 2 | Eval extensions: `speech_cut_safety()`, `structural_completeness_score()`, `coverage_score()` | `eval.py` | No | Yes |
| 3 | Thumbnail compositing: `generate_contact_strip()`, `generate_segment_grid()` | `render.py` | No | Yes (ffmpeg) |
| 4 | Tool implementations: all 8 tools in `director_tools.py` | `director_tools.py` (new) | No | Yes (unit tests) |
| 5 | Director prompts: system prompt + tool declarations | `director_prompts.py` (new) | No | Yes (template) |
| 6 | Agent harness: `run_editorial_review()` with loop, budget, micro-compact | `editorial_director.py` (new) | Yes | Yes (integration) |
| 7 | Integration: wire into `_run_phase2_split()`, add CLI args | `editorial_agent.py`, `cli.py` | No | — |
| 8 | TUI: progress display, human checkpoints | `interactive.py` | No | — |
| 9 | Tracing: log director turns + tool calls | `tracing.py` | No | — |

Steps 1-5 are testable without LLM calls (pure data models, ffmpeg, and templates). Step 6 is the first LLM-dependent step. Steps 7-9 are integration.

---

## K. Verification

1. **Eval extensions**: Run `speech_cut_safety()` on existing storyboards with transcripts — verify it catches known mid-sentence cuts
2. **Thumbnail compositing**: Generate contact strips for all library projects — verify images are valid and readable
3. **Tool unit tests**: Test each tool handler with mock storyboard data — verify correct behavior for valid/invalid inputs
4. **Regression guard**: Feed a fix that makes timestamp_precision worse — verify the tool reverts and returns error
5. **Budget enforcement**: Run agent with `max_turns=3` — verify it stops at 3 regardless of issue count
6. **End-to-end**: Run full review on library projects — verify review completes, cost is within $0.02-0.06, issues detected match known problems
7. **Cross-modal catch**: Inject a storyboard with mismatched transcript (transcript says "beach" but clip is indoors) — verify the director spots it
8. **No-issues fast path**: Run on a clean storyboard — verify 2-turn completion (overview → finalize)
9. Run `ruff check src/` and `ruff format src/` after all changes
