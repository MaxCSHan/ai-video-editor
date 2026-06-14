# VX — Agent-Director Architecture

> Status: v0.1 design, for evaluation. The **intelligence layer**: how VX's AI
> director gets the right context, makes editorial decisions a human reacts to,
> supports built-in **and user-authored** styles, and what the agent-native UI
> should be. Companion to [`COMPOSITION-ARCHITECTURE.md`](./COMPOSITION-ARCHITECTURE.md)
> (the rendering layer). Fact-checked against 2026 agent-engineering practice (§7),
> with an adversarial pass to separate established practice from hype.
>
> **Honest baseline:** an Editorial Director agent already exists and works
> (`editorial_director.py`, `director_tools.py`), but it is **opt-in/experimental**
> (`ReviewConfig.enabled=False`, invoked via `--review`/`chat`) and **eager-loads
> all clip reviews + transcripts into one context** — it is *not* yet
> context-engineered for 100–200 clips. This doc designs that next step and is
> explicit about what's reuse vs. new work (§8).

---

## 1. Framing: VX is an agent-native director

VX's methodology is: **ingest footage → build context → an AI director composes a
storyline → the human reacts → the director refines.** The product is the
*judgment*, not the cutting. So the architecture's job is to make a tool-using
agent (a) hold the **right context** about both the footage and the director's
intent, (b) **show its current result** so the human reacts fast, and (c) make
**better decisions** under that feedback.

The agent and the human look at **one shared, instantly-updating cut** — the live
`AVMutableComposition` from the composition doc. That shared surface is the whole
point: "see the current result efficiently" is not a separate feature, it's the
preview engine. This doc is the intelligence on top of it.

What exists today (reuse): a Gemini ReAct agent with INSPECT / EDIT / CONTROL
tools, regression-gated edits, a propose→approve→apply flow, the tiered Creative
Brief, the Phase-2 reason→structure→assemble split, an eval module, and one
hardcoded style preset. What's missing (new work): context engineering at clip
scale, persistent director-decision memory, and user-authored styles (§8).

---

## 2. The core problem: context engineering at clip scale

A 9-day trip is 160+ clips, each with a Phase-1 review + transcript. The current
director builds `clip_ids` and loads **every** review and **every** transcript into
one window (`editorial_director.py:227-228`). Per 2026 research this is the central
risk: **context rot** — model quality degrades *well before* the window limit, and
mid-context items get silently under-weighted (Chroma, Jul 2025; "Lost in the
Middle", 2023). Bigger window ≠ usable capacity.

The design follows Anthropic's context-engineering guidance (Sept 2025): **treat
context as a finite, curated resource — the smallest set of high-signal tokens.**

**(a) A durable Clip Index artifact.** Build, once, a compact structured index of
every clip: id, one-line summary, people, usable-segment ranges, has-speech, key
moments, quality flags — derived from the existing `ClipReview`s. This is the
agent's lightweight map; it is *not* the full reviews/transcripts. Persist it as a
versioned artifact (read back at session start), per the "externalize state"
pattern (Anthropic long-running-agents harness, Nov 2025).

**(b) Just-in-time hydration.** The agent holds lightweight identifiers and pulls
full data on demand via tools — which VX's INSPECT tools *already are*:
`get_clip_review`, `get_transcript_excerpt`, `get_full_transcript`,
`screenshot_segment` (2×2 keyframe grid), `get_unused_footage`. So the director
reasons over the index, then hydrates only the few clips it's deciding on. This is
exactly the recommended just-in-time/agentic-retrieval model.

**(c) Compaction.** When a chat/review trace nears the window, summarize-and-
reinitialize (the agent already does message-history micro-compaction,
`keep_recent_turns=5`); formalize it as "maximize recall, then precision."

**(d) Per-section work at scale.** Group clips by day/scene (the Divide-&-Conquer
direction) and let the director work a section at a time so each decision sees a
focused slice — not all 160 clips. (Sub-agent option in §4.)

> **Reuse note:** VX's Phase-2 split — 2A freeform reasoning (high temp) → 2A.5
> structuring into `StoryPlan` → 2B precise assembly — already implements the
> strongly-recommended **"separate free reasoning from structured commitment"**
> pattern (forcing reasoning + strict JSON in one constrained decode degrades
> reasoning ~10–30%). Keep it; it's ahead of the curve.

---

## 3. Two context streams: footage + director decisions

The agent needs both, and the second one is the gap.

**Footage context (exists, needs compaction).** Phase-1 `ClipReview`s,
`Transcript`s, `usable_segments`, scenes, and the briefing quick-scan are the
source of truth. Fold them into the Clip Index (§2) and hydrate on demand.

**Director-decision context (NEW — the missing memory).** The director's intent
must persist and steer subsequent turns, not evaporate after one call. Two sources:
- The tiered **Creative Brief** (`models.py:CreativeBrief`,
  `briefing.py`) — `constraints > direction > preferences`. Already steers prompts;
  keep as the standing intent.
- **Interactive decisions** — React Mode KEEP/CUT/TELL-IT, manual trims, accepted/
  rejected proposals. Today these are *not* remembered structurally.

**New work: a persistent, structured director-decision memory** (a "ledger") held
outside the window and read back each turn — the externalized-state / structured-
note-taking pattern. It records: used-clip ledger, **rejected-with-reason** (so the
agent stops re-proposing a cut the director already vetoed), open narrative gaps,
and standing preferences learned from decisions. This is what turns VX from
one-shot composition into a director that *learns the editor's taste within a
project* — and it feeds user-authored styles (§5).

---

## 4. The director agent loop

Reuse the Editorial Director ReAct agent (`run_director_chat` for collaboration,
`run_editorial_review` for autonomous passes) and its tool set, applying 2026
practice:

- **Match the loop to the task; don't default to an agent.** A bounded ask ("tighten
  the open") can be one structured call; only open-ended direction needs the full
  loop. (Google Cloud agentic-design guidance; Anthropic "minimal harness".)
- **Propose → human-gate → apply (HITL on the blast radius).** The existing
  `propose_edits` / `execute_proposal_batch` with `pending_proposal` is exactly the
  2026 interrupt→approve/edit/reject→resume pattern (LangGraph). Render the proposal
  as a **ghost diff** on the live cut; **the human is the quality gate.** Keep the
  mandatory "do it anyway" so intent always wins.
- **Few well-scoped tools.** The INSPECT/EDIT/CONTROL set is already well-shaped
  (Anthropic "writing tools for agents"). Gaps to add: keyword transcript search
  ("find where X says Y"), a **cost/token-delta preview** on `propose_edits` before
  the user approves, and **undo/rollback** of an applied batch.
- **Evals as the steering signal — scoped honestly.** Real today: **in-loop
  per-edit regression gating** in autonomous review (`_compute_eval_scores`
  @`director_tools.py:375`, `_REGRESSION_WEIGHTS` @407 — revert an edit if a
  weighted dimension drops >~10%) and hard-constraint checks. `score_storyboard`
  (`eval.py`) is otherwise **report-only** (`cmd_eval`), *not* a CI/threshold gate.
  The next step is to compute a **projected eval delta** (speech-cut safety,
  constraint satisfaction) and show it *in the ghost diff before apply* — which
  needs a pure **dry-run scorer** (apply to a `model_copy`, score, discard), since
  the current batch only evals after applying. (Online-eval-as-steering is an
  emerging, largely vendor-led 2026 practice — adopt the principle, not the hype.)
- **Keep loops short; reliability compounds.** Per-step reliability multiplies
  (0.85⁸ ≈ 27%), so long autonomous loops fail. Prefer **batched** intents:
  collect a stream of React decisions and compile them into **one** director turn
  (one `propose_edits`), not turn-by-turn — fewer steps, lower compounding risk,
  latency amortized.
- **Sub-agents only where they help.** For Phase-2 at scale, an orchestrator can
  fan out **3–5 per-section director sub-agents** with isolated context (Anthropic
  orchestrator-worker). But the **final assembly must stay single-threaded**
  (Cognition: multi-agent hurts when work is tightly coupled / shares context), and
  sub-agents must receive **full traces, not terse task briefs**. Budget for the
  **~15× token multiplier** of multi-agent vs chat — gate it behind project size,
  don't make it the default.

---

## 5. Director styles — built-in + user-authored

Today: one hardcoded `SILENT_VLOG` preset; a preset = `phase1_supplement`,
`phase2_supplement`, `has_phase3`, injected into the review/assembly prompts.
There is **no user authoring** — the `__custom__` option only yields a freeform
string that feeds the briefing `user_context`, not the phase supplements
(`style_presets.py`).

**Design: a `StyleProfile` as a first-class, versioned artifact** that compiles
into the *existing* supplement-consuming prompts. A profile carries: brief
supplements (the constraints/direction it adds), pacing & energy curve, transition
vocabulary, Phase-3 (monologue/overlay) behavior, and example references. Built-in
presets (Silent Vlog, etc.) become `StyleProfile`s; users author their own.

**Authoring paths** (from 2026 personalization patterns):
- **From a description** — the user describes the style; VX expands it into
  supplements. (Anthropic Custom Styles "describe how I should respond", Nov 2025.)
- **From references/examples** — point at a cut the user likes or reference videos;
  VX derives the supplement. (Midjourney `--sref`/Style Creator; Jasper Brand Voice
  ingesting samples — both treat a saved style as a **named, versioned artifact**.)
- **From learned taste** — distill the persistent decision ledger (§3): the
  director's accumulated KEEP/CUT choices become a profile the user can name + save.

Store/version via the existing `versioning.py` lineage; profiles are shareable.
This is real new work: a `StyleProfile` model + an authoring/loading path +
compilation into `phase1_supplement`/`phase2_supplement`/`has_phase3`.

---

## 6. Agent-native UI/UX

Rethink the four screens around the **loop**, not a batch tool (extends "The Living
Cut" React Mode in `PRD.md`/`UIUX.md`):

- **Make context visible and editable.** People (from quick-scan), the Creative
  Brief tiers, and the active StyleProfile are first-class, on-screen, editable —
  because they *are* the agent's context. Changing them re-steers the director.
- **Surface reasoning, proposals, and uncertainty.** The director's plan and each
  proposed edit appear as a **ghost diff on the live cut** with a plain reason and a
  **projected eval delta** (§4); the human accepts/edits/rejects per edit. Show when
  the agent is *unsure* (low-confidence cut, clipped speech) rather than hiding it.
- **One shared, instant result.** The agent's output is the same
  `AVMutableComposition` the human scrubs — no "render to see it." Reactions and
  manual edits and AI edits all flow through one storyboard, one decision ledger,
  one **server-authoritative undo stack** (resolves the undo-authority question in
  `SYSTEM-DESIGN.md` §8).
- **Style as a creative surface.** Picking/authoring a StyleProfile is a primary
  action (Briefing/Settings), not a buried dropdown.

---

## 7. 2026 agent-engineering fact-check

Researched against Anthropic engineering, Chroma, Cognition, Google Cloud, and
academic sources; an adversarial pass filtered hype and corrected attributions.

### Verdict: the design tracks established 2025-2026 practice
- **Context rot is real** — quality degrades before the window limit; curate the
  smallest high-signal context (Chroma "Context Rot", Jul 2025; "Lost in the
  Middle", Liu et al. 2023). → drives §2's Clip Index + JIT hydration.
- **Context engineering = curated finite resource**; just-in-time retrieval,
  compaction, externalized durable state, structured note-taking (Anthropic,
  "Effective context engineering", Sept 2025; "long-running agents harness", Nov
  2025). → §2/§3.
- **Separate reasoning from structured commitment** (two-pass) — VX's Phase-2 split
  already does this (constrained-decoding-degrades-reasoning literature). → keep.
- **Few well-scoped tools, clear schemas, human-readable returns** (Anthropic
  "Writing tools for agents", Sept 2025). → §4.
- **HITL: gate the blast radius, propose→approve/edit/reject→apply** (LangGraph
  interrupt+resume). → reuse `propose_edits`/`execute_proposal_batch`.
- **Reliability compounds; keep harnesses minimal** — long autonomous loops fail
  (0.85ⁿ math). → batched single-turn react, short loops.
- **Orchestrator-worker for breadth, single-thread for coupled work; share full
  traces; ~15× token cost** (Anthropic multi-agent research, Jun 2025; Cognition
  "Don't build multi-agents", Jun 2025; cost analyses 2026). → §4 sub-agents, gated.
- **User-authored style as a named, versioned artifact, from description/examples/
  references** (Anthropic Custom Styles, Nov 2025; Midjourney Style Creator; Jasper
  Brand Voice). → §5.

### Honesty notes (from the adversarial pass)
- **Evals-as-steering** is an *emerging, vendor-led* practice; adopt the principle
  (online scorers, projected deltas) but don't overstate maturity.
- Multi-agent **steering-degradation-with-count** is low-confidence (a 2026 preprint
  reports a wide ~21–60% flat-context range, not a clean curve) — treated as
  directional, hence the "keep fan-out narrow (3–5)" guardrail, not a hard law.
- Specific scare-stats common in agent blogs (a named airline mass-misbooking; exact
  self-RAG hallucination percentages) were **unverifiable or misattributed** and are
  deliberately **not** used here. The compounding-reliability *math* stands on its own.
- Several "2025/2026" secondary blogs recycle older primary results (LongLLMLingua,
  EMNLP 2023; Lost-in-the-Middle, 2023) — attributed to primaries above.

---

## 8. Reuse map vs. new work

| Capability | Status | File / note |
|---|---|---|
| ReAct director agent (autonomous + chat) | **reuse** | `editorial_director.py` `run_editorial_review:191`, `run_director_chat:597` (opt-in, `ReviewConfig.enabled=False`) |
| INSPECT/EDIT/CONTROL tools | **reuse** | `director_tools.py` — these *are* the just-in-time hydration tools |
| Propose → approve → apply (ghost-diff seam) | **reuse** | `propose_edits` / `execute_proposal_batch` / `pending_proposal` |
| Per-edit regression gating | **reuse** | `_compute_eval_scores:375`, `_REGRESSION_WEIGHTS:407` (revert on >~10% drop) |
| Tiered Creative Brief | **reuse** | `models.py:CreativeBrief`, `briefing.py` |
| Reason→structure→assemble split | **reuse** | `editorial_phase2.py` (2A/2A.5/2B) |
| Versioning / lineage for artifacts | **reuse** | `versioning.py` |
| **Clip Index + JIT context @100–200 scale** | **NEW** | today eager-loads all reviews+transcripts (`editorial_director.py:227-228`) |
| **Persistent director-decision memory (ledger)** | **NEW** | used-clip / rejected-with-reason / open gaps — absent today |
| **Projected eval delta (pre-apply dry-run scorer)** | **NEW** | current batch evals only *after* applying; `score_storyboard` is report-only |
| **User-authored StyleProfile + authoring/compilation** | **NEW** | only hardcoded `SILENT_VLOG`; `__custom__` ≠ supplement injection |
| **Per-section director sub-agents (orchestrator)** | **NEW (gated)** | Phase-2 at scale; single-threaded final assembly |
| Keyword transcript search · cost-preview · undo | **NEW (small)** | tool-set gaps noted in §4 |

---

## 9. Phased roadmap (deferred — for evaluation)
- **A1 Context engineering:** build the Clip Index artifact + route the director to
  reason over it and hydrate via existing INSPECT tools; add compaction. (Biggest
  quality win at scale; no new model behavior.)
- **A2 Decision memory:** persistent structured ledger (used/rejected/gaps), read
  back each turn; surface rejected-with-reason so the agent stops re-proposing.
- **A3 Projected eval delta:** pure dry-run scorer feeding the ghost diff.
- **A4 StyleProfile:** model + authoring (from brief/references/learned-taste) +
  compilation into existing supplements + versioned storage.
- **A5 Per-section sub-agents (gated by project size):** orchestrator-worker for
  Phase-2 breadth; measure the token multiplier; keep final assembly single-threaded.
- Each phase: keep the current director working; add evals + a verification spike.

## 10. Open decisions
- **Clip Index granularity** — how compact before the director loses signal; what
  forces a hydrate.
- **Decision-memory scope** — per-project only, or a cross-project taste profile
  (feeds StyleProfile "learned taste")?
- **Sub-agent trigger** — clip-count threshold for fan-out vs. the proven single
  Divide-&-Conquer sequential pass; given the 15× cost, default may be "off."
- **Style authoring surface** — how much the user edits the compiled supplements vs.
  a pure black-box "describe it / show me a reference."

---

## Companion
Rendering/preview/export/handoff lives in
[`COMPOSITION-ARCHITECTURE.md`](./COMPOSITION-ARCHITECTURE.md). The two layers share
one surface: the agent proposes onto, and the human reacts to, the **same live
`AVMutableComposition`** — so the director's "current result" is always one
instant, file-free scrub away.
