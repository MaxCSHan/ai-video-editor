# Feature Plan: Preview Editor Mode (Local Server)

## Problem

The current HTML preview is a self-contained static file. When a user adjusts segment cut points in the browser, the only way to persist changes is to export a JSON file via browser download, then manually move it into the project's `storyboard/` directory with the correct filename. This multi-step cycle (edit > export > download > move > rename > recut) breaks the creative flow and discourages iterative refinement.

## Solution

Add an **editor mode** — a local FastAPI server launched via `vx edit <project>` — that serves the preview UI and provides API endpoints for direct save-to-disk and in-browser recut. The existing static HTML preview (`vx preview`, `vx cut` output) is fully preserved and remains the default for quick viewing and portable snapshots.

## Relationship to Existing Flows

| Command | Mode | Purpose | Changes? |
|---------|------|---------|----------|
| `vx preview <project>` | Static | Generate self-contained HTML file, open in browser | **No change** |
| `vx cut <project>` | Static | Generate rough cut + static preview HTML | **No change** |
| `vx edit <project>` | **Editor (new)** | Start local server, open interactive editor in browser | **New** |

The static mode is the primary output of the pipeline. Editor mode is an optional, interactive layer on top for when the user wants to refine cut points and iterate quickly.

---

## Architecture

### Frontend: Vanilla JS (no framework, no build step)

The current preview JS is ~270 lines and well-structured. No React/Vue/Svelte needed. The JS is extracted into a standalone file that can operate in two modes:
- **Static mode** (file:// protocol): data inlined, export via browser download (current behavior)
- **Editor mode** (http:// protocol): data loaded via API, save via `POST`, recut trigger

Detection is automatic via `window.location.protocol`.

For future ROADMAP features (drag-to-reorder, waveform viz), use CDN libraries like SortableJS and wavesurfer.js — no build toolchain required.

### Backend: FastAPI + Uvicorn

- **Async-native**: serves multiple proxy videos concurrently while handling API calls
- **Range request support**: Starlette's `FileResponse` handles HTTP Range requests required by `<video>` seeking
- **Native Pydantic**: `EditorialStoryboard` model works as both API schema and domain model — zero glue code
- **Single pip install**: `fastapi` + `uvicorn[standard]`, no system-level deps

### Video Serving

Proxy videos served through the local server at `/api/clips/{clip_id}/proxy` instead of relative filesystem paths. This eliminates browser `file://` cross-origin restrictions that increasingly break video loading in Chrome/Safari. Proxies are 5-8MB each, so localhost serving is instant.

---

## API Design

```
GET  /                              Thin HTML shell (loads JS/CSS from /static/)
GET  /api/project                   Project metadata (name, clip count, versions)
GET  /api/storyboard                Active storyboard JSON (edited version first, then AI latest)
POST /api/storyboard                Save edited storyboard to editorial_edited.json
POST /api/storyboard/reset          Delete editing version, revert to AI original
POST /api/recut                     Trigger rough cut assembly (background, progress via SSE)
GET  /api/recut/status              SSE stream for recut progress events
GET  /api/clips/{clip_id}/proxy     Serve proxy .mp4 with range request support
GET  /api/clips/{clip_id}/duration  Return clip duration (ffprobe)
GET  /api/thumbnails/{filename}     Serve thumbnail images
GET  /static/{path}                 CSS, JS assets
```

### `GET /api/storyboard` Response

```json
{
  "storyboard": { ... },
  "clip_info": { "clip_id": { "duration": 42.5 }, ... },
  "source": "edited",
  "based_on": "editorial_gemini_v1.json",
  "stale": false,
  "ai_latest": "editorial_gemini_v1.json"
}
```

- `source`: `"edited"` or `"ai_v1"` (indicates which file was loaded)
- `stale`: `true` if a newer AI version exists than what the edited version was based on
- `ai_latest`: the current AI latest filename (for staleness comparison)

### `POST /api/storyboard` Request/Response

Request body: full `EditorialStoryboard` JSON (validated by Pydantic).

Response:
```json
{
  "saved": true,
  "path": "storyboard/editorial_edited.json",
  "based_on": "editorial_gemini_v1.json"
}
```

---

## Editing Version Mechanism

### File Layout

```
storyboard/
  editorial_gemini_v1.json          # AI original (immutable after creation)
  editorial_gemini_v1.md
  editorial_gemini_latest.json ->   editorial_gemini_v1.json  (symlink)
  editorial_edited.json             # User-edited working copy (mutable, created on first save)
```

### Rules

1. **AI outputs are never modified.** `editorial_gemini_v1.json` is written by Phase 2 and never touched.
2. **First save creates `editorial_edited.json`** as a copy of the current AI latest with user modifications applied.
3. **Subsequent saves overwrite `editorial_edited.json`** in place (no versioning for interactive edits — undo belongs in the browser, not on disk).
4. **`POST /api/storyboard/reset`** deletes `editorial_edited.json`, reverting to AI original.
5. **`vx cut` respects editing version**: `_find_storyboard_json()` checks `editorial_edited.json` first, then falls back to AI latest. This means once the user edits, all subsequent cuts automatically use the edited version.
6. **Re-analysis creates staleness**: Running `vx analyze` creates `editorial_gemini_v2.json`. The editing version (based on v1) is now stale. The editor UI warns the user and offers to reset or merge.

### Metadata Wrapper

```json
{
  "_editing_meta": {
    "based_on": "editorial_gemini_v1.json",
    "created_at": "2026-03-31T22:15:00Z",
    "last_modified": "2026-03-31T22:30:00Z"
  },
  "title": "Hungyi's Neon Marathon",
  "segments": [ ... ]
}
```

The `_editing_meta` key is stripped before passing to `EditorialStoryboard.model_validate()`. It exists only for tracking lineage and staleness.

---

## `vx edit` Command Behavior

```
$ vx edit my-project
  VX Editor — my-project
  Storyboard: editorial_gemini_v1.json (17 segments)
  Server:     http://localhost:8457

  Press Ctrl+C to stop.
```

1. Resolves the project and finds the active storyboard (edited first, then AI latest)
2. Starts FastAPI/Uvicorn on `localhost:8457` (fallback to next available port if busy)
3. Opens the browser via `webbrowser.open()`
4. Blocks on `uvicorn.run()` until Ctrl+C
5. Stores active port in `exports/.editor_server` lockfile to detect already-running instances

### TUI Integration

The interactive TUI (`_project_actions`) gets a new action:
- **"Edit in browser"** — starts the editor server (same as `vx edit`)
- **"Open preview in browser"** — unchanged, opens the latest static preview.html

---

## Implementation Phases

### Phase A: Extract static assets from render.py

**Goal**: Refactor the monolithic f-string into separate files. Zero behavior change.

**Create:**
- `src/ai_video_editor/static/preview.css` — extracted from the `<style>` block
- `src/ai_video_editor/static/preview.js` — extracted from the `<script>` block
- `src/ai_video_editor/static/preview.html` — thin HTML template with `{placeholders}`

**Modify:**
- `src/ai_video_editor/render.py` — `render_html_preview()` reads CSS/JS from static files and inlines them for the self-contained mode. Template structure becomes: read `preview.html`, fill placeholders, inline CSS/JS.

**Verify:** `vx cut` and `vx preview --static` produce identical output to current behavior.

### Phase B: Create FastAPI server module

**Create:**
- `src/ai_video_editor/server.py` — FastAPI app with all API endpoints

Key implementation details:
- `GET /api/storyboard`: resolve `editorial_edited.json` first, then AI latest via `_find_storyboard_json()`. Include `clip_info` (durations) in response so JS doesn't need a separate call.
- `POST /api/storyboard`: validate via `EditorialStoryboard`, wrap with `_editing_meta`, write to `editorial_edited.json`.
- `GET /api/clips/{clip_id}/proxy`: reuse `_resolve_clip_proxy()` from render.py, return `FileResponse`.
- Static file mount at `/static/` pointing to the `static/` directory.

**Modify:**
- `pyproject.toml` — add `fastapi>=0.115`, `uvicorn[standard]>=0.34` to dependencies
- `src/ai_video_editor/config.py` — add `edited_storyboard` property to `EditorialProjectPaths`:
  ```python
  @property
  def edited_storyboard(self) -> Path:
      return self.storyboard / "editorial_edited.json"
  ```

**Verify:** Start server manually, hit endpoints with curl, confirm JSON responses and video streaming.

### Phase C: Add `vx edit` command

**Modify:**
- `src/ai_video_editor/cli.py`:
  - Add `cmd_edit()` function and `edit` subparser
  - Starts uvicorn, opens browser, blocks until Ctrl+C
  - Update `_find_storyboard_json()` to check `editorial_edited.json` first
- `src/ai_video_editor/interactive.py`:
  - Add "Edit in browser" action to project actions menu

**Verify:** `vx edit my-project` starts server, browser opens, page loads.

### Phase D: Adapt JS for dual-mode operation

**Modify:**
- `src/ai_video_editor/static/preview.js`:
  - Add mode detection: `const SERVER_MODE = location.protocol === 'http:'`
  - Server mode init: `fetch('/api/storyboard')` instead of reading inline `const storyboard`
  - Replace `exportJSON()` with `saveChanges()` (server mode) — `POST /api/storyboard`
  - Keep `exportJSON()` for static mode (unchanged)
  - Video src: `/api/clips/${clipId}/proxy` (server) vs relative path (static)
  - Add UI indicators: "Saved" badge, editing-version banner, staleness warning
  - Add "Recut" button (server mode only)

**Verify:** Open editor → edit segment → save → confirm `editorial_edited.json` on disk. Open static preview → confirm export download still works.

### Phase E: In-browser recut (can be deferred)

**Modify:**
- `src/ai_video_editor/server.py`:
  - `POST /api/recut` runs `run_rough_cut()` in a background thread
  - `GET /api/recut/status` SSE stream pushes progress events (segment N/M extracted, concatenating, done)
- `src/ai_video_editor/static/preview.js`:
  - Recut button triggers `POST /api/recut`
  - Progress bar UI driven by SSE events
  - On completion: reload rough cut `<video>` element source

**Verify:** Click Recut in browser → progress bar fills → rough cut video reloads and plays.

---

## Files Reference

| File | Status | Purpose |
|------|--------|---------|
| `src/ai_video_editor/server.py` | New | FastAPI app, API endpoints, video serving |
| `src/ai_video_editor/static/preview.css` | New | Extracted styles from render.py |
| `src/ai_video_editor/static/preview.js` | New | Extracted + adapted JS (dual-mode) |
| `src/ai_video_editor/static/preview.html` | New | Thin HTML template |
| `src/ai_video_editor/render.py` | Modify | Refactor to read from static files, inline for static mode |
| `src/ai_video_editor/cli.py` | Modify | Add `cmd_edit()`, update `_find_storyboard_json()` |
| `src/ai_video_editor/interactive.py` | Modify | Add "Edit in browser" action |
| `src/ai_video_editor/config.py` | Modify | Add `edited_storyboard` property |
| `src/ai_video_editor/rough_cut.py` | Unchanged | Called from server for recut |
| `src/ai_video_editor/models.py` | Unchanged | Used as API schema |
| `src/ai_video_editor/versioning.py` | Unchanged | Used by server for version management |
| `pyproject.toml` | Modify | Add fastapi, uvicorn deps |

## Existing Code to Reuse

- `render.py:_resolve_clip_proxy()` (line 104) — proxy video lookup by clip_id
- `render.py:_get_clip_duration()` (line 113) — ffprobe-based duration
- `cli.py:_find_storyboard_json()` (line 490) — storyboard resolution logic (extend with edited version)
- `rough_cut.py:run_rough_cut()` (line 200) — full rough cut pipeline (call from server endpoint)
- `versioning.py:next_version()`, `versioned_dir()`, `update_latest_symlink()` — version management

---

## Dependency Additions

```toml
# pyproject.toml
dependencies = [
    ...
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
]
```

FastAPI pulls in Starlette + a few small deps. `uvicorn[standard]` adds uvloop + httptools for performance. No Node.js, no npm, no frontend build step.

---

## Future Considerations (not in scope)

- **Drag-to-reorder segments**: SortableJS via CDN, save reordered indices via `POST /api/storyboard`
- **Add/remove segments**: UI for segment CRUD, validated by Pydantic on save
- **Waveform visualization**: wavesurfer.js via CDN, audio served from `/api/clips/{id}/audio`
- **Side-by-side version comparison**: `GET /api/storyboard?version=ai_latest` vs current edited
- **Live reload**: SSE event when storyboard files change on disk (watchdog or polling)
- **iPad/touch support**: touch event handlers for range scrubber
