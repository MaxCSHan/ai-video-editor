# LLM Orchestration Techniques & Local Model Feasibility

Research notes from analyzing mazsola2k-ai-video-editor, academic papers, and VX's current pipeline. Two focus areas: (1) practical local model opportunities on Apple Silicon, and (2) context manipulation and prompting techniques to improve LLM editorial quality — particularly instruction-following and creative output.

---

## Part 1: Local Models on Apple Silicon

### The Constraint

VX targets MacBook Pro M4 Pro as the baseline. This means:
- 18-36GB unified memory (shared between CPU and GPU)
- ~30 TOPS Neural Engine (not usable by most ML frameworks yet)
- No CUDA — must use MLX, Core ML, or llama.cpp with Metal
- Must not block the main pipeline or make the machine unusable during inference

### What mazsola2k Does Locally

mazsola2k runs its entire AI stack on a desktop NVIDIA GPU:
- Qwen2.5-VL-7B (4-bit GGUF, ~4.7GB VRAM) — frame captioning + scene reasoning
- CLIP ViT-B/32 (~350MB) — semantic scoring
- ResNet-50 (~100MB) — feature extraction / duplicate detection

Key insight: **the LLM is the bottleneck** (~1.2 frames/sec for captioning). CLIP and ResNet are fast batch operations.

### Critical Difference: VX vs mazsola2k Task Requirements

Before evaluating any local model, we must acknowledge a fundamental gap in what these two projects demand from AI:

**mazsola2k's task:** "Is this segment boring or interesting?" → assign a speed multiplier. This is a **low-precision classification** problem. All footage is kept; the AI only decides how fast to play it. A wrong classification means a boring segment plays at 1x instead of 4x — suboptimal but not broken.

**VX's task:** "Which 5-second subclip from this 3-minute clip best serves the narrative?" → precise timestamp selection for cut-and-recompose editing. This is a **high-precision editorial judgment**. A wrong selection means the wrong moment appears in the final video. Temporal continuity is not preserved — segments are rearranged for narrative arc.

This means: **local models cannot simply replace cloud APIs for VX's editorial reasoning.** The quality bar is fundamentally higher. Local models are valuable as a **metadata enrichment layer** — giving the cloud LLM better information to reason with, not making the editorial decisions themselves.

### Realistic Local Model Candidates for M4 Pro

#### Tier 1: Already proven feasible

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **mlx-whisper** (small/medium) | Transcription | 1-3GB | ~10x real-time | In VX codebase but **untested in practice**. See transcription section below. |
| **CLIP ViT-B/32** (MLX) | Semantic embeddings | ~350MB | Fast (batch) | Well-understood, deterministic. See VX-specific use cases below. |
| **SigLIP** (MLX) | Better CLIP | ~400MB | Fast (batch) | Google's improved CLIP. Better zero-shot accuracy. |

#### Tier 2: Worth testing, likely feasible

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **Qwen2.5-VL-3B** (4-bit MLX) | Frame captioning | ~2GB | ~15-25 tok/s | See "withdrawn" note below — limited value for VX. |
| **Florence-2** (MLX) | Object detection + captioning | ~500MB | Fast | Microsoft's efficient vision model. Structured extraction. |
| **pyannote** (CPU) | Speaker diarization | ~500MB | ~real-time | Pairs with whisper for speaker-attributed transcripts. |

#### Tier 3: Possible but risky on base configs

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **Qwen2.5-VL-7B** (4-bit MLX) | Full vision-language | ~5GB | ~8-12 tok/s | Usable on 36GB configs. Tight on 18GB alongside other processes. |
| **Llama 3.2 11B Vision** (4-bit) | Vision-language | ~7GB | ~6-10 tok/s | Apple Silicon MLX support exists. Memory-hungry. |

### Local Transcription: An Untested Alternative

The mlx-whisper path exists in VX's codebase but has **never been tested end-to-end**. The Gemini transcription path was chosen initially because it provides speaker diarization for free — knowing WHO is speaking drives segment selection in Phase 2.

However, the Gemini transcription path has known issues (timestamp drift on long clips, chunk boundary artifacts). Local transcription deserves evaluation as an alternative.

**A viable local transcription stack:**
- **mlx-whisper** (medium or large-v3) — word-level transcription with timestamps
- **whisperX** — wraps whisper with forced alignment for precise word-level timestamps
- **pyannote** — speaker diarization (runs on CPU, pairs with any whisper variant)

The combination of whisperX + pyannote could produce speaker-attributed, word-level timestamped transcripts locally. The open question is quality — see Part 4 (Evaluation Framework) for how to test this.

**Key risk:** pyannote's speaker diarization requires a HuggingFace token and the model is ~500MB. It works on CPU but may be slow for long clips. Also, it identifies speakers as "SPEAKER_0", "SPEAKER_1" — it cannot name them like Gemini can when given user context. Speaker name resolution would need a post-processing step using briefing data.

### Where Local Models Add Value in VX (Re-evaluated for Cut-and-Recompose Editing)

The key insight from mazsola2k's two-pass design: **use local models as a pre-processing layer, not as the primary reasoning engine.** But the specific use cases must be tailored to VX's higher-precision editorial task, not copied from mazsola2k's classification task.

#### Opportunities — re-evaluated for VX:

**1. CLIP/SigLIP as editorial metadata (NOT as a pre-filter)**

In mazsola2k, CLIP answers "is this boring?" — a binary gate. In VX, that question is too crude. VX needs to know **what is happening and how visually distinct each moment is**, so the editorial LLM can make better subclip selections.

VX-specific use cases for CLIP embeddings:
- **Cross-segment similarity detection:** "Segments C0012:45-60s and C0015:10-25s have 94% visual similarity — using both in the storyboard creates a jarring repeat." Currently Phase 2 relies on the LLM noticing this from text descriptions alone. Embedding similarity is objective and cheap.
- **Visual diversity scoring per clip:** "This clip has high compositional variety at 45-60s (many distinct frames) vs static establishing shot at 0-15s." Helps Phase 2 pick the most visually dynamic subclips.
- **Content clustering across clips:** "Your 17 clips contain 4 visual themes: beach (C0001-C0005), city (C0006-C0010), food (C0011-C0014), people (C0015-C0017)." This is useful structural metadata for Phase 2's story arc construction — the LLM can interleave themes intentionally.

What this is NOT: a filter that decides which clips to skip. VX cannot afford to pre-exclude footage — a "boring" establishing shot might be exactly what the narrative needs as a breathing moment. The editorial LLM must see everything and decide.

- Implementation: Run CLIP during preprocessing, store embeddings + derived metrics in clip metadata JSON
- Risk: Low. We're adding metadata, not making editorial decisions. The LLM still decides.

**2. ~~Local VLM for frame captions~~ — WITHDRAWN for VX**

In mazsola2k, Qwen2.5-VL captions every frame because the LLM cannot see the video directly. The LLM reasons over text summaries instead of visual data.

**VX already sends proxy videos to Gemini.** Gemini sees the actual footage. Adding local captions would be strictly less information than what Gemini already processes. At best redundant, at worst misleading (if the local model misidentifies something, it could bias Gemini's analysis).

The one exception: if we ever want an **offline mode** or **budget mode** that avoids cloud APIs entirely, local captioning becomes the backbone. But that's a different product decision, not an incremental improvement to the current pipeline. Defer this entirely.

**3. Cross-clip similarity detection (CLIP embeddings)**

This is **more important for VX than for mazsola2k**, not less. VX recomposes footage across clips — if Phase 2 unknowingly selects two segments that show nearly identical content from different clips (same location, same angle, different take), the final video has a jarring repeat.

Currently Phase 2 relies on the LLM noticing textual similarity in clip review descriptions. Local embeddings would give it hard data: "segment C0012:45-60s and C0015:10-25s have 94% visual similarity — consider using only one."

- Implementation: Compute CLIP embeddings for usable_segment boundaries (not every frame — just the segments Phase 1 identified). Compare across clips. Inject similarity warnings into Phase 2 prompt.
- Risk: Low. Numerical comparison, not generation.

**4. Scene energy and cut-point scoring (no ML needed)**

Valuable for VX but for different reasons than mazsola2k. mazsola2k uses energy for boring/interesting classification. VX would use it for:
- **Cut-point detection:** Low-motion frames make natural edit points. Motion-free frames cut cleanly without visual jarring. This metadata helps Phase 2 select segment boundaries that align with natural pauses.
- **Pacing profile per clip:** "This segment starts slow, peaks at 12.3s, then settles." Helps Phase 2 place segments in the narrative arc — high-energy segments for climax, low-energy for transitions.
- **Audio energy mapping:** Speech vs silence vs ambient noise. Helps Phase 2 decide which segments need music overlay vs which have natural audio worth preserving. This partially overlaps with transcription but is complementary — transcription catches words, energy scoring catches non-verbal audio character.

- Implementation: OpenCV frame differencing for motion, librosa/scipy for audio RMS. Pure signal processing, no ML. Run during preprocessing.
- Risk: Zero. Deterministic operations.

### Recommendation (Revised)

**Start with:**
1. **Local energy scoring** (zero risk, no ML, immediate value for cut-point metadata)
2. **CLIP embedding computation** during preprocessing (low risk, enables similarity detection and content clustering)

**Test separately:**
3. **Local transcription stack** (mlx-whisper + pyannote) — must benchmark against Gemini transcription before committing. See Part 4 evaluation framework.

**Defer:**
4. Local VLM captioning — not needed while cloud APIs handle video understanding
5. Local editorial reasoning — quality bar too high for current local models

---

## Part 2: LLM Orchestration — Improving Instruction-Following and Editorial Quality

### The Problem

Even with user context provided (briefing answers, must-include moments, things to avoid), Phase 2 storyboard results frequently ignore these instructions. The LLM "does its own thing" — producing reasonable but generic edits rather than following the filmmaker's specific vision.

This is a known failure mode in LLM systems: **instruction dilution under long context.** The more data you provide (clip reviews, transcripts, visual references), the more the LLM treats user instructions as suggestions rather than requirements.

### Root Cause Analysis

Reading VX's current prompt architecture (`editorial_prompts.py`), the Phase 2 prompt flows like this:

```
1. System role + rules (~300 tokens)
2. User context (~200-400 tokens)          ← filmmaker's intent
3. Style supplement (~200-500 tokens)
4. Filming timeline (~100-300 tokens)
5. Clip reviews + transcripts (~2,000-20,000 tokens)  ← DOMINATES
6. Visual reference instructions (~200 tokens)
7. Final instruction (~50 tokens)
```

**Problem 1: Signal-to-noise ratio.** User context is 200-400 tokens buried in a prompt that may be 5,000-25,000+ tokens. The LLM's attention is dominated by the clip review data.

**Problem 2: Soft language.** The user context ends with "Prioritize the filmmaker's stated preferences" — this is a suggestion, not a constraint. LLMs treat "prioritize" as "consider" not "must follow."

**Problem 3: No structural enforcement.** User preferences are free-text mixed with data. There's no mechanism that forces the LLM to address each preference explicitly.

**Problem 4: Reasoning competition.** The `editorial_reasoning` field lets the LLM think freely, but there's no requirement that it reference user constraints during reasoning. The LLM can reason its way past user instructions ("while the filmmaker wanted X, the footage better supports Y").

### Technique 1: Constraint Hierarchy (Hard Rules vs Soft Preferences)

Separate user inputs into hard constraints and soft preferences. Hard constraints are non-negotiable; soft preferences guide creative choices.

**Current approach:**
```
The filmmaker provided the following context:
- **Must-include moments**: The sunset at the temple
- **Things to avoid**: Don't use any shaky footage from the bus ride
- **Desired tone**: Calm, contemplative
```

**Improved approach:**
```
FILMMAKER CONSTRAINTS (non-negotiable — violating these makes the edit unusable):
1. MUST INCLUDE: The sunset at the temple — at least one segment must feature this.
2. MUST EXCLUDE: All footage from the bus ride (clips X, Y if identifiable).
3. If you cannot satisfy a constraint, explain why in editorial_reasoning.

FILMMAKER PREFERENCES (guide your creative choices):
- Desired tone: Calm, contemplative
- Duration preference: 8-12 minutes
```

**Why this works:** LLMs respond much more reliably to explicit constraint language ("MUST", "non-negotiable", "violating makes the edit unusable") than to soft guidance ("Prioritize"). The accountability clause ("explain why if you can't") forces the LLM to explicitly reason about each constraint rather than silently ignoring them.

### Technique 2: Structured Reasoning with Checkpoints

Force the LLM to address user constraints explicitly in its reasoning before producing segments.

**Current `editorial_reasoning` field description:**
```python
Field(description="Think through your editorial decisions before filling in segments")
```

**Improved field description:**
```python
Field(description=(
    "Your editorial thinking process. You MUST address these in order: "
    "1) Constraint check — for each filmmaker constraint, state how you will satisfy it "
    "and which clip(s) fulfill it. "
    "2) Story concept — what story does this footage tell? "
    "3) Opening hook — what's the strongest first 10 seconds? "
    "4) Arc structure — beginning/middle/end with segment assignments. "
    "5) Pacing — where is the edit fast vs slow? "
    "If a filmmaker constraint cannot be satisfied, explain why here."
))
```

**Why this works:** By encoding a reasoning checklist in the schema field description, the LLM must explicitly address user constraints as step 1, before it starts making creative decisions. This is inspired by the "Let Me Speak Freely?" (ICLR 2025) finding: **reasoning before answering** prevents the model from committing to a plan that ignores constraints.

### Technique 3: Two-Call Decoupled Reasoning

Split Phase 2 into two LLM calls: (a) freeform editorial reasoning, then (b) structured output generation.

```
Call 1 (Reasoning — freeform text, no schema):
  "You are a video editor. Here are clip reviews and the filmmaker's requirements.
   Write an editorial plan addressing each filmmaker constraint, then describe
   the story arc, segment sequence, and pacing decisions."
  
  → Output: 500-1500 tokens of free-text reasoning

Call 2 (Structuring — schema-bound):
  "Convert this editorial plan into an EditorialStoryboard JSON.
   The plan: {reasoning_output}
   Clip reviews: {reviews}
   
   Your job is faithful translation — do not add, remove, or change
   editorial decisions from the plan."
  
  → Output: EditorialStoryboard Pydantic model
```

**Why this works:** The "Let Me Speak Freely?" paper showed that forcing simultaneous reasoning + JSON output degrades quality by 10-15%. The current `editorial_reasoning` field is a single-call approximation. Decoupling fully eliminates the penalty. The reasoning call can use higher temperature (1.0) for creativity; the structuring call can use lower temperature (0.3) for precision.

**Cost:** 2x API calls for Phase 2. But Phase 2 is a single call per project, so cost impact is marginal ($0.05-0.20 extra).

**VX's cookbook already identifies this** (Section 5: "Two-stage reasoning — future consideration"). The evidence from mazsola2k supports it: their two-pass approach is conceptually similar — compute features first, reason separately.

### Technique 4: User Context Echo-Back Validation

After generating the storyboard, run a lightweight validation call that checks constraint satisfaction.

```
Validation Call (cheap model, e.g., Gemini Flash):
  "The filmmaker specified these constraints:
   1. Must include: sunset at the temple
   2. Must exclude: bus ride footage
   3. Tone: calm, contemplative
   
   Here is the generated storyboard:
   {storyboard_json}
   
   For each constraint, answer YES or NO — is it satisfied?
   If NO, suggest a specific fix (which segment to add/remove/modify)."
```

If any constraint fails, either auto-fix (if the fix is simple — e.g., add a segment) or re-run Phase 2 with the validation feedback prepended as an additional instruction.

**Why this works:** Self-Refine paper shows iteratively refined outputs are preferred ~20% more by humans. This is a targeted version — only re-runs when constraints are violated, not unconditionally.

**Cost:** One cheap validation call (~$0.01). Re-run only on failure.

### Technique 5: Context Compression for Large Projects

For projects with 15+ clips, the raw clip reviews + transcripts can exceed 50K tokens, entering the "context rot" zone documented in VX's cookbook (Chroma, July 2025: 20-50% accuracy drops from 10K to 100K tokens).

**Tiered context strategy:**

```
Tier A (full detail): Clips rated as "high editorial value" by Phase 1
  → Full review + full transcript + all usable segments

Tier B (summary): Clips rated as "moderate" by Phase 1
  → Summary line + best 2-3 usable segments only

Tier C (mention): Clips rated as "low/B-roll only" by Phase 1
  → One-line description: "C0045: 3min establishing shots of market, mostly shaky. 
     One usable segment: 45.2-58.0s (market entrance, good quality)"
```

This compresses 20 clips from ~80K tokens to ~20-30K tokens while preserving full detail for the clips that matter most.

**Implementation:** Add an `editorial_priority` field to Phase 1 output (high/medium/low). In `_format_clip_reviews_text()`, switch formatting based on priority tier.

### Technique 6: Instruction Anchoring (Repeat at End)

The "lost in the middle" bias means early instructions (user context at position 2) lose attention weight as context grows. The final instruction currently says:

```
Now produce the EditorialStoryboard. Use the editorial_reasoning field to think
through your editorial decisions before filling in segments for a compelling {style}.
```

**Improved final instruction:**
```
Now produce the EditorialStoryboard.

BEFORE writing segments, use editorial_reasoning to:
1. State how you satisfy each filmmaker constraint (review them above)
2. Explain your story arc and opening hook choice
3. Note any constraints you cannot satisfy and why

Then produce the segments. Remember: the filmmaker's must-include and must-exclude
items are non-negotiable requirements, not suggestions.
```

**Why this works:** Restating constraints at the end of the prompt exploits recency bias — the LLM pays most attention to the beginning and end. This "bookend" pattern (constraints early + constraint reminder late) is a well-documented prompting technique.

### Technique 7: Few-Shot Example with Constraint Satisfaction

The cookbook identifies missing few-shot examples as a high-priority gap. A good example should demonstrate constraint satisfaction, not just output format.

```
<example>
Filmmaker constraints:
- MUST INCLUDE: The group photo at the summit
- MUST EXCLUDE: All footage where camera is accidentally recording inside the bag
- Tone: Triumphant, building tension toward the summit

editorial_reasoning: "The filmmaker requires the summit group photo — this appears
in clip C0023 at 142.5-155.0s. I'll place it as the climax at segment 8. The 
bag footage appears in C0005 (0-45s) and C0012 (0-12s) — excluding both entirely.
For triumphant tone, I'll build the arc from preparation (base camp) through 
increasingly dramatic trail footage, culminating in the summit..."

segments: [
  {index: 0, clip_id: "C0023", in_sec: 148.0, out_sec: 153.0, 
   purpose: "hook", description: "Flash-forward: summit celebration..."},
  ...
  {index: 8, clip_id: "C0023", in_sec: 142.5, out_sec: 155.0,
   purpose: "climax", description: "The summit group photo — filmmaker's 
   must-include moment. Full duration preserved."},
]
</example>
```

**Why this works:** The example shows the model HOW to reference constraints in reasoning and HOW to connect them to specific segments. Without this example, the model has to invent its own approach to constraint handling.

### Technique 8: Structured User Context with Clip ID Resolution

Currently, user answers are free-text ("The sunset at the temple"). The LLM must figure out which clip contains this moment. This is an unnecessary reasoning burden.

**Enhanced briefing → context flow:**

After Phase 1 completes, resolve user mentions to specific clips:

```python
# In briefing post-processing or Phase 2 prompt building:
"MUST INCLUDE: The sunset at the temple
  → Likely matches: C0034 key_moment @185.3s (sunset over temple, editorial_value=high)
  → Also possible: C0035 usable_segment 0.0-45.0s (temple approach at dusk)"
```

This gives the LLM explicit clip references for user constraints, eliminating the need to search through all reviews to find the right moment.

**Implementation:** Fuzzy match user context keywords against Phase 1 `key_moments.description` and `summary` fields. Append resolved references to the constraint block.

### Technique 9: Temperature Calibration for Instruction-Following

Higher temperature increases creativity but decreases instruction-following. VX currently uses 0.8 for Phase 2.

**Consider a split approach:**
- If user provided specific constraints → temperature 0.5-0.6 (more faithful)
- If user provided only vague preferences → temperature 0.8-1.0 (more creative)
- If two-call decoupled: reasoning at 0.8-1.0, structuring at 0.3

The presence of hard constraints should automatically lower temperature, because the task shifts from "create freely" to "create within boundaries."

### Technique 10: Iterative Storyboard Refinement (Human-in-the-Loop)

Instead of one-shot Phase 2 → rough cut, add an optional refinement loop:

```
Phase 2 → Storyboard v1 → Quick preview (HTML) → User feedback
  ↓
"The filmmaker reviewed v1 and provided feedback:
 - Move the market scene earlier, it should set context before the hike
 - The ending feels abrupt, extend the final segment
 - Good opening hook, keep that
 
 Here is the current storyboard: {v1_json}
 
 Produce a revised storyboard addressing this feedback. Keep everything
 the filmmaker approved. Only change what they flagged."
  ↓
Phase 2 (revision) → Storyboard v2
```

**Why this works:** Revision is cheaper and more reliable than generation. The LLM doesn't need to re-reason the entire edit — it only modifies flagged segments. This is how professional editors actually work: rough cut → notes → revision.

**VX's versioning system already supports this** — storyboard versions can be tracked and compared.

---

## Part 3: Synthesis — A Practical Improvement Roadmap

### Phase 0: Foundation (Required before any comparison)

0. **Build evaluation test fixture** — Select 8-12 clip test project, manually annotate transcription ground truth for 3-4 clips, write 2-3 test scenarios with filmmaker constraints. (~2-4 hours, one-time investment. See Part 4.)

### Phase A: Quick Wins — Prompt Engineering (Low effort, high impact)

These target the instruction-following problem directly. No infrastructure changes.

1. **Constraint hierarchy in prompts** — Rewrite `format_context_for_prompt()` to separate MUST constraints from preferences. Add accountability clause. (~1 hour)

2. **Instruction anchoring** — Add constraint reminder to the final instruction block in `build_editorial_assembly_prompt()`. (~30 min)

3. **Structured reasoning checkpoint** — Update `editorial_reasoning` field description to require explicit constraint-check step. (~30 min)

4. **Temperature tuning** — Lower Phase 2 temperature to 0.5-0.6 when hard constraints are present. (~30 min)

Validate Phase A improvements using Checkpoint 3 (Part 4) against the test fixture.

### Phase B: Medium-Term — Context Quality (Moderate effort, significant impact)

5. **Few-shot example** — Write 1-2 examples demonstrating constraint satisfaction in editorial reasoning + segments. Add to Phase 2 prompt. (~2-3 hours, mostly writing)

6. **Context compression** — Add `editorial_priority` to Phase 1 output. Tier clip review detail in Phase 2 based on priority. (~4-6 hours)

7. **User context → clip resolution** — After Phase 1, fuzzy-match user constraints to specific clips. Append resolved references. (~4-6 hours)

8. **Validation call** — Post-Phase 2 constraint checker using Gemini Flash. Auto-flag violations. (~4-6 hours)

### Phase C: Local Metadata Pipeline (Moderate effort, requires evaluation)

9. **Local energy scoring** — Motion (OpenCV frame diff) + audio energy (RMS). Pure signal processing, zero ML, zero risk. Run during preprocessing, store in clip metadata. (~4-6 hours)

10. **CLIP embedding computation** — Compute embeddings during preprocessing. Derive: cross-clip similarity warnings, visual diversity scores, content clusters. Inject as metadata into Phase 1/2 prompts. (~8-12 hours)

Validate Phase C using Checkpoint 2 (Part 4) — does local metadata actually improve Phase 1 review quality?

### Phase D: Local Transcription (High effort, must benchmark first)

11. **Benchmark local transcription** — Test mlx-whisper + pyannote against Gemini using Checkpoint 1 (Part 4). Evaluate WER, speaker attribution, timestamp precision. (~4-6 hours for setup + evaluation)

12. **Speaker name resolution** — If diarization passes, build post-processing to match anonymous SPEAKER_N labels to briefing-provided names. (~4-6 hours)

Only proceed if Checkpoint 1 evaluation shows local stack meets quality thresholds.

### Phase E: Advanced LLM Orchestration (Higher effort, highest quality ceiling)

13. **Two-call decoupled reasoning** — Split Phase 2 into freeform reasoning + structured output. (~8-12 hours, needs testing)

14. **Iterative refinement loop** — User feedback → revision call. Integrate with existing versioning. (~16-24 hours, UX design needed)

---

## Part 4: Evaluation Framework — Comparing Pipeline Variants

### The Problem with Side-by-Side Comparison

A local-model-enriched pipeline and the current cloud-only pipeline produce different intermediate artifacts. We can't simply diff JSON outputs. And watching two full rough cuts side-by-side for every experiment is impractical — it's slow, subjective, and doesn't tell you WHERE the quality difference originated.

The evaluation framework must:
1. Measure quality at **specific checkpoints**, not just end-to-end
2. Distinguish **information quality** (does the output accurately describe the footage?) from **decision impact** (does better information lead to a better final video?)
3. **Fail fast** — validate foundational checkpoints before investing in downstream ones

### Checkpoint Architecture

```
Checkpoint 1: Transcription
  "Can we hear what's said, by whom, and when?"
  Objective metrics possible. Test FIRST — if this fails, the local stack is blocked.
       │
Checkpoint 2: Clip Understanding (Phase 1 output)
  "Does the model correctly identify what's in each clip?"
  Semi-objective. Human rating on specific dimensions.
       │
Checkpoint 3: Editorial Decisions (Phase 2 storyboard)
  "Did the LLM follow constraints and make good narrative choices?"
  Partially objective (constraint satisfaction), partially subjective (narrative quality).
       │
Checkpoint 4: Final Video (rough cut)
  "Would you post this?"
  Fully subjective. A/B blind review. Only run when Checkpoints 1-3 pass.
```

**Progressive evaluation:** Only advance to the next checkpoint when the current one passes. This prevents wasting time building a full parallel pipeline before validating the foundation.

### Checkpoint 1: Transcription Quality

**What to compare:** Gemini transcription vs mlx-whisper + pyannote (local stack)

**Test corpus:** Select 3-4 clips with varied audio conditions:
- Clear single-speaker speech (easy baseline)
- Multiple speakers with overlapping dialogue (stress test for diarization)
- Background noise / outdoor ambient (stress test for word accuracy)
- Mixed languages or accented speech (if applicable to your footage)

Total test material: ~5 minutes across the clips. Small enough to manually annotate.

**Ground truth:** Manually transcribe each test clip with speaker labels and word-level timestamps. This is tedious (~30 min per minute of audio) but only needs to be done once. The ground truth becomes a reusable test fixture.

**Metrics:**

| Metric | What it measures | How to compute |
|--------|-----------------|----------------|
| **WER** (Word Error Rate) | Transcription accuracy | `jiwer` Python library against ground truth |
| **Speaker Attribution Accuracy** | Diarization quality | % of words assigned to the correct speaker |
| **Timestamp Deviation** | Temporal precision | Mean absolute error of segment start/end times vs ground truth (in seconds) |
| **Speaker Count Accuracy** | Can it detect how many speakers? | Correct count vs ground truth |
| **Boundary Precision** | Speaker turn detection | % of speaker turns detected within ±1 second |

**Pass criteria (proposed):**
- WER < 15% (Gemini baseline is ~5-10% on clear speech; local can be looser)
- Speaker attribution > 85% (critical for Phase 2 to know WHO said what)
- Timestamp deviation < 1.5 seconds per segment boundary
- If local stack fails on speaker attribution, it's a blocker — VX's editorial decisions depend on knowing who is speaking

**Important nuance:** Gemini transcription gets speaker NAMES from user context (briefing). The local stack only produces SPEAKER_0, SPEAKER_1. We'd need a post-processing step to match anonymous speakers to named people (using voice clustering + briefing data). This additional complexity is part of the evaluation — not just "does whisper work" but "does the full local transcription pipeline produce usable editorial input?"

### Checkpoint 2: Clip Understanding (Phase 1 Quality)

**What to compare:** Phase 1 output quality under two conditions:
- **Condition A:** Current pipeline (Gemini sees proxy video, no local metadata)
- **Condition B:** Current pipeline + local metadata injected (CLIP similarity scores, energy profile, content clusters)

**Test corpus:** Select 5-6 clips with varied content (action, dialogue, establishing, B-roll, mixed).

**Evaluation method:** Human preference scoring. For each clip, the filmmaker rates both Phase 1 reviews (blinded) on:

| Dimension | Question | Scale |
|-----------|----------|-------|
| **Segment Boundaries** | Did it find the right start/end points for usable segments? | 1-5 |
| **Completeness** | Did it catch all important moments? Any missed? | 1-5 |
| **People ID** | Did it correctly identify and describe people consistently? | 1-5 |
| **Actionability** | Could you build an edit from this review alone, without watching the clip? | 1-5 |
| **Redundancy Awareness** | (Condition B only) Did the similarity/cluster data help identify redundant content? | 1-5 |

**Pass criteria:** Condition B should score >= Condition A on average. If local metadata doesn't improve or slightly degrades Phase 1 quality, the metadata pipeline isn't worth the complexity.

**What this reveals:** Whether Gemini already captures everything from the video (making local metadata redundant) or whether the local metadata fills real gaps in Gemini's perception. If Gemini consistently misses duplicate content across clips but CLIP embeddings catch it, that's a clear win even if other dimensions are equal.

### Checkpoint 3: Editorial Decision Quality (Phase 2 Storyboard)

**What to compare:** Phase 2 storyboard quality under different pipeline configurations. This checkpoint tests both the local metadata AND the prompt engineering techniques from Part 2.

**Test scenarios:** Design 2-3 scenarios with explicit filmmaker constraints:
- Scenario A: "Must include the sunset. Must exclude the bus footage. Tone: contemplative. Duration: 5-8 min."
- Scenario B: "Focus on the food experiences. Upbeat. Keep it under 5 minutes."
- Scenario C: (Open-ended, minimal constraints) "Make the best vlog you can from this footage."

**Evaluation dimensions:**

| Dimension | Objective? | How to score |
|-----------|-----------|--------------|
| **Constraint satisfaction** | Yes | Binary per constraint: did the MUST-INCLUDE appear? Was the MUST-EXCLUDE absent? |
| **Segment selection** | Semi | For each segment, rate: "Is this the best available moment for this narrative slot?" (1-5) |
| **Narrative arc** | No | Does the segment sequence tell a coherent story with beginning/middle/end? (1-5) |
| **Timestamp precision** | Yes | Spot-check 5 segments: do in_sec/out_sec cut at natural boundaries (not mid-sentence, not mid-action)? |
| **Pacing** | No | Does the edit breathe? Or is it all the same energy level? (1-5) |
| **Redundancy** | Yes | Count segments with >80% visual similarity to another segment (should be 0) |

**Pass criteria:**
- Constraint satisfaction: 100% on MUST constraints (non-negotiable)
- Segment selection: average >= 3.5
- Narrative arc: >= 3.0
- Timestamp precision: >= 4 out of 5 spot-checks land on natural boundaries
- Redundancy: 0 near-duplicate segments

**Comparing pipeline variants at this checkpoint:**

| Variant | What changes |
|---------|-------------|
| Baseline | Current VX pipeline, no changes |
| +Prompt hardening | Part 2 techniques (constraint hierarchy, instruction anchoring, reasoning checkpoints) |
| +Local metadata | CLIP embeddings, energy scoring, similarity flags injected into Phase 1/2 |
| +Prompt + metadata | Both prompt hardening and local metadata |
| +Two-call decoupled | Split Phase 2 into reasoning + structuring (Part 2, Technique 3) |

Run each variant on the same test scenarios. Compare scores. This tells you which improvements actually move the needle.

### Checkpoint 4: Final Video (Rough Cut)

**When to run:** Only after Checkpoints 1-3 show clear improvements. This is expensive (time to watch) and subjective.

**Method:** A/B blind review. Generate two rough cuts from the same footage under different pipeline configurations. Watch both without knowing which is which.

**Scoring:**

| Question | Type |
|----------|------|
| Would you post this? | Yes/No |
| Overall quality | 1-10 |
| Pacing quality | 1-5 |
| Story coherence | 1-5 |
| Worst moment (timestamp + why) | Open text |
| Best moment (timestamp + why) | Open text |
| What would you change? | Open text |

The **"worst moment"** metric is the most diagnostic. It reveals where the pipeline fails, not just its average quality. A rough cut with one terrible segment is worse than one that's consistently mediocre — it breaks viewer trust.

### Building the Test Fixture

All checkpoints share one prerequisite: **a reusable test project.** This should be:

- A real project with 8-12 clips of varied content (not synthetic data)
- Manually annotated ground truth transcription for 3-4 clips
- Pre-written filmmaker constraints for 2-3 test scenarios
- Phase 1 reviews from the current pipeline (baseline to compare against)
- Stored as a fixture in the repo (clip metadata + ground truth, not the video files themselves)

This test fixture is the foundation for all evaluation. Building it is the first investment — ~2-4 hours of manual annotation — but it pays off across every experiment.

### Evaluation Sequence

```
Step 1: Build test fixture                        (~2-4 hours, one-time)
Step 2: Checkpoint 1 — transcription comparison    (~2-3 hours)
  → If local transcription fails: STOP, stay with Gemini
  → If local transcription passes: continue
Step 3: Checkpoint 2 — Phase 1 with local metadata (~4-6 hours)
  → If no improvement: local metadata has limited value, focus on Part 2 prompt techniques
  → If improvement: continue, integrate local metadata
Step 4: Checkpoint 3 — Phase 2 variant comparison  (~4-6 hours)
  → Test prompt hardening variants independently of local metadata
  → Test combined variants
  → Identify which changes actually improve constraint satisfaction and narrative quality
Step 5: Checkpoint 4 — rough cut A/B review        (~2-3 hours)
  → Only for the winning variant(s) from Step 4
  → Final validation before committing to a pipeline change
```

Total evaluation investment: ~15-22 hours across all steps, but spread over time and with early exit points.

---

## References

- "Let Me Speak Freely?" (ICLR 2025) — JSON-mode vs reasoning quality
- PARSE (EMNLP 2025, Amazon) — Schema description optimization
- Context Rot (Chroma, July 2025) — Accuracy degradation at scale
- Self-Refine (2023) — Iterative refinement preference gains
- "Lost in the Middle" (MIT 2025) — Position bias in transformers
- Gemini Prompting Strategies — Context-first, instruction-last ordering
- Anthropic Context Engineering — Effective context management for agents
- mazsola2k-ai-video-editor — Two-pass analysis architecture
- VX Prompt Engineering Cookbook — Existing guidelines and research
