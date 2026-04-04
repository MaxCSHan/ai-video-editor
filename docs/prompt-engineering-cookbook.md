# Prompt & Context Engineering Cookbook

Guidelines for all LLM/AI features in VX, based on 2025-2026 research and practitioner findings. Each principle includes the research source and how it applies to our pipeline.

---

## 1. Structured Output & Schema Design

### Field descriptions are inline instructions

**Principle**: Pydantic `Field(description=...)` values are passed directly to Gemini as schema-level instructions. Omitting them forces the model to guess semantics from field names alone.

**Research**: PARSE paper (EMNLP 2025, Amazon) — optimizing schema descriptions achieved 64.7% improvement in extraction accuracy and 92% error reduction on first retry.

**VX application**: Every model field used as a `response_schema` must have a `Field(description=...)` that encodes constraints, valid values, and editorial intent. This is especially critical for fields like `purpose`, `transition`, and `description` where the LLM's interpretation directly affects creative quality.

### Reasoning before answers

**Principle**: JSON-mode degrades reasoning by 10-15% because the model generates answer fields before reasoning through the problem. Adding a reasoning/thinking field *before* the answer fields prevents this.

**Research**: "Let Me Speak Freely?" (ICLR 2025) — JSON-mode significantly degrades reasoning. Mitigation: add `reasoning` string field as the first field in the schema. Result: 60% accuracy improvement on GSM8K benchmarks.

**VX application**: `EditorialStoryboard.editorial_reasoning` is the first field in the schema. The LLM writes its editorial thinking (story arc, hook selection, pacing decisions) before committing to segments. This single change has the highest impact on creative output quality.

**Alternative approaches**:
- Claude's Think Tool / Extended Thinking — reasoning happens internally, no schema field needed
- Two-step decoupled: freeform reasoning call, then a separate structuring call (2x cost)

### Keep schemas flat

**Principle**: Deeply nested structures increase grammar compilation time and degrade output quality. LLMs have trouble attending to deeply nested fields.

**Research**: Gemini docs explicitly warn about deeply nested schemas. OpenAI structured output docs recommend flat structures.

**VX application**: Our models are already reasonably flat. Resist the temptation to add nested sub-objects (e.g., don't nest `AudioStrategy` inside `Segment` — keep it as a string field with good description).

### Property ordering matters

**Principle**: Gemini 2.5+ preserves key ordering from the schema. If you want reasoning before decisions, order fields accordingly.

**VX application**: In `EditorialStoryboard`, `editorial_reasoning` comes first, then metadata (`title`, `style`), then creative decisions (`story_arc`, `segments`). The LLM processes fields in this order.

---

## 2. Context Window Management

### Context rot is real

**Principle**: Every model gets worse as input grows. 20-50% accuracy drops from 10K to 100K tokens. This is structural to transformers, not a fixable bug.

**Research**: Chroma (July 2025) tested 18 frontier models (GPT-4.1, Claude 4, Gemini 2.5): accuracy degrades significantly at 50K tokens even with 200K windows. Surprising finding: shuffled documents performed better than logically coherent ones — structural coherence makes irrelevant content look more relevant.

**VX application**: 
- Flatten clip reviews into compact plain text instead of verbose JSON (~60-70% token savings per clip)
- Inline transcripts with their clip reviews instead of a separate section (reduces cross-referencing distance)
- Trim redundant instructions once constraints are encoded in Field descriptions
- For 20+ clip projects, consider summarizing low-priority clips

### "Lost in the middle" persists

**Principle**: Beginning and end of the context window get more attention. Information buried in the middle is more likely to be missed.

**Research**: MIT (2025) — causal masking means beginning tokens accumulate more attention weight. No production model has eliminated position bias.

**VX application**:
- Most important context (user intent, style guidelines) goes at the top of the Phase 2 prompt
- Clip reviews are ordered chronologically (most important for narrative coherence)
- Final instruction ("Now produce the EditorialStoryboard") goes at the very end
- The current section order: role/rules -> user context -> style -> timeline -> clip reviews -> visual reference -> final instruction

### Instruction length sweet spot

**Principle**: LLM reasoning degrades around 3,000 tokens of instructions. Sweet spot: 150-300 words of instructions.

**Research**: Multiple practitioner findings. Gemini official guidance: context first, instructions/questions at the very end.

**VX application**: Keep the base `EDITORIAL_ASSEMBLY_PROMPT` template concise. Move constraints into Field descriptions rather than repeating them in prompt text. Let the data (clip reviews, transcripts) be the bulk of the prompt.

---

## 3. Temperature & Generation Settings

### Creative vs factual tasks

**Principle**: Temperature 0.2 is appropriate for factual extraction (Phase 1 clip review). Creative editorial decisions need 0.8-1.0 for variety and genuine creative choices.

**Research**: Gemini 3 docs recommend temperature 1.0 (below causes looping/degradation). General research: 0.8-1.0 for creative tasks.

**VX application**:
- Phase 1 (clip review): `temperature=0.2` — factual analysis of what's in the clip
- Phase 2 (editorial assembly): `temperature=0.8` — creative decisions about story, pacing, segment selection
- Transcription: `temperature=0.0` or lowest available — precision matters most
- Briefing quick scan: `temperature=0.2` — factual summary

---

## 4. Prompt Structure

### Section ordering

**Principle**: For Gemini, put context/data first and instructions/questions at the end. The LLM should understand the full picture before being asked to produce output.

**Research**: Gemini official prompting guide. Anthropic context engineering blog.

**VX Phase 2 section order**:
1. Role + editorial thinking bullets + rules (brief)
2. User context (filmmaker's intent, people, preferences)
3. Style supplement (preset-specific guidelines)
4. Filming timeline (chronological order with datetime)
5. Clip reviews with inline transcripts (the bulk of the data)
6. Transcript usage guidance
7. Visual reference (Gemini only — video bundles)
8. Final instruction ("Now produce the EditorialStoryboard...")

### Conditional sections

**Principle**: Only include sections that have content. Empty sections add noise and waste tokens.

**VX application**: All optional sections (filming_timeline, transcripts, visual_timeline, style_supplement, user_context) are conditionally appended. Use `if value:` guards.

### Reduce redundancy between prompt and schema

**Principle**: If a constraint is encoded in a Field description, don't repeat it verbatim in the prompt text. The LLM sees both — doubling up wastes tokens and can cause confusion if they diverge.

**VX application**: After adding Field descriptions to models, the CRITICAL RULES section was trimmed from 7 lines to 4, keeping only rules that can't be expressed in schema descriptions (e.g., "be thorough", "chronological order of output").

---

## 5. Creative Task Prompting

### Anti-pattern lists

**Principle**: Explicitly forbid common LLM failures in creative output. "Avoid corporate jargon. Self-evaluate: does this sound human or templated?"

**Research**: Phil Schmid (Gemini 3 prompt practices). Multiple practitioner findings.

**VX application**: For Phase 2, consider adding anti-patterns: "Avoid generic descriptions like 'beautiful scenery' or 'exciting moment'. Be specific about what makes each segment compelling."

### Self-evaluation loops

**Principle**: Ask the LLM to rate its output against explicit quality criteria. If below threshold, ask it to improve.

**Research**: Self-Refine paper — iteratively refined outputs preferred by humans ~20% more than single-shot. Gemini Flash 2.0 gained 32 percentage points with iteration.

**VX application**: A lightweight validation pass (separate cheap LLM call to check coverage, timestamp bounds, narrative arc) is more cost-effective than full iterative refinement. This is a good follow-up improvement.

### Two-stage reasoning (future consideration)

**Principle**: Freeform reasoning in step 1, structuring in step 2. "When LLMs are asked to both reason and produce structured output simultaneously, performance often degrades."

**Research**: "Prompt-Driven Agentic Video Editing System" (ACM 2025). "Let Me Speak Freely?" (ICLR 2025).

**VX application**: Our `editorial_reasoning` field is a single-call approximation of this. If quality is still insufficient, consider a two-call approach: first generate a freeform editorial plan (prose), then convert it to structured JSON in a second call. 2x cost but preserves full reasoning quality.

---

## 6. Few-Shot Examples

### Always include examples for complex output

**Principle**: "We recommend to always include few-shot examples. Prompts without few-shot examples are likely to be less effective."

**Research**: Google Gemini official prompting guide. Anthropic: "examples are the 'pictures' worth a thousand words."

**VX application**: For Phase 2, a condensed example of a well-crafted 3-5 segment storyboard excerpt would demonstrate editorial voice and decision-making altitude. Token cost: ~500-800 tokens. Worth it if it prevents even one retry. This is a high-priority follow-up improvement.

**Guidelines for writing examples**:
- 1-3 diverse examples including edge cases
- For Claude, wrap in `<example>` tags
- Show the thinking process in editorial_reasoning, not just the output
- Include one example with B-roll interleaving and one with dialogue-driven segments

---

## 7. Provider-Specific Notes

### Gemini

- **Structured output**: Use `response_schema=PydanticModel` directly. Gemini SDK handles conversion.
- **Video understanding**: 1 FPS sampling — fast action loses detail. Text prompt should come after video in contents array. Use context caching for videos >10 min.
- **Temperature**: Must stay at 1.0 for Gemini 3 (below causes looping). For Gemini 2.5 Flash, 0.8 is fine.
- **Property ordering**: Preserved from schema — put reasoning fields first.
- **Enums**: Use Literal types or enums for fields with limited valid values. Gemini respects these strictly.
- **Don't mix XML and Markdown** in Gemini prompts.
- **`media_resolution`**: Gemini 3 parameter for per-frame token allocation. Consider using for higher fidelity visual analysis.

### Claude

- **Structured output**: No native response_schema. Use JSON parsing with fallback (code fence stripping, brace extraction).
- **XML tags**: Claude is specifically tuned for XML. Performance can vary up to 40% based on format alone. For Claude-specific prompts, consider `<clip_review>`, `<transcript>` tags.
- **Extended Thinking**: Claude's think tool achieved 54% relative improvement over baseline. Consider enabling for Phase 2 Claude path.
- **Max tokens**: Set generously for Phase 2 (8192+) — structured output with 20+ segments is verbose.

---

## 8. VX Pipeline Stage Reference

| Stage | Task Type | Temperature | Key Principle |
|-------|-----------|-------------|---------------|
| Briefing (quick scan) | Factual summary | 0.2 | Concise, accurate observation |
| Transcription | Precision extraction | 0.0-0.2 | Timestamps and speaker ID accuracy |
| Phase 1 (clip review) | Factual analysis | 0.2 | Per-clip, structured observation |
| Phase 2 (editorial) | Creative assembly | 0.8 | Reasoning-first, chronological coherence |
| Phase 3 (monologue) | Creative writing | 0.8-1.0 | Voice, tone, pacing |

---

## References

- PARSE: Schema description optimization (EMNLP 2025, Amazon) — https://arxiv.org/abs/2510.08623
- "Let Me Speak Freely?": JSON-mode vs reasoning (ICLR 2025) — https://arxiv.org/abs/2408.02442
- Context Rot: Chroma research (July 2025) — https://research.trychroma.com/context-rot
- Anthropic Context Engineering — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Claude Think Tool — https://www.anthropic.com/engineering/claude-think-tool
- Gemini Structured Output — https://ai.google.dev/gemini-api/docs/structured-output
- Gemini Prompting Strategies — https://ai.google.dev/gemini-api/docs/prompting-strategies
- Gemini 3 Prompt Practices (Phil Schmid) — https://www.philschmid.de/gemini-3-prompt-practices
- Self-Refine — https://arxiv.org/abs/2303.17651
- Prompt-Driven Agentic Video Editing (ACM 2025) — https://arxiv.org/abs/2509.16811
