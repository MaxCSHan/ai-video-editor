# Reviewing LLM Agent Traces

How to debug Editorial Director (and other LLM agent) runs using Phoenix tracing and `traces.jsonl`.

## Setup

Phoenix auto-instruments all `google.genai` calls via OpenTelemetry when a Phoenix server is running:

```bash
vx trace              # start Phoenix on localhost:6006
vx analyze my-trip    # traces are captured automatically
```

Connection happens in `tracing.py:connect_phoenix()` — called at pipeline startup if Phoenix is reachable.

## Two trace sources

| Source | What it captures | Where |
|--------|-----------------|-------|
| `traces.jsonl` | Per-call token counts, cost, duration, phase label | `library/<project>/traces.jsonl` |
| Phoenix UI | Full request/response content, tool calls, function responses, message history | `http://localhost:6006` |

`traces.jsonl` tells you **how much** each call cost. Phoenix tells you **what the model saw and said**.

## Phoenix UI: finding director traces

1. Open `http://localhost:6006`
2. Select project **vx-pipeline**
3. Filter spans by name: `GenerateContent`
4. Sort by time (newest first)
5. Director review spans appear in clusters of 5-15 calls within ~1 minute, all with the same model (e.g., `gemini-2.5-flash`)

Each `GenerateContent` span is one agent turn. Phoenix creates one trace per API call (not one trace per review session), so director turns appear as separate traces.

## What to check in each span

### Input tab — what the model received

- **First turn**: Should contain the full initial prompt with:
  - Storyboard summary (title, segments, duration)
  - Contact strip image (inline_data with JPEG bytes)
  - Eval scores (constraints, timestamps, speech-safe, structure, coverage)
  - Segment list
  - Budget info
  - System instruction with review rubric
  - Tool declarations (function_declarations array)

- **Subsequent turns**: Should contain:
  - Previous assistant response
  - Function response(s) with tool results
  - Any inline images from screenshot_segment

**Red flag**: If user messages say `[previous tool results cleared — use tools to re-fetch if needed]`, micro_compact fired and the model lost context from that turn's tool results.

### Output tab — what the model responded

Look at `candidates[0].content.parts`:

- `text` parts: The model's reasoning (shown as "thought" in the UI when `thought_signature` is present)
- `functionCall` parts: Actual tool invocations

**Red flag**: If the model writes "I will screenshot segment X" in text but there's no `functionCall` part, the model is narrating instead of acting. This means tools may not be configured correctly.

**Red flag**: If `finish_reason` is `STOP` but `content.parts` is empty or has no `functionCall`, the model decided to stop without calling `finalize_review`.

### Usage metadata

```
prompt_token_count: 5219
prompt_tokens_details:
  TEXT: 4961
  IMAGE: 258       # 258 = one thumbnail, 4644 = contact strip (21 segments)
thoughts_token_count: 1144
```

- If IMAGE tokens are 0 on the first turn, the contact strip failed to generate
- If IMAGE is 258, only one thumbnail is present (not the full contact strip)
- `thoughts_token_count > 0` means the model used internal reasoning (Gemini 2.5 Flash thinking)

## Diagnosing common issues

### Model narrates but doesn't call tools

**Symptom**: Every turn has text like "I will apply fixes" but no `functionCall` parts.

**Cause**: Usually means the tools declaration is malformed, or the model's function calling mode isn't enabled. Check that `tools` appears in the input with valid `function_declarations`.

### Model hallucinates tool results

**Symptom**: Model says "The screenshots clearly show X" but the previous user message is `[previous tool results cleared]`.

**Cause**: `micro_compact` cleared the tool results before the model could reference them. Increase `keep_recent_turns` or check the compaction logic.

### "No constraints to check" from eval

**Symptom**: `run_eval_check("constraint_satisfaction")` returns empty results.

**Cause**: `user_context` not passed to the tool context. The constraint checker needs `highlights` and `avoid` fields from user_context.

### All fixes reverted by regression guard

**Symptom**: Tool results all say "Fix reverted: Scores regressed: timestamp_precision: 1.00 → 0.95". The model keeps trying to fix speech cuts but every fix gets rejected.

**Cause**: Competing eval dimensions — extending `out_sec` to capture a full sentence pushes timestamps outside Phase 1 usable segment bounds, dropping `timestamp_precision`. But the speech cut fix is correct. The regression guard now uses weighted net score so speech_cut_safety improvements can offset small timestamp_precision drops.

**What to look for**: Function responses containing "Fix reverted" in the Phoenix user messages. If multiple consecutive fixes are reverted, the regression weights may need tuning.

### Empty response on final turn

**Symptom**: Last span has 0 output tokens, `finish_reason: STOP`, empty parts.

**Cause**: Model was done but didn't call `finalize_review`. Common when the model runs out of things to do. The harness handles this via `convergence_reason = "no_response"`. Not a bug per se, but indicates the model should have finalized on the previous turn.

### Token count too low on first turn

Expected first-turn input tokens for a 20-segment storyboard:
- System prompt + tools: ~2,000 tokens
- Storyboard text + eval scores: ~2,000 tokens
- Contact strip (20 segments): ~5,200 tokens (20 x 258)
- **Total**: ~9,000+ tokens

If you see only 3,000-4,000 on the first turn, the contact strip image likely failed to generate (ffmpeg error) or wasn't included.

## Quick checks via traces.jsonl

```bash
# Show all director turns for a project
grep editorial_review library/<project>/traces.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    t = json.loads(line)
    print(f'in={t[\"input_tokens\"]:>5} out={t[\"output_tokens\"]:>4} cost=\${t[\"estimated_cost_usd\"]:.4f} dur={t[\"duration_sec\"]:.1f}s')
"
```

What to look for:
- **Flat input token growth**: Tokens should grow ~500-1000/turn as context accumulates. If flat, micro_compact is too aggressive.
- **0 output tokens**: Model returned empty — review ended abnormally.
- **Very high input tokens**: Context window may be filling up, consider more aggressive compaction for older turns.

## Quick checks via Phoenix GraphQL

```bash
# Get a span's output (tool calls + text)
ID=$(echo -n "Span:82" | base64)
curl -s -X POST "http://localhost:6006/graphql" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"query { node(id: \\\"$ID\\\") { ... on Span { output { value } } } }\"}"
```

The output JSON contains the full Gemini response including `candidates[0].content.parts` with all `functionCall` and `text` entries.

## Phoenix known issues

- **GraphQL introspection error**: `Cannot convert value to AST: {}` — this is a `graphql-core` bug with Phoenix's schema. Doesn't affect span queries, only introspection.
- **One trace per API call**: Phoenix auto-instrumentation creates separate traces for each `GenerateContent` call, not one trace per agent session. Use timestamps to group director turns.
