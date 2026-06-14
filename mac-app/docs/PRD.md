# VX — Product Requirements (Native macOS App)

> Status: v0.1 — drafted alongside the first runnable build. Product vision
> ("The Living Cut") synthesized from a 16-agent design study grounded in the
> codebase; see `PERFORMANCE.md` for the verified latency/cost facts that bound it.

## 1. Why this exists

VX turns a folder of raw trip footage into a polished vlog: it reviews every
clip with an LLM (Phase 1), assembles a structured **EditorialStoryboard** —
story arc + cast + a timed Edit Decision List (Phase 2) — optionally writes a
visual monologue (Phase 3), then renders a rough cut and can export FCPXML.

Today that power is locked behind a **Python CLI/TUI** plus a **one-shot static
HTML preview**. Two problems:

1. **Unapproachable.** A casual vlogger faces a terminal, `vx new / analyze /
   cut`, `$EDITOR` briefing templates, and raw JSON. The HTML preview is
   read-only — you can inspect a cut but not change it without going back to the
   CLI.
2. **Batch, not creative.** The mental model is fire-and-wait: run a phase
   (minutes, dollars), look at the result, edit JSON or chat, re-run. There is
   no interactive loop where you nudge a cut and watch it change.

The product goal: a **native macOS app** that replaces both surfaces with a
single, live editing experience — without throwing away the Python pipeline,
which is reused wholesale via a local sidecar (see `SYSTEM-DESIGN.md`).

## 2. The product: "The Living Cut"

**One open editing session per project. You are always holding a finished,
playable cut; everything you do refines it in place.** There is no "new → analyze
→ cut" sequence in the UI — analysis is the one setup gate, after which you are
always editing a living film.

Two modes onto the **same** storyboard, version lineage, and undo stack — so a
casual user graduates into a pro without switching tools:

- **React Mode (casual on-ramp).** VX hands you a complete first cut and walks
  you through it beat by beat with three verbs — **KEEP / CUT / TELL‑IT** — never
  showing a timecode or the word "segment." Notes batch into a single AI turn.
- **The Cutting Room (pro substrate).** Tap any beat to open the Inspector and
  drag the green-in / red-out scrubber against magnetic speech-boundary snaps,
  reorder the timeline, retag a purpose, discard/restore. **The LLM is never in
  the gesture hot path** — direct edits are in-memory mutations + a single-segment
  re-encode returned in well under a render cycle.

**AI is summoned, never autonomous.** Any AI proposal renders as a **ghost diff**
on the real timeline before you accept, with a projected quality delta and a
per-edit veto (plus a mandatory "do it anyway"). Cost is **honest and ambient**:
a receipt that only moves when AI is summoned, and a per-action estimate *before*
any spend that touches Phase 2.

**Convergence is explicit and reversible.** At a taste fork, "cut it both ways"
spawns cheap A/B variants (reusing persisted Phase 2A reasoning); **Promote**
picks a winner while every other variant stays intact. The **pro exit ramp** is
FCPXML into Resolve/Premiere/FCP.

## 3. Target users (the three lenses we designed against)

| Persona | Wants | How VX serves them |
|---|---|---|
| **Casual vlogger** | A shareable vlog tonight, minimal decisions, zero jargon | React Mode: a finished cut on open, KEEP/CUT/TELL‑IT, one "Apply my notes" |
| **Top-tier editor** | Frame-accurate control, fast keyboard iteration, trust in output | The Cutting Room: direct scrubber/reorder with snapping, ghost-diff review, FCPXML hand-off |
| **UI/UX designer** | A coherent, unmistakably-VX product surface | The design system: the dark room, emerald action color, the 15-hue purpose vocabulary, SF Pro/SF Mono (see `UIUX.md`) |

## 4. The creative loop (user stories)

0. **Import & auto-brief** *(once)* — Drop a footage folder on the Library; a
   quick-scan card shows detected people and activities and asks 1–3 sharp
   questions. → reuses smart briefing (`briefing.py`), preprocessing.
1. **Meet your cut** — The finished rough cut plays start-to-finish the moment
   analysis ends; never a blank timeline.
2. **React beat-by-beat** *(zero re-render, zero cost)* — "Review with me"
   auto-advances through the cut; each beat shows its clip + plain-language
   reason; KEEP / CUT / TELL‑IT.
3. **One batched "Do it"** *(a single AI turn, ghost-diffed)* — "Apply my notes"
   compiles all reactions into one director proposal; review the ghost diff +
   projected delta; accept/veto per edit. → reuses `propose_edits` /
   `execute_proposal_batch` (`director_tools.py`).
4. **Sculpt directly** *(pro hot path, no LLM, sub-render-cycle)* — scrubber drag
   with speech-boundary snapping, reorder, retag, discard/restore. → reuses
   per-segment mutation primitives with `skip_regression=True`.
5. **Watch it re-assemble** — the revised film plays with only changed beats
   re-encoded. → reuses proxy-mode rough cut + per-segment cache (now correct,
   see §6).
6. **Cut it both ways** *(A/B decision relief)* — two variants side by side,
   pick one. → reuses tracks + compositions (`versioning.py`) + cheap-variant
   reuse of Phase 2A reasoning.
7. **Promote & hand off** — Promote a winning cut (reversible); export FCPXML.

## 5. Scope

**In scope (first runnable build — shipped):**
- The four screens (Library, Briefing, Editor, Settings), faithful to the design
  system, in native SwiftUI.
- The FastAPI sidecar wrapping the real pipeline: read endpoints wired to live
  `library/` data; create/analyze/cut as background jobs with WebSocket progress.
- The **segment cache-key correctness fix** (substrate blocker #1 — done).

**In scope (roadmap, specified here + in `SYSTEM-DESIGN.md`):** React Mode, the
synchronous instant-edit path, the persistent editing session, the scrubbable
playback proxy, batched-react compiler, ghost-diff proposals, cheap variants,
snap index. These are sequenced behind the three substrate blockers (§6).

**Out of scope (v1):** conversational chat as the cold-start home interaction;
making the variant the primary unit ("never converge early"); full NLE finishing
depth (J/K/L, ripple/roll, multitrack mix, keyframes, segment splitting,
transcript editing); silent automatic rollback without override; app
notarization/signing/distribution; a pipeline rewrite into another language
(rejected on evidence — see `PERFORMANCE.md`).

## 6. Non-negotiable build-order rules (from adversarial verification)

Three **substrate blockers** must land before any "watch it re-assemble" feature,
or the app serves stale/unwatchable frames:

1. **Segment cache-key fix** — *done.* `rough_cut.py` keyed cached segments on
   index, not in/out, so a trim served stale video and a reorder re-encoded
   needlessly. Now content-addressed (`_segment_cache_name`); regression test in
   `tests/test_rough_cut_cache.py`.
2. **Scrubbable playback proxy** — the existing proxy is 360×240 @ 1fps (for the
   LLM); humans need a 540/720p native-fps proxy to scrub/play. Keep the 1fps
   proxy for the LLM only.
3. **Sync/async path split** — the sidecar job runner is a single-worker FIFO
   with a process-global stdout tee; interactive gestures must bypass it via
   synchronous endpoints, never queue behind multi-minute analyze/render jobs.

And: **keep the LLM out of the gesture loop** — trims/reorders/retags are local
mutations + one ffmpeg re-encode, never an LLM call.

## 7. Success metrics

- **Time-to-first-cut:** a non-technical user imports a folder and is watching a
  playable cut without touching a terminal or seeing JSON.
- **Edit latency:** a single trim/reorder reflects in the preview in seconds
  (gated by substrate #2/#3; perf gate p95 single-beat re-encode < 2.5s on the
  playback proxy — see `PERFORMANCE.md`).
- **Cost honesty:** every Phase-2-touching action shows an estimate before spend;
  the receipt only moves when AI is summoned.
- **Pro trust:** FCPXML round-trips into at least one NLE with transitions +
  overlays intact (manual QA gate).
