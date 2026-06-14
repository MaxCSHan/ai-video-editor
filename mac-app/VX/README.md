# VX — Native macOS App

A SwiftUI recreation of VX (see `../docs/` for PRD / UI-UX / system design /
performance). It talks to the Python pipeline through a local **FastAPI sidecar**
— it does not reimplement any pipeline logic.

## Run it (fast path — no Xcode project needed)

From the repo root, start the sidecar:

```bash
uv pip install -e ".[server]"              # one-time: installs fastapi + uvicorn into .venv
.venv/bin/python -m ai_video_editor.server  # serves http://127.0.0.1:8765
```

> Use `.venv/bin/python` (or `source .venv/bin/activate` first). `uv pip install`
> installs into the project's `.venv`; a bare `python` may resolve to a different
> interpreter (e.g. pyenv) that doesn't have the package — that's the
> `No module named 'ai_video_editor'` error.

Then build & launch the app:

```bash
cd mac-app/VX
swift run VX
```

The app health-checks the sidecar on `:8765` (shows an "offline" badge until
reachable), then loads your real projects from `library/`. Open a project to see
its storyboard in the editor; the player streams clip proxies from the sidecar.

> Requires macOS 13+ and the Swift toolchain (ships with Xcode / Command Line
> Tools). Verified to compile with Swift 6.2.

### Optional: let the app spawn the sidecar
```bash
VX_AUTOSPAWN_SIDECAR=1 VX_REPO=/path/to/ai-video-editor swift run VX
```

## Build into an Xcode app (for distribution)

`swift run` is great for iteration but produces a bare executable. For a proper
`.app` (icon, Info.plist, signing, a bundled Python runtime that auto-spawns the
sidecar): create a macOS App target in Xcode and add `Sources/VX/` to it
(remove `@main` duplication by keeping `VXApp.swift` as the entry). The code is
structured to drop in unchanged.

## Layout

```
Sources/VX/
  VXApp.swift                  @main · window chrome · optional sidecar spawn
  DesignSystem/                VXColors · VXType · VXMetrics · Purpose (tokens 1:1 from the design system)
  Components/                  VXButton · SegmentedControl · PurposeTag · Timecode · Badge · Card · Toolbar · Toast · VXIcon
  Models/                      Codable mirrors of EditorialStoryboard / Project / Job / Cost
  Services/                    APIClient (REST) · JobStream (WebSocket) · AppState · SidecarManager
  Views/                       RootView(AppShell) · LibraryView · BriefingView · EditorView · Inspector · SettingsView
```

## Status

First runnable build: the four screens are faithful to the design system, the
read side is wired to live `library/` data, and create/analyze/cut run as
background jobs with WebSocket progress. The interactive "Living Cut" layer
(React Mode, direct-manipulation scrubber, ghost-diff proposals, A/B variants) is
specified in `../docs/SYSTEM-DESIGN.md` §5 and sequenced behind the three
substrate blockers in `../docs/PERFORMANCE.md`.
