# VX — Native macOS App

This directory holds the native macOS app that replaces VX's CLI/TUI + one-shot
HTML preview, plus its planning docs. The app drives the existing Python pipeline
through a local FastAPI sidecar (in `../src/ai_video_editor/server/`) — no
pipeline logic is duplicated.

## Contents

```
mac-app/
  docs/
    PRD.md            Product requirements — "The Living Cut" vision, personas, scope, build-order rules
    UIUX.md           Design contract (the VX design system) → SwiftUI mapping, screen specs
    SYSTEM-DESIGN.md  Three-tier architecture, API contract, job model, roadmap & reuse map
    PERFORMANCE.md    "Should we rewrite it in a faster language?" — evidence-based answer + optimization roadmap
  VX/                 The SwiftUI app (see VX/README.md to build & run)
```

## Quick start

```bash
# from the repo root
uv pip install -e ".[server]"               # installs into .venv
.venv/bin/python -m ai_video_editor.server  # sidecar on :8765 (use the venv's python, not pyenv)
# in another shell
cd mac-app/VX && swift run VX                # the app
```

## How it was built

The product vision was synthesized from a multi-agent design study (4 codebase
maps → 4 competing flow visions → 4 lens judges → synthesis → 3 adversarial
verifiers), all grounded in the real codebase. The verifiers caught a real
correctness bug — the rough-cut segment cache keyed on segment index, not in/out,
so trims served stale frames — which is fixed and tested
(`../src/ai_video_editor/rough_cut.py:_segment_cache_name`,
`../tests/test_rough_cut_cache.py`). See `docs/PERFORMANCE.md`.
