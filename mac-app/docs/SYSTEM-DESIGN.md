# VX — System Design

> Three tiers: **SwiftUI app ⇄ FastAPI sidecar ⇄ existing Python pipeline +
> `library/`**. The sidecar adds NO pipeline logic — it wraps the same functions
> the CLI calls and serves the same JSON artifacts.

## 1. Architecture

```
┌─────────────────────────────┐     HTTP / WebSocket (loopback :8765)
│  SwiftUI macOS app (mac-app/VX) │ ───────────────────────────────────┐
│  • DesignSystem (tokens)        │                                     │
│  • Components / Views           │                                     ▼
│  • APIClient (REST) + JobStream │              ┌──────────────────────────────────┐
│  • AVKit player                 │              │  FastAPI sidecar                    │
└─────────────────────────────┘              │  src/ai_video_editor/server/        │
                                              │   app.py  routes.py  jobs.py  schemas│
                                              └───────────────┬──────────────────────┘
                                                              │ direct function calls
                                                              ▼
                              ┌───────────────────────────────────────────────────────┐
                              │  Existing pipeline (unchanged contracts)               │
                              │  editorial_agent.run_editorial_pipeline                │
                              │  editorial_phase1/2/3 · rough_cut.run_rough_cut        │
                              │  render · versioning · tracing · director_tools        │
                              └───────────────┬───────────────────────────────────────┘
                                              ▼
                              library/<project>/ {project.json, clips/, storyboard/,
                                                  exports/, traces.jsonl}
```

**Why a sidecar (not subprocess-the-CLI, not a rewrite):** the pipeline is pure
Python functions with no server. A thin FastAPI process imports them directly —
clean typed contract, real-time progress over WebSocket, and reusable beyond the
app. The native shell (SwiftUI) owns realtime/UI; Python owns batch throughput.
See `PERFORMANCE.md` for why this split (not a language port) is the right move.

## 2. The API contract (implemented today)

Base URL `http://127.0.0.1:8765` (env `VX_HOST`/`VX_PORT`). Defined in
`server/routes.py`; DTOs in `server/schemas.py`; Swift mirrors in
`mac-app/VX/Sources/VX/Models/`.

**Reads** (serve `library/` artifacts):
- `GET /health` → `{ok, library}`
- `GET /projects` → `[ProjectSummary]` (enumerate `library/*/project.json` + derived state)
- `GET /projects/{id}` → `ProjectDetail` (clips, latest version, storyboard/rough-cut paths)
- `GET /projects/{id}/storyboard` → raw `EditorialStoryboard` JSON (via `versioning` `_latest` resolution)
- `GET /projects/{id}/cost` → `CostSummary` (from `traces.jsonl` via `tracing.summarize_traces`)
- `GET /projects/{id}/clips` → `[{clip_id, has_proxy}]`
- `GET /media/proxy/{id}/{clip}` · `GET /media/roughcut/{id}` → `FileResponse` for the player

**Mutations → background jobs** (dispatch to the real pipeline):
- `POST /projects` → ingest + preprocess a folder → `JobInfo`
- `POST /projects/{id}/analyze` → `run_editorial_pipeline(interactive=False)` → `JobInfo`
- `POST /projects/{id}/cut` → `run_rough_cut` (`proxy_mode` supported) → `JobInfo`
- `GET /jobs` · `GET /jobs/{id}` → `JobInfo`
- `WS /jobs/{id}/ws` → live `{status, stage, progress, cost, log_tail}` until terminal

### Verified end-to-end (against the real `library/`)
8 projects enumerated; `myanmar` storyboard decodes to the exact
`EditorialStoryboard` shape (56 segments); cost endpoint returns a real per-phase
breakdown (`phase1`, `phase2a_reasoning`, `phase2b_assembly`, `director_chat`, …).

## 3. Job model

`server/jobs.py` — a single background worker executes jobs FIFO (realistic: one
machine, ffmpeg/LLM contend anyway). Each job captures the pipeline's stdout via
a tee, parses the existing `[2/4] Phase 1: …` lines into `stage`/`progress`, and
pushes snapshots to WebSocket subscribers. No pipeline code changed.

> **Known constraint (drives roadmap §5):** the worker swaps the *process-global*
> `sys.stdout` while a job runs. This is fine for batch jobs, but the
> synchronous instant-edit path (below) MUST capture ffmpeg output via a
> subprocess pipe, never `sys.stdout`, to avoid colliding with a running job's
> tee. Alternatively, refactor `jobs.py` to per-job capture.

## 4. Data model bridge (Codable ⇄ Pydantic)

The storyboard JSON is the single source of truth (`models.py:EditorialStoryboard`).
Swift `Codable` mirrors in `Models/Storyboard.swift` use `CodingKeys` to map
snake_case (`in_sec`, `clip_id`, `story_arc`) and tolerate unknown keys
(`appears_in`, `segment_indices`, `strategy`) so pipeline additions don't break
decoding. All timestamps are seconds (Double), consumed directly by AVKit.

## 5. Roadmap: the substrate for "The Living Cut"

> **Composition engine:** the preview/export/handoff layer is designed in depth in
> [`COMPOSITION-ARCHITECTURE.md`](./COMPOSITION-ARCHITECTURE.md) — VX composes by
> reference (`AVMutableComposition` + `AVPlayer`, zero intermediate files) instead
> of the current render-every-segment-to-disk model (measured 814 MB / ~85 s on
> `myanmar`). Direct export via VideoToolbox; pro handoff via OTIO `.otioz` +
> FCPXML 1.10. That doc supersedes the "realtime-vs-batch split" sketch here.
>
> **Agent-Director layer:** see
> [`AGENT-DIRECTOR-ARCHITECTURE.md`](./AGENT-DIRECTOR-ARCHITECTURE.md) — VX as an
> agent-native director: context engineering at clip scale (Clip Index + just-in-time
> hydration, since today's director eager-loads all reviews+transcripts), persistent
> director-decision memory, the propose→ghost-diff→approve loop (reusing the
> Editorial Director + `director_tools.py` + eval regression gating), **built-in +
> user-authored StyleProfiles**, and the agent-native UIUX. Includes a 2026
> agent-engineering fact-check with an adversarial hype filter and an explicit
> reuse-vs-new-work map.

Sequenced so interactive features never serve stale/unwatchable frames. Three
**substrate blockers** come first:

| # | Blocker | Status | Change |
|---|---|---|---|
| 1 | **Segment cache key** | **done** | `rough_cut._segment_cache_name` — content-addressed (clip+in/out+transition+overlays+format+mode), not index. Test: `tests/test_rough_cut_cache.py`. |
| 2 | **Scrubbable playback proxy** | roadmap | second preprocess pass → 540/720p H.264 @ native fps; `EditorialProjectPaths.playback`; `GET /media/playback/{id}/{clip}`. Keep 360p@1fps for the LLM only. |
| 3 | **Sync/async split** | roadmap | synchronous edit endpoints that mutate a warm in-memory session, bypassing the FIFO job queue; ffmpeg output via subprocess pipe (see §3 constraint). |

Then the interactive layer (each reuses existing capability — see table §6):
- **Persistent editing session** — `POST /session/open` warms & caches a
  `DirectorToolContext` per (project, track) with single-writer discipline; the
  4th de-facto substrate piece (memory/eviction/restart-recovery/undo-authority
  must be specified). Phases 2–6 all depend on it.
- **Synchronous instant-edit** — `PATCH /projects/{id}/storyboard/segment/{i}`
  (in/out/purpose/transition), reorder, discard/restore; `POST
  …/segment/{i}/preview` (single-beat re-encode). Reuses the per-segment mutation
  primitives (`director_tools` `_action_update/_remove/_move/_add` with
  `skip_regression=True`).
- **Batched-react compiler** — new `domain/reactions.py` (pure, with
  `tests/domain/` test per the layering invariant): `ReactionIntent[]` →
  one director seed. `POST /session/reactions/apply` → `{prose, structured
  edits, ghost diff, projected eval delta}`. Reuses `propose_edits` /
  `execute_proposal_batch`.
- **Structured proposal + dry-run eval** — extend `propose_edits` to also emit a
  structured edits array + a *pre-apply* eval delta computed by a new pure
  dry-run scorer in `domain/` (apply to a `model_copy`, score, discard — the
  live batch only evals post-apply). Powers the ghost diff + per-edit veto with a
  mandatory "do it anyway."
- **Cheap-variant engine** — persist `editorial_plan_text` (Phase 2A reasoning)
  as a versioned artifact; `POST /projects/{id}/variants` with
  `reuse_reasoning_from`. Reuses tracks + compositions (`versioning.py`).
- **Snap index + compare** — on session open, precompute per-clip cut-safe points
  from `transcript.json` (silence/speaker turns) + `scenes/manifest.json`;
  `GET …/snap-index/{clip}`, `GET …/compare?a=&b=`.

## 6. Reuse map (what already exists)

| Capability | File | App use |
|---|---|---|
| Per-segment mutations w/o LLM (`skip_regression=True`) | `director_tools.py` | every direct gesture |
| Structured proposal cached on context (`pending_proposal`) + batch apply w/ eval | `director_tools.py` | "Do it" card + ghost diff |
| Segment grids / contact strip (today LLM-only) | `render.py` | react beat thumbnails, Inspector filmstrip |
| Targeted per-clip re-review (`only_clip_ids`) | `editorial_phase1.py` | "Re-review this clip" (~one clip's cost) |
| Unused-footage discovery (`get_unused_footage`) | `director_tools.py` | "Find a better take" |
| Tracks + compositions + two-phase commit lineage | `versioning.py` | A/B variants, Promote, lineage view |
| Proxy-mode rough cut + per-segment cache | `rough_cut.py` | "watch it re-assemble", variant preview |
| Speech-cut-safety + storyboard scoring | `eval.py` | amber "clips a word" dots, projected delta |
| FCPXML export | `fcpxml_export.py` | pro exit ramp |

## 7. Process lifecycle

Dev: run `python -m ai_video_editor.server` (or `vx serve`, roadmap) from the
repo root; launch the app (`swift run VX` in `mac-app/VX`, or via Xcode). The app
health-checks the sidecar on `:8765` and shows an offline badge until reachable.
`SidecarManager` can optionally spawn the sidecar (`VX_AUTOSPAWN_SIDECAR=1`); a
shipping `.app` would bundle a Python runtime and spawn it on launch.

## 8. Open questions to resolve before the interactive build

- **Undo authority** — server-side on the session (recommended) so manual + react
  + AI edits share one stack; the client sends undo/redo commands.
- **Concurrent tabs / restart recovery** for the warm session + undo stack.
- **Claude path parity** — React/visual features assume Gemini-style assets
  (native video, segment grids); the Claude path (base64 frames) degrades — gate
  or message it.
- **Per-segment discard model** — `discarded` is clip-level (`DiscardedClip`);
  the Discarded tray needs a session-scoped `DiscardedSegment` snapshot (full
  in/out/purpose/position) to restore losslessly.
