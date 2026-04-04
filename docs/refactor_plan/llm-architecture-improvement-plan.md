# VX LLM Architecture Improvement Plan

## Context

VX's Phase 2 (editorial storyboard) produces results that frequently ignore filmmaker constraints — must-include moments are missing, must-exclude footage appears, tone preferences are overridden. Root cause: 200-400 tokens of soft user context drowning in 5,000-25,000 tokens of clip data, compounded by simultaneous creative reasoning + JSON generation in a single monolithic LLM call.

Two existing refactor plans (`docs/refactor_plan/`) propose splitting Phase 2 into a two-call pipeline (planning → assembly) and Phase 3 into a three-call pipeline. This plan incorporates those architectural changes with prompt engineering techniques from `docs/research-llm-orchestration-and-local-models.md` into a phased, testable implementation.

### Framework Decision: Stay Framework-Free

**No LangChain, LlamaIndex, or Instructor.** Rationale:

- VX already has clean abstractions: `traced_gemini_generate()` handles retry + tracing + cost tracking. Gemini's native `response_schema` handles structured output. Pydantic handles validation.
- Phoenix + OpenTelemetry (Phase A complete) provides observability without framework lock-in.
- The multi-call architecture is an orchestration pattern, not a framework choice — `run_phase2()` already chains Phase 1 → Phase 2 → Phase 3. Splitting Phase 2 into 2A/2B is the same pattern at a finer granularity.
- LangChain's value (chains, retry, structured output) is already covered by existing code. Adding it would require migrating working patterns for marginal benefit while introducing dependency churn.
- OpenTelemetry spans nest naturally for hierarchical tracing (Phase 2 parent span → 2A child → 2B child). No framework needed.

**One exception to evaluate:** `instructor` library (lightweight, ~500 lines) for Claude structured output. Currently Claude responses require manual JSON extraction with markdown fence stripping. Instructor wraps Anthropic SDK to add `response_model=PydanticModel` — same pattern as Gemini's `response_schema`. Low risk, minimal dependency. Evaluate in Phase 2 of this plan.

---

## Phase 1: Prompt Engineering Quick Wins

**Goal:** Improve instruction-following without architectural changes. Testable immediately against existing projects.

**Duration:** ~3-4 hours

### 1.1 Constraint Hierarchy in User Context

**File:** `src/ai_video_editor/briefing.py` — `format_context_for_prompt()` (line 519)

Current output:
```
The filmmaker provided the following context:
- **Must-include moments**: The sunset at the temple
- **Things to avoid**: Don't use shaky bus footage
- **Desired tone**: Calm, contemplative
Use this context to make better editorial decisions.
Prioritize the filmmaker's stated preferences.
```

New output — split into hard constraints and soft preferences:
```
FILMMAKER CONSTRAINTS (non-negotiable — violating these makes the edit unusable):
1. MUST INCLUDE: The sunset at the temple — at least one segment must feature this.
2. MUST EXCLUDE: Don't use shaky bus footage.
3. If you cannot satisfy a constraint, you MUST explain why in editorial_reasoning.

FILMMAKER PREFERENCES (guide your creative choices):
- Desired tone: Calm, contemplative
- Duration preference: [if provided]
- Additional context:
  - Q: [question] → [answer]
```

Implementation: Modify `format_context_for_prompt()` to categorize keys — `highlights` and `avoid` become CONSTRAINTS; `tone`, `duration`, `activity`, `people`, `context_qa` become PREFERENCES. Add accountability clause.

### 1.2 Structured Reasoning Checkpoint

**File:** `src/ai_video_editor/models.py` — `EditorialStoryboard.editorial_reasoning` field

Current:
```python
editorial_reasoning: str = Field(
    description="Your editorial thinking — story arc, hook selection, pacing decisions. "
    "Write this BEFORE filling in the segments."
)
```

New:
```python
editorial_reasoning: str = Field(
    description=(
        "Your editorial thinking process. Address these in order: "
        "1) CONSTRAINT CHECK — for each filmmaker MUST-INCLUDE/MUST-EXCLUDE, state which "
        "clip and segment satisfies it. If unsatisfiable, explain why. "
        "2) Story concept — what story does this footage tell? "
        "3) Opening hook — what is the strongest first 10 seconds? "
        "4) Arc structure — beginning/middle/end with clip assignments. "
        "5) Pacing plan — where is the edit fast vs slow, energetic vs contemplative?"
    )
)
```

### 1.3 Instruction Anchoring

**File:** `src/ai_video_editor/editorial_prompts.py` — `build_editorial_assembly_prompt()` (line 324)

Current final instruction:
```python
prompt += (
    "\n\nNow produce the EditorialStoryboard. "
    "Use the editorial_reasoning field to think through your editorial decisions "
    f"before filling in the segments for a compelling {style}."
)
```

New:
```python
prompt += (
    "\n\nNow produce the EditorialStoryboard."
    "\n\nBEFORE writing segments, use editorial_reasoning to:"
    "\n1. State how you satisfy each filmmaker MUST-INCLUDE/MUST-EXCLUDE constraint"
    "\n2. Explain your story arc and opening hook choice"
    "\n3. Note any constraints you cannot satisfy and why"
    "\n\nThen produce the segments. The filmmaker's MUST-INCLUDE and MUST-EXCLUDE "
    f"items are non-negotiable requirements, not suggestions."
)
```

### 1.4 Temperature Default Adjustment

**File:** `src/ai_video_editor/config.py` — `GeminiConfig.phase2_temperature` and `ClaudeConfig.phase2_temperature`

Lower the default Phase 2 temperature from 0.8 → 0.6. Rationale: with stronger constraint language in the prompt (1.1) and structured reasoning checkpoints (1.2), the model needs less randomness to produce creative output — the creativity comes from the `editorial_reasoning` field, not from temperature. 0.6 balances instruction-following with creative variation.

Note: a conditional approach ("lower temp when constraints are present") doesn't work because the briefing always includes `highlights` and `avoid` as standard questions — these keys are always present regardless of how specific the filmmaker's answers are.

### Verification (Phase 1)

Run `vx analyze` on an existing project with specific must-include/must-exclude constraints. Compare:
- Does `editorial_reasoning` now explicitly reference each constraint?
- Are MUST-INCLUDE moments present in segments?
- Are MUST-EXCLUDE items absent?

Use Phoenix traces to compare prompt size and response quality before/after.

---

## Phase 2: Multi-Call Phase 2 (Editorial Storyboard)

**Goal:** Split `run_phase2()` into deterministic pre-processing + Call 2A (editorial planning) + Call 2B (precise assembly) + deterministic validation. Based on `docs/refactor_plan/Editorial Storyboard Pipeline: Multi-Call Architecture Plan.md`, verified against codebase.

**Duration:** ~16-20 hours

### 2.0 Multi-Call Design: Freeform Reasoning → Structured Output

**Critical design decision:** Call 2A must NOT use `response_schema` / structured output.

The "Let Me Speak Freely?" (ICLR 2025) finding shows JSON-mode degrades reasoning by 10-15%. The whole point of splitting Phase 2 is to separate *thinking* from *structuring*. If Call 2A must produce `PlannedSegment` objects with `clip_id`, `usable_segment_index`, `purpose`, `arc_phase` — the model is simultaneously reasoning AND structuring, which is the problem we're solving.

**Revised three-step design:**

```
Call 2A (Reasoning)        Call 2A.5 (Structuring)      Call 2B (Assembly)
┌────────────────────┐    ┌────────────────────┐    ┌────────────────────┐
│ Freeform text       │    │ Schema-bound        │    │ Schema-bound        │
│ No response_schema  │───▶│ response_schema=    │───▶│ response_schema=    │
│ Temp: 0.8-1.0       │    │ StoryPlan           │    │ EditorialStoryboard │
│ Think freely about  │    │ Temp: 0.2           │    │ Temp: 0.3           │
│ story, arc, clips   │    │ Faithful conversion │    │ Precise timestamps  │
│ constraint check    │    │ of plan → JSON      │    │ within bounded      │
└────────────────────┘    └────────────────────┘    │ segment windows     │
                                                     └────────────────────┘
```

**Call 2A** (Reasoning — freeform, highest quality):
- No `response_schema`, no `response_mime_type="application/json"`
- Temperature 0.8-1.0 (creative editorial judgment)
- Output: natural language editorial plan (~500-2000 tokens of prose)
- Prompt asks the model to: check each filmmaker constraint, propose a story arc, select clips with specific usable segments, explain pacing and audio strategy
- This is where the editorial quality lives — unconstrained reasoning

**Call 2A.5** (Structuring — cheap, mechanical):
- `response_schema=StoryPlan`
- Temperature 0.2 (faithful conversion, no creativity needed)
- Input: Call 2A's freeform plan + condensed clip list (for ID resolution)
- Prompt: "Convert this editorial plan into a StoryPlan JSON. Your job is faithful translation — do not add, remove, or change editorial decisions from the plan."
- Model: cheapest available (Gemini Flash Lite). This is a formatting task, not a reasoning task.
- Cost: ~$0.01-0.02 (small context, simple task)

**Call 2B** (Assembly — schema-bound, precise):
- Same as before — receives StoryPlan + selected clip data, produces EditorialStoryboard with timestamps

**Why three calls instead of two?** The extra structuring call costs ~$0.01-0.02 and takes ~2-3 seconds. In return, we get:
1. Full reasoning quality in Call 2A (no JSON-mode penalty)
2. Programmatic validation of the structured plan (can check constraints, verify clip IDs)
3. Clean separation: thinking → structuring → assembly

### 2.0.1 New Pydantic Models

**File:** `src/ai_video_editor/models.py`

Add `StoryPlan` and `PlannedSegment` (output of Call 2A.5, input to Call 2B):

```python
class PlannedSegment(BaseModel):
    clip_id: str = Field(description="Full clip ID from the available clips list")
    usable_segment_index: int = Field(description="Index into the clip's usable_segments array from the review")
    purpose: str = Field(description="opening_hook|establishing|context|action|reaction|b_roll|climax|outro")
    arc_phase: str = Field(description="opening_context|experience|closing_reflection")
    narrative_role: str = Field(description="What this segment contributes to the story — 1 sentence")
    audio_strategy: str = Field(description="preserve_dialogue|music_bed|ambient_only")
    is_speech_segment: bool = Field(description="True if primary content is dialogue")

class StoryPlan(BaseModel):
    title: str
    style: str
    story_concept: str = Field(description="2-3 sentence narrative summary")
    cast: list[CastMember]
    story_arc: list[StoryArcSection]
    planned_segments: list[PlannedSegment]
    discarded: list[DiscardedClip]
    pacing_notes: str
    music_direction: str
    constraint_satisfaction: str = Field(description="For each filmmaker constraint, state how it was satisfied or why it couldn't be")
```

Note: `StoryPlan` intentionally has NO `editorial_reasoning` field — the reasoning lives in Call 2A's freeform output, not in the structured representation. The `constraint_satisfaction` field captures just the constraint-checking conclusions.

### 2.1 Deterministic Pre-Processing

**File:** `src/ai_video_editor/editorial_prompts.py` — new functions

**`extract_cast_from_reviews(clip_reviews)`** — Deduplicate people across all clip reviews into a single cast list. Currently the same person (e.g., "man with glasses wearing green Puma t-shirt") appears in 12+ clip reviews. Extract once, reference by label.

**`condense_clip_for_planning(review)`** — Strip people arrays (replaced by cast reference), strip full editorial notes, keep: clip_id, duration, content_type, cast_present (labels only), speakers (labels only), key_moments (compact), usable_segments (with index), audio_summary, has_speech.

**`trim_transcript_to_usable(transcript_text, usable_segments)`** — Filter transcript lines to only those within usable segment time ranges. Currently `_load_all_transcripts_for_prompt()` loads full transcripts including discarded portions.

Prompt size impact (verified from refactor plan Appendix A):
- Current single call: ~30K chars
- Call 2A (planning): ~16K chars (condensed reviews + cast + context)
- Call 2B (assembly): ~11K chars (selected clips only, no style/context overhead)

### 2.2 Call 2A — Freeform Editorial Reasoning

**File:** `src/ai_video_editor/editorial_prompts.py` — new `build_phase2a_reasoning_prompt()`
**File:** `src/ai_video_editor/editorial_agent.py` — new `run_phase2a_reasoning()`

Input: deduplicated cast, condensed clip summaries, trimmed transcripts, user context (with constraint hierarchy from Phase 1), style supplement, filming timeline.

NOT included: full clip review people arrays, timestamp validation rules, full editorial notes.

**Output: freeform text** (no response_schema). The prompt asks:

```
You are a professional video editor who has watched all the dailies.
Write an editorial plan for a {style} from this footage.

Your plan MUST address:
1. CONSTRAINT CHECK: For each filmmaker MUST-INCLUDE and MUST-EXCLUDE item,
   state which clip and usable segment satisfies it, or explain why it can't be satisfied.
2. STORY CONCEPT: What narrative does this footage support?
3. OPENING HOOK: What are the strongest first 10 seconds?
4. SEGMENT SEQUENCE: List each segment you want to include, in output order:
   - Clip ID (use FULL clip IDs from the list above)
   - Which usable segment (by index number)
   - Purpose in the edit (hook, establishing, context, action, climax, outro)
   - Audio strategy (preserve dialogue, music bed, ambient only)
5. DISCARDED CLIPS: Which clips are you cutting and why?
6. PACING: Where is the edit fast vs slow? Where does it breathe?
7. MUSIC DIRECTION: What audio strategy ties this together?

Think freely. Write in natural language. Be specific about clip references.
```

Temperature: 0.8 (creative judgment, unconstrained by schema).

Visual mode: Proxy video bundles attach to Call 2A only (editorial judgment needs visual reference).

### 2.2.1 Call 2A.5 — Plan Structuring

**File:** `src/ai_video_editor/editorial_prompts.py` — new `build_phase2a_structuring_prompt()`
**File:** `src/ai_video_editor/editorial_agent.py` — new `run_phase2a_structuring()`

Input: Call 2A's freeform text + condensed clip list (for ID resolution).

Output: `StoryPlan` Pydantic model via `response_schema`.

Prompt: "Convert this editorial plan into a StoryPlan JSON. Faithfully translate every decision — do not add, remove, or change editorial choices. If the plan references a clip ambiguously, resolve it to the closest matching full clip ID from the available list."

Model: cheapest available (e.g., `gemini-3.1-flash-lite-preview`). Temperature: 0.2.

**Checkpoint (deterministic):** After structuring, validate:
- All MUST-INCLUDE moments appear in planned_segments (fuzzy-match against Phase 1 key_moments)
- All MUST-EXCLUDE items absent
- Selected clip IDs exist in clip_reviews
- usable_segment_index is valid for each clip
- Story arc phase distribution within acceptable range

If validation fails: log warnings, optionally block Call 2B. Save the freeform reasoning text alongside the StoryPlan for debugging.

### 2.3 Call 2B — Precise Assembly

**File:** `src/ai_video_editor/editorial_prompts.py` — new `build_phase2b_assembly_prompt()`
**File:** `src/ai_video_editor/editorial_agent.py` — new `run_phase2b_assembly()`

Input: StoryPlan (from Call 2A.5) + full clip review data for ONLY the selected clips + full transcripts for selected clips only + usable segment boundaries as **explicit per-segment constraints**.

NOT included: unselected clip reviews, style supplement, filmmaker Q&A, cast deduplication work.

#### Timestamp Continuity Guarantee

This is the critical design that ensures multi-call doesn't degrade timestamp precision. Timestamps flow as structured data — no call re-derives them from raw text.

**The chain:**
1. **Phase 1** produces `usable_segments` per clip with `in_sec`, `out_sec`, `duration_sec` — these are the source of truth.
2. **Call 2A** references segments by `usable_segment_index` — no timestamps generated.
3. **Call 2A.5** faithfully translates index references into `StoryPlan.planned_segments[].usable_segment_index` — still no timestamps.
4. **Orchestrator code** (deterministic) resolves each `usable_segment_index` to its actual `in_sec`/`out_sec` range from Phase 1 data, and injects these as **explicit bounded constraints** into Call 2B's prompt.
5. **Call 2B** receives bounded windows and selects precise sub-ranges within them.
6. **Code validation** clamps any timestamp that exceeds the usable segment bounds.

At no point does the LLM need to track timestamp ranges from distant context. Each planned segment arrives with its bounds pre-resolved.

#### Prompt Structure for Call 2B

```
You are assembling a video edit from a pre-approved editorial plan.

For each planned segment below, select precise in_sec and out_sec
timestamps WITHIN the usable segment range shown. Your job is
mechanical refinement — the creative decisions are already made.

HARD CONSTRAINTS:
- in_sec and out_sec must fall within the "Usable range" shown
- in_sec < out_sec
- Timestamps are in seconds, relative to clip start (not global timeline)
- Use the transcript to find natural cut points (sentence boundaries,
  pauses, scene transitions)

## Planned Segment 1
Clip: 20260328172340_C0041
Usable range: 0.0s – 9.0s (9.0s available)     ← from Phase 1 data
Purpose: opening_hook
Plan: HungYi walking and turning to smile at camera
Audio: preserve_dialogue
Transcript:
  [0:00] Max: 我們現在在那個 ... [0:06] Max: 麥口，轉過來

→ Select in_sec and out_sec. Write segment description and audio_note.

## Planned Segment 2
Clip: 20260328174512_C0045
Usable range: 27.0s – 54.0s (27.0s available)   ← from Phase 1 data
Purpose: context
Plan: Wide shot of the starting area, crowd energy building
Audio: ambient_only
Transcript:
  [0:28] Max: 好多人喔 ... [0:35] HungYi: 對啊

→ Select in_sec and out_sec. Write segment description and audio_note.
```

The model sees a dramatically simpler task — bounded windows to fill in, not a sea of clip data to navigate. The creative decisions are made; this is mechanical refinement.

Output: `EditorialStoryboard` Pydantic model (same schema as today — no downstream changes to render/rough_cut).

Temperature: 0.3 (lower than current 0.8 — this is assembly, not creative judgment).

### 2.4 Enhanced Validation (Deterministic, No LLM)

**File:** `src/ai_video_editor/editorial_agent.py` — new `validate_and_fix_storyboard()`

The existing `validate_storyboard()` (line 846) detects errors but cannot fix them. The enhanced version auto-fixes recoverable issues — the final safety net in the timestamp continuity chain.

```python
def validate_and_fix_storyboard(
    storyboard: EditorialStoryboard,
    clip_reviews: list[dict],
    story_plan: StoryPlan,
) -> tuple[EditorialStoryboard, list[str], list[str], bool]:
    """Validate and auto-fix. Returns (storyboard, fix_log, warnings, is_critical)."""
    fix_log = []
    reviews_by_id = {r["clip_id"]: r for r in clip_reviews}

    # 1. Resolve abbreviated clip IDs (existing _resolve_clip_id_refs logic)
    _resolve_clip_id_refs(storyboard, set(reviews_by_id.keys()))

    # 2. Clamp timestamps to usable segment bounds
    for seg in storyboard.segments:
        review = reviews_by_id.get(seg.clip_id)
        if not review:
            continue
        usable = review.get("usable_segments", [])
        # Find matching usable segment by overlap
        matching = _find_matching_usable(seg, usable)
        if matching:
            if seg.in_sec < matching["in_sec"]:
                fix_log.append(
                    f"Seg {seg.index}: clamped in_sec {seg.in_sec:.1f} → {matching['in_sec']:.1f}"
                )
                seg.in_sec = matching["in_sec"]
            if seg.out_sec > matching["out_sec"]:
                fix_log.append(
                    f"Seg {seg.index}: clamped out_sec {seg.out_sec:.1f} → {matching['out_sec']:.1f}"
                )
                seg.out_sec = matching["out_sec"]

    # 3. Verify in_sec < out_sec after clamping
    # 4. Verify no duplicate segment indices
    # 5. Recalculate estimated_duration_sec from sum of segment durations
    # 6. Run existing validate_storyboard() for remaining checks

    return storyboard, fix_log, warnings, is_critical
```

**Evidence this is needed:** The existing `validate_storyboard()` already catches the exact errors this fixes — timestamp overflows and unknown clip IDs are the primary warnings. The `_resolve_clip_id_refs()` function exists solely because the LLM abbreviates clip IDs in late-generation output. With the split pipeline, these errors should be rare (Call 2B receives bounded windows), but the safety net ensures zero timestamp errors reach the renderer regardless.

Return type changes from `(warnings, is_critical)` to `(storyboard, fix_log, warnings, is_critical)`. The fix_log is saved alongside the storyboard for debugging.

### 2.5 Orchestrator Refactor

**File:** `src/ai_video_editor/editorial_agent.py` — refactor `run_phase2()`

Add `use_split_pipeline: bool` parameter (default False during migration). When True:
1. Pre-process: extract cast, condense clips, trim transcripts
2. Call 2A (Reasoning): freeform editorial plan → raw text
3. Call 2A.5 (Structuring): raw text → StoryPlan JSON (cheap model)
4. Checkpoint: validate StoryPlan against constraints
5. Filter: select only planned clips for Call 2B
6. Call 2B (Assembly): StoryPlan + selected clip data → EditorialStoryboard
7. Validate & fix: enhanced validation with auto-clamping
8. Version & save: same existing logic, plus save freeform plan + StoryPlan as intermediate artifacts

Artifacts versioned and saved:
- `editorial_plan_{provider}_v{N}.txt` — Call 2A freeform reasoning (human-readable, debuggable)
- `storyplan_{provider}_v{N}.json` — Call 2A.5 structured plan
- `editorial_{provider}_v{N}.json` — Call 2B final storyboard (existing format)

This enables: re-running Call 2A.5 from cached plan text, re-running Call 2B from cached StoryPlan, inspecting reasoning without re-running anything.

### 2.6 Tracing for Multi-Call

**File:** `src/ai_video_editor/tracing.py`

The existing `traced_gemini_generate()` already records per-call traces with `phase` field. For multi-call:
- Call 2A: `phase="phase2a_reasoning"`
- Call 2A.5: `phase="phase2a_structuring"`
- Call 2B: `phase="phase2b_assembly"`
- Validation call (Phase 3): `phase="phase2_validation"`

`ProjectTracer.summary()` already groups by phase via `_group_by_phase()`. No changes needed to the tracing infrastructure — just use distinct phase labels.

For Phoenix/OpenTelemetry: the auto-instrumentation from `GoogleGenAIInstrumentor` captures each `generate_content()` call as a separate span. The phase label can be set as a span attribute via OpenTelemetry's `set_attribute()` if we want hierarchical grouping. This is a nice-to-have, not blocking.

**Key tracing insight:** With three calls, we can now diagnose WHERE quality degrades:
- Bad clip selection? → Check Call 2A freeform text (reasoning quality)
- Bad structuring of good reasoning? → Check Call 2A.5 (faithful translation)
- Bad timestamps on good plan? → Check Call 2B (assembly precision)
- This was impossible with the single-call design.

### 2.7 Config

**File:** `src/ai_video_editor/config.py`

Add to `GeminiConfig`:
```python
phase2b_temperature: float = 0.3  # assembly is mechanical, not creative
use_split_pipeline: bool = False   # migration toggle
```

### Verification (Phase 2)

1. Run both pipelines (single-call and split) on the same project input
2. Compare: timestamp accuracy (count `validate_storyboard()` warnings), constraint satisfaction, editorial quality
3. Use Phoenix to compare: total latency, token usage, cost per phase
4. StoryPlan intermediate artifact should be inspectable — review it manually for clip selection quality

---

## Phase 3: Post-Generation Validation Call

**Goal:** Add a cheap Gemini Flash validation call after Phase 2 to catch constraint violations before rendering. This is Technique 4 from the research doc.

**Duration:** ~4-6 hours

**File:** `src/ai_video_editor/editorial_agent.py` — new `run_phase2_validation()`

After Call 2B produces the storyboard, run a lightweight validation call:

```
The filmmaker specified these constraints:
1. MUST INCLUDE: [from user_context.highlights]
2. MUST EXCLUDE: [from user_context.avoid]
3. Tone: [from user_context.tone]

Here is the generated storyboard:
[storyboard segments — clip_id, in/out, purpose, description only]

For each constraint, answer:
- SATISFIED: YES/NO
- If NO: which segment to add/remove/modify, and how
```

Model: cheapest available (Gemini Flash Lite or similar). Temperature: 0.1.

If any MUST constraint fails: either auto-retry Phase 2 with the validation feedback prepended, or log and warn the user.

Cost: ~$0.01 per validation call. Only adds one cheap call per project.

Tracing: `phase="phase2_validation"`.

---

## Phase 4: Multi-Call Phase 3 (Visual Monologue)

**Goal:** Split `run_monologue()` into three calls based on `docs/refactor_plan/Visual Monologue Pipeline: Multi-Call Architecture Plan.md`.

**Duration:** ~12-16 hours

### 4.1 Call 1: Segment Analysis & Arc Planning
- Input: full segment table, trimmed transcripts, filmmaker context
- Output: `OverlayPlan` — eligible segments (no speech), arc phase assignments, intents, surrounding context summaries
- Checkpoint: verify eligibility decisions before burning tokens on creative generation

### 4.2 Call 2: Creative Text Generation
- Input: Call 1's OverlayPlan + creative rules only (persona, lowercase, word count, synergy)
- NOT included: full segment table, full transcripts, filmmaker Q&A
- Output: `OverlayDraft` — text + appear_at + duration per overlay

### 4.3 Call 3: Validation & Final Formatting
- Input: Call 2's drafts + Call 1's timing boundaries + constraint rules
- Output: `MonologuePlan` (same schema as today) + validation_log
- This call can be partially or fully replaced by deterministic code validation

### 4.4 Reuse from Phase 2
- `StoryPlan.planned_segments[].is_speech_segment` and `.arc_phase` directly feed Call 1's eligibility analysis — potential to skip Call 1 entirely if StoryPlan already contains sufficient data
- Same tracing pattern: `phase="phase3_analysis"`, `phase="phase3_creative"`, `phase="phase3_validation"`
- Same versioning pattern: intermediate artifacts saved for re-runs

---

## Phase 5: Context Compression & Few-Shot Examples

**Goal:** Address context rot for large projects (15+ clips) and add few-shot examples.

**Duration:** ~8-10 hours

### 5.1 Tiered Context Compression

**File:** `src/ai_video_editor/editorial_prompts.py` — modify `_format_clip_reviews_text()`

Add `editorial_priority` field to Phase 1 output (high/medium/low). In Phase 2 prompt building:
- **Tier A (high):** Full review + full transcript + all usable segments
- **Tier B (medium):** Summary + best 2-3 usable segments only
- **Tier C (low/B-roll):** One-line: "C0045: 3min establishing shots, 1 usable segment 45.2-58.0s"

Target: compress 20-clip projects from ~80K tokens to ~25-30K tokens.

### 5.2 User Context → Clip Resolution

**File:** `src/ai_video_editor/editorial_prompts.py` — new `resolve_constraints_to_clips()`

After Phase 1, fuzzy-match user mentions ("the sunset at the temple") against Phase 1 `key_moments.description` and `summary` fields. Append resolved references to constraint block:
```
MUST INCLUDE: The sunset at the temple
  → Likely: C0034 key_moment @185.3s (sunset over temple, editorial_value=high)
  → Also: C0035 usable_segment 0.0-45.0s (temple approach at dusk)
```

### 5.3 Few-Shot Example

**File:** `src/ai_video_editor/editorial_prompts.py` — add to `build_editorial_assembly_prompt()` (or `build_phase2a_planning_prompt()`)

Write 1 example (~500-800 tokens) demonstrating:
- Constraint check in editorial_reasoning
- How constraints map to specific segments
- Story arc with beginning/middle/end
- A discarded clip with clear reason

---

## Phase 6: Evaluation Harness

**Goal:** Systematic comparison of pipeline variants. Based on Part 4 of the research doc.

**Duration:** ~8-12 hours

### 6.1 Test Fixture

Create a reusable test project with:
- 8-12 clips of actual footage (clip metadata + Phase 1 reviews, not video files)
- 2-3 test scenarios with explicit filmmaker constraints
- Ground truth: manually annotated "expected" storyboard decisions (which clips should appear, which should not)

Store in `tests/fixtures/eval_project/`.

### 6.2 Automated Scoring

**File:** new `src/ai_video_editor/eval.py`

Scoring functions for Checkpoint 3 (editorial decisions):
- `score_constraint_satisfaction(storyboard, user_context)` → binary per constraint
- `score_segment_redundancy(storyboard, clip_reviews)` → count near-duplicate segments (would use CLIP embeddings when available, text similarity for now)
- `score_timestamp_precision(storyboard, clip_reviews)` → % of segments with valid timestamps within usable bounds

### 6.3 Pipeline Variant Runner

CLI command or script that runs multiple pipeline configurations on the same test fixture:
- Baseline (current single-call)
- +Prompt hardening (Phase 1 changes)
- +Multi-call (Phase 2 split)
- +Validation call (Phase 3)

Output: comparison table with scores per variant, token usage, cost, latency.

Tracing: each variant run gets a distinct trace session in Phoenix for side-by-side inspection.

---

## Files to Modify (Summary)

| File | Phase | Changes |
|------|-------|---------|
| `briefing.py` | 1 | Rewrite `format_context_for_prompt()` — constraint hierarchy |
| `models.py` | 1, 2 | Update `editorial_reasoning` field description; add `StoryPlan`, `PlannedSegment` |
| `editorial_prompts.py` | 1, 2, 5 | Instruction anchoring; add `build_phase2a_reasoning_prompt()`, `build_phase2a_structuring_prompt()`, `build_phase2b_assembly_prompt()`, `extract_cast_from_reviews()`, `condense_clip_for_planning()`, `trim_transcript_to_usable()`, `resolve_constraints_to_clips()`; few-shot example |
| `editorial_agent.py` | 1, 2, 3 | Refactor `run_phase2()` with split pipeline; add `run_phase2a_reasoning()`, `run_phase2a_structuring()`, `run_phase2b_assembly()`, `run_phase2_validation()`; enhance `validate_storyboard()` |
| `config.py` | 1, 2 | Lower `phase2_temperature` default to 0.6; add `phase2b_temperature`, `structuring_model`, `use_split_pipeline` to GeminiConfig |
| `tracing.py` | 2 | Phase labels for multi-call (minimal changes — existing infra supports it) |
| `eval.py` (new) | 6 | Evaluation scoring functions |

## Existing Code to Reuse

| Function | File | How it's reused |
|----------|------|-----------------|
| `traced_gemini_generate()` | tracing.py | Wraps all Gemini calls — handles retry, tracing, cost. No changes needed. |
| `_resolve_clip_id_refs()` | editorial_agent.py | Still runs as safety net in validation step. |
| `validate_storyboard()` | editorial_agent.py | Enhanced with auto-fix, same core logic. |
| `_format_clip_reviews_text()` | editorial_prompts.py | Used in Call 2B for selected clips. Add `exclude_people` param. |
| `format_concat_timeline()` | preprocess.py | Used in Call 2A for visual timeline. |
| `begin_version()`/`commit_version()` | versioning.py | Same versioning for StoryPlan intermediate + final storyboard. |
| `ProjectTracer.record()` | tracing.py | Same tracing with new phase labels. |
| `connect_phoenix()` | tracing.py | Same Phoenix integration, auto-instruments all Gemini calls. |

## Verification Plan

| Phase | How to verify |
|-------|---------------|
| 1 (Prompt) | Run `vx analyze` on existing project. Check `editorial_reasoning` references constraints. Check MUST items appear/absent in segments. Compare Phoenix traces before/after. |
| 2 (Multi-call) | Run both pipelines on same input. Compare `validate_storyboard()` warning counts. Inspect Call 2A freeform plan for reasoning quality. Verify Call 2A.5 faithfully structures the plan. Compare constraint satisfaction and token usage in Phoenix. |
| 3 (Validation) | Deliberately include a constraint the storyboard is likely to miss. Check if validation call catches it. Measure cost overhead (<$0.02). |
| 4 (Monologue) | Compare overlay timestamp accuracy and constraint compliance. Check no overlays on speech segments. |
| 5 (Compression) | Measure prompt token count for 15+ clip project before/after. Compare Phase 2 output quality. |
| 6 (Eval) | Run variant comparison on test fixture. Produce scoring table. |
