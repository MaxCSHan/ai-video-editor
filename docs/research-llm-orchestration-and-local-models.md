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

### Realistic Local Model Candidates for M4 Pro

#### Tier 1: Already proven feasible

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **mlx-whisper** (small/medium) | Transcription | 1-3GB | ~10x real-time | Already in VX. Works well. |
| **CLIP ViT-B/32** (MLX) | Semantic scoring | ~350MB | Fast (batch) | Pre-filter frames before cloud API. No quality concern. |
| **SigLIP** (MLX) | Better CLIP | ~400MB | Fast (batch) | Google's improved CLIP. Better zero-shot accuracy. |

#### Tier 2: Worth testing, likely feasible

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **Qwen2.5-VL-3B** (4-bit MLX) | Frame captioning | ~2GB | ~15-25 tok/s | Smaller than mazsola2k's 7B. Keyword captioning at 40 tokens should be fast enough. |
| **SmolVLM-500M** (MLX) | Frame description | ~500MB | ~40+ tok/s | HuggingFace's tiny VLM. Very fast but less capable. |
| **MobileVLM v2** | Frame triage | ~1.5GB | ~30 tok/s | Designed for mobile. Good for binary "interesting/boring" classification. |
| **Florence-2** (MLX) | Object detection + captioning | ~500MB | Fast | Microsoft's efficient vision model. Good for structured extraction (bounding boxes, captions). |

#### Tier 3: Possible but risky on base configs

| Model | Task | Memory | Speed (M4 Pro) | Notes |
|-------|------|--------|-----------------|-------|
| **Qwen2.5-VL-7B** (4-bit MLX) | Full vision-language | ~5GB | ~8-12 tok/s | Usable on 36GB configs. Tight on 18GB alongside other processes. |
| **Llama 3.2 11B Vision** (4-bit) | Vision-language | ~7GB | ~6-10 tok/s | Apple Silicon MLX support exists. Memory-hungry. |

### Where Local Models Add Value in VX (Without Replacing Cloud)

The key insight from mazsola2k's two-pass design: **use local models as a pre-processing layer, not as the primary reasoning engine.** VX's strength is Gemini/Claude for deep editorial reasoning. Local models should handle tasks where:
1. The task is simple and well-defined (classification, not creativity)
2. Volume is high (per-frame, not per-clip)
3. Errors are tolerable (pre-filtering, not final decisions)
4. Privacy/cost matters (no API call needed)

#### Concrete opportunities:

**1. CLIP/SigLIP as a pre-filter before Phase 1**
- Score every frame against semantic prompts (like mazsola2k's 9-prompt approach)
- Use scores to identify boring/duplicate segments before sending to Gemini
- Potential savings: skip 30-50% of footage from Phase 1 analysis, reducing API cost and context size
- Implementation: run during preprocessing, store scores in clip metadata
- Risk: almost zero. CLIP is well-understood, fast, and deterministic.

**2. Local VLM for quick frame captions (pre-Phase 1 metadata)**
- Caption frames at 2-5 second intervals using Qwen2.5-VL-3B or SmolVLM
- Output: keyword tags per frame (like mazsola2k: "hands, blue paint, brush, detail work")
- Feed these as structured metadata into Phase 1 prompts, giving Gemini a head start
- Benefit: Gemini can spend its tokens on editorial judgment, not basic scene description
- Risk: moderate. Caption quality from 3B models can be inconsistent. Needs testing with actual trip footage (not just workshop/craft content).

**3. Local duplicate detection (ResNet or CLIP embeddings)**
- Compute frame embeddings locally during preprocessing
- Detect near-duplicate segments within and across clips
- Feed duplicate flags into Phase 2 so the LLM knows "segments X and Y show essentially the same thing"
- Implementation: cosine similarity on CLIP embeddings, threshold-based flagging
- Risk: low. This is a numerical comparison, not a generation task.

**4. Local scene energy scoring**
- Motion estimation from frame differencing (no ML needed, pure OpenCV)
- Audio energy from waveform RMS (no ML needed)
- Combined "energy score" per segment: high-motion + loud audio = action, low-motion + silence = establishing shot
- Feed into Phase 1/Phase 2 as metadata
- Risk: zero. These are deterministic signal processing operations.

### Recommendation

Start with **CLIP/SigLIP pre-filtering** (Tier 1, no risk) and **local energy scoring** (no ML needed). These provide immediate value — reducing context size and giving the LLM richer metadata — without any quality risk. Defer local VLM captioning until we can benchmark Qwen2.5-VL-3B on actual VX footage and confirm caption quality meets our threshold.

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

### Phase A: Quick Wins (Low effort, high impact)

1. **Constraint hierarchy in prompts** — Rewrite `format_context_for_prompt()` to separate MUST constraints from preferences. Add accountability clause. (~1 hour)

2. **Instruction anchoring** — Add constraint reminder to the final instruction block in `build_editorial_assembly_prompt()`. (~30 min)

3. **Structured reasoning checkpoint** — Update `editorial_reasoning` field description to require explicit constraint-check step. (~30 min)

4. **Temperature tuning** — Lower Phase 2 temperature to 0.5-0.6 when hard constraints are present. (~30 min)

### Phase B: Medium-Term (Moderate effort, significant impact)

5. **Few-shot example** — Write 1-2 examples demonstrating constraint satisfaction in editorial reasoning + segments. Add to Phase 2 prompt. (~2-3 hours, mostly writing)

6. **Context compression** — Add `editorial_priority` to Phase 1 output. Tier clip review detail in Phase 2 based on priority. (~4-6 hours)

7. **User context → clip resolution** — After Phase 1, fuzzy-match user constraints to specific clips. Append resolved references. (~4-6 hours)

8. **Validation call** — Post-Phase 2 constraint checker using Gemini Flash. Auto-flag violations. (~4-6 hours)

### Phase C: Longer-Term (Higher effort, highest quality ceiling)

9. **Two-call decoupled reasoning** — Split Phase 2 into freeform reasoning + structured output. (~8-12 hours, needs testing)

10. **CLIP/SigLIP pre-filtering** — Local semantic scoring during preprocessing. Feed as metadata into Phase 1. (~8-12 hours)

11. **Iterative refinement loop** — User feedback → revision call. Integrate with existing versioning. (~16-24 hours, UX design needed)

12. **Local energy scoring** — Motion + audio energy computed during preprocessing. Zero ML, pure signal processing. (~4-6 hours)

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
