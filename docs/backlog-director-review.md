# Editorial Director & Chat: Architectural Review Backlog

Review date: 2026-04-09. Applied the five design principles from the Timeline Mode refactoring (over-deterministic, bad context management, overloaded invoke, no feedback loop, no HITL).

**Overall assessment**: The system is well-engineered tactically (tool implementations, error handling, session management) but over-constrained strategically (rigid quality bar, no LLM agency, weak feedback loops). The Chat mode's propose-approve-execute pattern is a good HITL foundation to build on.

---

## 1. Over-Deterministic

### 1.1 Coverage threshold hard-coded at 60%
- **Files**: `director_prompts.py:33, 58, 121`
- **Severity**: Medium
- **Problem**: The director is told to "expand if below 60% of clips used or explicitly discarded." This is a fixed number regardless of project type — a 5-minute highlight reel from 50 clips might correctly use only 20%.
- **Fix**: Make coverage threshold part of `ReviewConfig` or derive from the Creative Brief (short highlights → lower coverage is fine; comprehensive recap → higher coverage expected).

### 1.2 Quality bar is absolute — LLM can't negotiate trade-offs
- **Files**: `director_prompts.py:52-60`
- **Severity**: Medium
- **Problem**: "Constraints: 100% must be satisfied (hard requirement)." If a constraint is genuinely unsatisfiable (footage doesn't exist), the LLM has no escape valve — it can only keep trying.
- **Fix**: Reframe as "prioritized guidelines" with an explicit "explain why not" path. The constraint_satisfaction field exists but the prompt doesn't acknowledge it as valid.

### 1.3 Regression weights are constants
- **Files**: `director_tools.py:407-415`
- **Severity**: Low
- **Problem**: Fixed weights (constraint_satisfaction: 0.30, speech_cut_safety: 0.25, etc.) and a hard 10% max-dimension-drop cap. A narrative project might tolerate coverage drops for better speech safety.
- **Fix**: Move to `ReviewConfig` so presets or the brief can adjust them per project.

---

## 2. Bad Context Management

### 2.1 ~~Initial message dumps all clips~~ — INTENTIONAL
- **Files**: `director_prompts.py:199-240` (`build_initial_message`)
- **Severity**: N/A — by design
- **Rationale**: The director agent needs visibility into ALL available footage to make informed decisions about adding, swapping, or expanding coverage. Filtering clips would limit the agent's flexibility — it can't suggest "add this unused high-value moment from clip X" if it doesn't know clip X exists. The token cost is acceptable given the decision quality improvement.

### 2.2 Chat session rebuild truncates tool results to 100 chars
- **Files**: `editorial_director.py:1335` (`_rebuild_messages_from_session`)
- **Severity**: Medium
- **Problem**: `tr.get('result', '')[:100]` — when resuming a chat session, prior tool results are truncated to 100 characters. The LLM loses information about what it previously inspected, leading to redundant tool calls.
- **Fix**: Summarize prior tool results into key facts (e.g., "Segment 3: speech cut at 45.2s, speaker was mid-sentence") rather than truncating raw output.

### 2.3 Micro-compact discards context without summarization
- **Files**: `editorial_director.py:149-187` (`_micro_compact`)
- **Severity**: Medium
- **Problem**: Old tool results are replaced with `"[previous tool results cleared — use tools to re-fetch if needed]"`. This forces the LLM to re-inspect segments it already examined if the context window fills up.
- **Fix**: Before clearing, generate a one-line summary per cleared message (e.g., "Inspected segments 3-7, found speech cut issue in seg 5, fixed by adjusting out_sec"). Use an LLM call or heuristic.

### 2.4 Contact strip always generated regardless of project size
- **Files**: `editorial_director.py:246-247`, `render.py:194-231`
- **Severity**: Low
- **Problem**: For a 100-segment project, the contact strip is a wide image with tiny frames. For a 3-segment highlight, it may not add value. Always generated, always included.
- **Fix**: Make contact strip generation configurable. Skip for very small (<5 segments) or very large (>50 segments) projects where it's not useful.

---

## 3. Overloaded Single Invoke

### 3.1 System prompt encodes full 5-phase workflow
- **Files**: `director_prompts.py:13-66` (`DIRECTOR_SYSTEM_PROMPT`)
- **Severity**: Medium
- **Problem**: ~60 lines instruct the LLM to follow a 5-step process: OVERVIEW → INSPECT → EXPAND → EDIT → FINALIZE. If the LLM deviates (e.g., edits before inspecting), there's no recovery. The process is implicit in the prompt, not enforced by the harness.
- **Fix**: Either enforce phases via the harness (state machine that restricts available tools per phase) or simplify the prompt to "review and improve the storyboard" with the process as a soft suggestion.

### 3.2 Six granular inspect tools without batch capability
- **Files**: `director_prompts.py:320-510`
- **Severity**: Low
- **Problem**: `screenshot_segment`, `get_transcript_excerpt`, `get_full_transcript`, `get_clip_review`, `run_eval_check`, `get_unused_footage` — each inspects one thing per turn. If the LLM wants to check 5 segments, it needs 5 turns minimum.
- **Fix**: Add `inspect_segments(segment_indices: list[int])` batch tool that returns a summary of multiple segments in one turn, reducing turn count and cost.

---

## 4. No Feedback Loop

### 4.1 Regression reverts don't explain root cause
- **Files**: `director_tools.py:418-438` (`_check_regression`)
- **Severity**: Medium
- **Problem**: When an edit is reverted, the LLM gets `"Reverted: regression detected"` but not which metric dropped or by how much. The LLM can't adapt its strategy.
- **Fix**: Return diagnostic detail: `"Reverted: speech_cut_safety dropped 95%→80% (15pts, exceeds 10% limit). Segment 3's edit cut mid-sentence at 45.2s. Try a different out_sec."` Include the specific metric, the delta, and a hint about what went wrong.

### 4.2 No auto-eval after successful edits
- **Files**: `editorial_director.py:405-494`
- **Severity**: Medium
- **Problem**: After a successful edit, the LLM doesn't automatically see the score impact. It must voluntarily call `run_eval_check()`. Most turns, it doesn't.
- **Fix**: After each successful `edit_timeline` call, auto-inject a score delta summary into the tool response: `"Edit applied. Score impact: constraint_satisfaction 85%→90% (+5%), coverage 45%→48% (+3%)."` This closes the feedback loop without requiring an extra LLM turn.

### 4.3 Finalization verdict not re-validated
- **Files**: `director_tools.py:795-804` (`finalize_review`)
- **Severity**: Medium
- **Problem**: The LLM calls `finalize_review(passed=True, summary=...)` and the review ends. No validation that the storyboard actually passes eval thresholds. The agent's word is final.
- **Fix**: Before accepting finalization, run `score_storyboard()` and check the hard gate (constraint satisfaction = 1.0) and soft gate (weighted average >= 0.70). If failed, return "Cannot finalize: constraint_satisfaction is 0.85, must be 1.0" and let the LLM try again.

### 4.4 No edit history or pattern recognition
- **Files**: Overall agent loop
- **Severity**: Low
- **Problem**: The LLM doesn't know how many edits it's attempted, how many were reverted, or what patterns led to reversions. It can repeat the same mistake.
- **Fix**: Inject periodic summaries: "Edits so far: 5 applied, 2 reverted. Reversion pattern: both reverts were on segments with dialogue — check speech boundaries before editing."

---

## 5. No HITL (Human in the Loop)

### 5.1 Auto-review is fully autonomous
- **Files**: `editorial_director.py:190-549` (`run_editorial_review`)
- **Severity**: **High**
- **Problem**: The autonomous review loop makes all decisions without human input. If the agent makes a poor judgment call (e.g., removes a key moment to improve coverage metrics), there's no intervention point.
- **Fix**: Add optional mid-review checkpoints. After N edits (configurable), pause and show: "I've made 3 changes: [list]. Continue?" This is especially important for constraint-affecting edits.

### 5.2 Chat proposals not diffed
- **Files**: `editorial_director.py:807-867`
- **Severity**: Medium
- **Problem**: The agent describes proposed edits in prose, but the user doesn't see a structured before/after comparison. Hard to evaluate "I'll adjust segment 3's out_sec" without seeing current vs proposed values.
- **Fix**: Include a structured diff in the proposal output:
  ```
  Segment 3: out_sec 45.0 → 42.5 (trim 2.5s)
  Segment 7: NEW — add IMG_9815 [3.0-8.0s] as context
  ```

### 5.3 Batch approval is all-or-nothing
- **Files**: `editorial_director.py:752-801`
- **Severity**: Medium
- **Problem**: When the agent proposes multiple edits via `propose_edits`, the user must approve or reject the entire batch. Can't accept 7 good edits and reject 3 bad ones.
- **Fix**: Allow granular approval: display each edit numbered, let user specify which to apply (e.g., "apply 1,3,5-7").

### 5.4 No approval gate before finalization
- **Files**: Both auto-review and chat
- **Severity**: **High**
- **Problem**: `finalize_review(passed=True)` ends the review immediately. The user never sees the final storyboard state or eval scores before acceptance.
- **Fix**: Before finalizing, show a summary: "Final storyboard: 26 segments, 2:15. Constraint satisfaction: 100%. Coverage: 68%. Approve? [Y/n]"

### 5.5 Chat mode inflates budgets silently
- **Files**: `editorial_director.py:641-644`
- **Severity**: Medium
- **Problem**: Chat mode overrides budgets to `max_turns=200, max_fixes=100, max_cost=$2.00` without telling the user. A filmmaker could spend $2+ per session without realizing.
- **Fix**: Show budget at session start: "Budget: up to 200 turns, $2.00 max. Current spend: $0.00." Show running cost after each turn.

---

## Implementation Priority

| Priority | Items | Rationale |
|----------|-------|-----------|
| **P0 (done)** | Timeout on ffmpeg subprocess calls | Fixes the freeze bug |
| **P1** | 4.1 Diagnostic regression messages | Cheap fix, big quality improvement — LLM learns from failures |
| **P1** | 4.2 Auto-eval feedback after edits | Closes the feedback loop without extra LLM turns |
| **P1** | 4.3 Finalization validation gate | Prevents premature finalization |
| **P2** | 5.1 Mid-review HITL checkpoints | Major quality gate for autonomous mode |
| **P2** | 5.4 Pre-finalization approval | Human sees result before acceptance |
| **P2** | 2.1 Trim initial context | Token savings + relevance improvement |
| **P3** | 1.1 Configurable coverage threshold | Per-project tuning |
| **P3** | 2.2 Session rebuild summarization | Better chat resume quality |
| **P3** | 3.1 Phase-aware harness | Structural improvement to agent loop |
| **P3** | 5.2 Proposal diffing | Better chat HITL experience |
| **P3** | 5.3 Granular batch approval | Better chat HITL experience |
