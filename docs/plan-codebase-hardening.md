# Plan: Codebase Hardening (Batches 1-5)

**Status:** Complete
**Priority:** Must-do before new ROADMAP features
**Created:** 2026-04-03

## Context

A comprehensive code review identified 26 issues across 9 source files: 2 critical security vulnerabilities, 9 high-severity reliability issues, 12 medium issues, and 3 low issues. These span XSS, silent subprocess/parallel failures, missing timeouts, unsafe API key access, and resource leaks. Fixing these before adding new features prevents compounding technical debt.

---

## Task List

### Batch 1: Security & Input Validation (Critical)

- [x] **1.1 XSS in `render.py`** — LLM-generated data interpolated into HTML without escaping
- [x] **1.2 Project name validation in `cli.py`** — no validation, path traversal possible
- [x] **1.3 Drawtext escaping in `preprocess.py`** — filename in ffmpeg filter unescaped

### Batch 2: API Key Safety & Gemini Upload Timeout (Critical)

- [x] **2.1 API key access via `os.environ[]`** — crashes with KeyError instead of helpful message
- [x] **2.2 Gemini file upload poll timeout** — infinite loop if Gemini hangs

### Batch 3: Subprocess Robustness (High)

- [x] **3.1 Silent failure in `detect_scenes()`** — subprocess returncode never checked
- [x] **3.2 Missing ffmpeg timeouts** — 7 subprocess calls with no timeout parameter
- [x] **3.3 Temp file cleanup on error** — `_transcribe_chunks` cleanup not in try/finally

### Batch 4: Error Handling & Resilience (High)

- [x] **4.1 Silent exception swallowing in parallel workers** — failures printed but not propagated
- [x] **4.2 Unhandled JSON parse errors** — `json.loads()` on cached files with no try/except
- [x] **4.3 Narrow broad exception handlers** — `except Exception` catches too much

### Batch 5: Resource Management & Configuration (Medium)

- [x] **5.1 File cache cleanup** — stale Gemini URI entries never removed
- [x] **5.2 Proxy cache integrity check** — zero-byte files pass existence check
- [x] **5.3 Transcript chunk merge logging** — segments silently dropped during merge
- [x] **5.4 Hardcoded model pricing fallback** — unknown models crash cost estimation

---

## Detailed Implementation

### Batch 1: Security & Input Validation

#### 1.1 XSS in `render.py` (lines 310-355)

**Problem:** LLM-generated data (cast names, descriptions, arc titles, notes, warnings) interpolated directly into HTML without escaping. If an LLM returns `<script>alert(1)</script>` in a cast name, it executes in the browser.

**Fix:**
- Add `import html` at top of file
- Create helper: `_esc = lambda v: html.escape(str(v), quote=True)`
- Wrap all user-data interpolations:
  - Line 310: `seg.clip_id`, `seg.purpose` in title attribute
  - Line 316: `p.name`, `p.description`, `p.role`, `', '.join(p.appears_in)`
  - Line 320: `m.section`, `m.strategy`, `m.notes`
  - Lines 323-324: each item in `technical_notes`, `pacing_notes`
  - Line 340: each warning item
  - Line 346: `a.title`, `a.description[:250]`
  - Line 355: `sb.title`
- Do NOT escape: numeric fields (`seg.index`, `seg.duration_sec`), internal constants (`PURPOSE_COLORS` keys)

**Size:** Medium

#### 1.2 Project Name Validation in `cli.py` (~line 117)

**Problem:** No validation on project names. Names like `../../etc/passwd` or `project; rm -rf /` accepted.

**Fix:**
- Add `import re` at top (if not present)
- In `cmd_new()`, after `name = args.name`:
  ```python
  if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
      print(f"Error: Project name may only contain letters, digits, hyphens, and underscores.")
      sys.exit(1)
  ```

**Size:** Small

#### 1.3 Drawtext Escaping in `preprocess.py` (~line 616)

**Problem:** Filename interpolated into ffmpeg drawtext filter without escaping. Filenames with quotes, colons, or backslashes break the filter.

**Fix:**
- Add helper function:
  ```python
  def _escape_drawtext(text: str) -> str:
      return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "%%")
  ```
- Apply at line 616: `label = _escape_drawtext(c["filename"])`

**Size:** Small

---

### Batch 2: API Key Safety & Gemini Upload Timeout

#### 2.1 API Key Access via `os.environ[]`

**File:** `editorial_agent.py` (lines 384, 686, 738, 768, 898, 919)

**Problem:** `os.environ["GEMINI_API_KEY"]` crashes with `KeyError` traceback instead of helpful error message.

**Fix:**
- Add helper at module level:
  ```python
  def _require_api_key(name: str) -> str:
      key = os.environ.get(name)
      if not key:
          raise RuntimeError(f"{name} is not set. Add it to your .env file (see .env.example).")
      return key
  ```
- Replace all 6 `os.environ["GEMINI_API_KEY"]` and `os.environ["ANTHROPIC_API_KEY"]` call sites

**Size:** Medium

#### 2.2 Gemini File Upload Poll Timeout

**Files:** `editorial_agent.py` (398-400), `transcribe.py` (257-259, 325-327), `gemini_analyze.py` (26-29), `briefing.py` (274-276)

**Problem:** `while video_file.state.name == "PROCESSING"` loops forever if Gemini hangs.

**Fix:** At all 4+ locations, add bounded loop:
```python
GEMINI_UPLOAD_TIMEOUT_SEC = 300
start = time.monotonic()
while video_file.state.name == "PROCESSING":
    if time.monotonic() - start > GEMINI_UPLOAD_TIMEOUT_SEC:
        raise TimeoutError(f"Gemini file processing timed out after {GEMINI_UPLOAD_TIMEOUT_SEC}s")
    time.sleep(3)
    video_file = client.files.get(name=video_file.name)
```

**Size:** Small

---

### Batch 3: Subprocess Robustness

#### 3.1 Silent Failure in `detect_scenes()` (~line 367)

**Problem:** `subprocess.run(cmd, capture_output=True, text=True)` has no returncode check. Silently produces empty scene list.

**Fix:**
```python
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode != 0:
    print(f"  WARN: scene detection failed (rc={result.returncode}): {result.stderr[:200]}")
    return []
```

**Size:** Small

#### 3.2 Missing ffmpeg Timeouts

**Files:** `preprocess.py` (lines 280, 367, 418, 451, 645), `rough_cut.py` (lines 917, 1096)

**Problem:** 7 subprocess.run() calls with no timeout. Hangs indefinitely on corrupt files.

**Fix:** Add `timeout=` parameter and catch `subprocess.TimeoutExpired`:
- `create_proxy()`: 300s (5 min)
- `detect_scenes()`: 120s (2 min)
- `extract_audio()`: 120s (2 min)
- `generate_contact_sheet()`: 60s
- `concat_proxies()` per-segment: 60s
- `rough_cut.py` segment extraction: 120s
- `rough_cut.py` final concat: 600s (10 min)

**Size:** Medium

#### 3.3 Temp File Cleanup on Error (`transcribe.py` ~lines 409-432)

**Problem:** Chunk cleanup runs only on happy path. If transcription raises, temp files leak.

**Fix:** Wrap in try/finally:
```python
else:
    try:
        # ... existing chunked transcription code ...
        gemini_result = _merge_chunk_transcripts(chunk_results)
    finally:
        chunk_dir = proxy_path.parent / "_transcribe_chunks"
        if chunk_dir.exists():
            for f in chunk_dir.iterdir():
                f.unlink()
            chunk_dir.rmdir()
```

**Size:** Small

---

### Batch 4: Error Handling & Resilience

#### 4.1 Silent Exception Swallowing in Parallel Workers

**File:** `editorial_agent.py` (lines 166, 312, 501)

**Problem:** `except Exception as e: print(...)` in thread pool workers. Failures printed but not propagated. Partial data silently flows downstream.

**Fix:** At each location, track failures and abort if >50% fail:
```python
failed_ids = []
# ... existing collection loop ...
if failed_ids:
    print(f"\n  WARNING: {len(failed_ids)}/{total} clips failed: {', '.join(failed_ids)}")
    if len(failed_ids) > total // 2:
        raise RuntimeError(f"Too many failures ({len(failed_ids)}/{total}). Aborting.")
```

Apply at: preprocessing (line 166), transcription (line 312), Phase 1 reviews (line 501).

**Size:** Medium

#### 4.2 Unhandled JSON Parse Errors

**File:** `editorial_agent.py` (lines 74, 236, 382, 515, 557, 568)

**Problem:** `json.loads()` on cached files (manifest, reviews, transcripts) with no try/except. Corrupt cache = crash.

**Fix:** Wrap critical `json.loads()` calls:
```python
try:
    data = json.loads(path.read_text())
except json.JSONDecodeError as e:
    print(f"  WARN: corrupt cache {path.name}, will re-generate: {e}")
    # return None or continue as appropriate
```

**Size:** Medium

#### 4.3 Narrow Broad Exception Handlers

**Files:** `preprocess.py`, `rough_cut.py`

**Problem:** `except Exception` catches too much (including `KeyboardInterrupt` in some contexts).

**Fix:** Narrow to specific types where possible:
- `CalledProcessError` for subprocess failures
- `JSONDecodeError` for JSON parsing
- `ValueError`, `KeyError` for data access

Leave thread pool `except Exception` handlers as-is (those correctly need to catch everything from workers).

**Size:** Small

---

### Batch 5: Resource Management & Configuration

#### 5.1 File Cache Cleanup (`file_cache.py`)

**Problem:** Stale entries never removed. Cache grows indefinitely.

**Fix:** In `load_file_api_cache()`, purge expired entries:
```python
def load_file_api_cache(editorial_paths):
    cache_path = editorial_paths.root / "file_api_cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        now = time.time()
        expired = [k for k, v in cache.items()
                   if now - v.get("cached_at", 0) > FILE_API_CACHE_MAX_AGE_SEC]
        if expired:
            for k in expired:
                del cache[k]
            save_file_api_cache(editorial_paths, cache)
        return cache
    return {}
```

**Size:** Small

#### 5.2 Proxy Cache Integrity Check (`preprocess.py` ~line 251)

**Problem:** Cache check only tests `path.exists()`. Zero-byte files pass.

**Fix:** Change to `path.exists() and path.stat().st_size > 0` at:
- `create_proxy()` (line 251)
- `extract_frames()` manifest check
- `detect_scenes()` manifest check
- `extract_audio()` WAV check

**Size:** Small

#### 5.3 Transcript Chunk Merge Logging (`transcribe.py` ~line 204)

**Problem:** Segments with timestamp drift beyond 2s margin silently dropped.

**Fix:** Add logging when segments are discarded:
```python
if seg.start > chunk_dur + 2.0:
    log.debug("Dropped drifted segment at offset %.1f: start=%.1f > chunk_dur=%.1f",
              offset_sec, seg.start, chunk_dur)
    continue
```

**Size:** Small

#### 5.4 Hardcoded Model Pricing Fallback (`tracing.py` lines 21-29)

**Problem:** Unknown models crash or return $0 silently. No way to override.

**Fix:** Add warn-once fallback:
```python
_warned_models: set[str] = set()

def _get_rate(model: str, direction: str) -> float:
    rates = COST_PER_1M_TOKENS.get(model)
    if not rates:
        for key in COST_PER_1M_TOKENS:
            if model.startswith(key):
                rates = COST_PER_1M_TOKENS[key]
                break
    if not rates:
        if model not in _warned_models:
            _warned_models.add(model)
            print(f"  WARN: Unknown model '{model}' for cost estimation. Update COST_PER_1M_TOKENS.")
        return 0.0
    return rates[direction]
```

**Size:** Small

---

## Verification Plan

| Batch | Test |
|-------|------|
| 1 | Create project with name `../../test` — verify rejection. Put `<script>alert(1)</script>` in a review JSON cast name, render preview, verify escaped HTML |
| 2 | Unset `GEMINI_API_KEY`, run `vx analyze` — verify clear RuntimeError, not KeyError. Simulate Gemini hang — verify 5-min timeout |
| 3 | Run pipeline on real footage. Create zero-byte proxy — verify regenerated. Verify no ffmpeg hangs on corrupt file |
| 4 | Corrupt a cached `review_*.json` — verify re-analysis. Run with one unreadable clip — verify partial failure summary and abort threshold |
| 5 | Check `file_api_cache.json` after run — verify no entries >90 min. Create zero-byte proxy — verify regenerated |

Run `ruff check src/` and `ruff format src/` after each batch.

---

## Files Modified

| File | Tasks |
|------|-------|
| `src/ai_video_editor/render.py` | 1.1, 4.3 |
| `src/ai_video_editor/cli.py` | 1.2 |
| `src/ai_video_editor/preprocess.py` | 1.3, 3.1, 3.2, 4.3, 5.2 |
| `src/ai_video_editor/editorial_agent.py` | 2.1, 4.1, 4.2 |
| `src/ai_video_editor/transcribe.py` | 2.2, 3.3, 5.3 |
| `src/ai_video_editor/gemini_analyze.py` | 2.2 |
| `src/ai_video_editor/rough_cut.py` | 3.2, 4.3 |
| `src/ai_video_editor/file_cache.py` | 5.1 |
| `src/ai_video_editor/tracing.py` | 5.4 |
