# Design: FCPXML Export for DaVinci Resolve

## Context

VX currently outputs a rough cut MP4 and an HTML preview. Users who want to fine-tune the edit in a professional NLE have no path to do so. The mazsola2k project demonstrates that generating FCPXML for DaVinci Resolve import is practical and valuable — it bridges the gap between "AI rough cut" and "professional edit." This feature would let users run `vx export-xml <project>` and get an `.fcpxml` file they can import into DaVinci Resolve (and also Final Cut Pro) to finalize the edit with full control over every cut.

---

## Research Findings

### FCPXML Format

- **Target version: 1.9** — this is what DaVinci Resolve itself exports, well-tested across Resolve 17+. Versions 1.12+ (FCP 11) have known compatibility issues with Resolve.
- The format is straightforward XML: `<resources>` (formats + assets) → `<project>` → `<sequence>` → `<spine>` (clips in order).
- All timing uses **rational fractions** (e.g., `1001/24000s` for 23.976fps). Python's `fractions.Fraction` handles this cleanly.
- Transitions are `<transition>` elements between clips. Cross-dissolve uses a well-known UID (`FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265`).
- Audio can be muted per-clip via `<adjust-volume amount="-96dB"/>`.

### DaVinci Resolve Compatibility Notes

| FCPXML Version | Resolve Support |
|----------------|-----------------|
| 1.3–1.8 | Supported across all recent Resolve versions |
| **1.9** | **Recommended** — Resolve exports this version natively |
| 1.10 | Supported since Resolve 18+ |
| 1.11 | Edge of compatibility (Resolve 19.1) |
| 1.12–1.13 | NOT reliably supported — FCP 11 format |

**DaVinci Resolve import quirks:**
- Titles, generators, and FCP-specific effects are stripped on import
- Captions are NOT imported via FCPXML (use separate SRT import)
- Compound clips work but are fragile — **flatten the timeline instead**
- Media must exist in Media Pool before importing the XML for best results (though Resolve can auto-detect media paths from `<asset>` URIs)
- Use absolute `file:///` URIs with percent-encoded paths
- Use `tcFormat="NDF"` (non-drop-frame) — Resolve's default
- Mixed frame rates: set Resolve's conform method to "Final Cut Pro X" in project settings

### Supported Import Formats (DaVinci Resolve 19)

| Format | Notes |
|--------|-------|
| **FCPXML** | Primary interchange format, `.fcpxml` extension |
| FCP7 XML | Legacy format, `.xml` extension |
| AAF | Avid Media Composer interchange |
| EDL | Simple edit decision lists |
| OTIO | OpenTimelineIO, native since Resolve 18.5 |
| DRT | DaVinci Resolve Timeline (single timeline) |

FCPXML is the best choice for our use case: works across FCP + Resolve + Premiere (via import), is simple XML we generate directly, and has the most predictable behavior.

### Why Not OTIO?

OpenTimelineIO is an interesting format but adds complexity without clear benefit:
- Requires `opentimelineio` library dependency
- OTIO→FCPXML adapters have known Resolve compatibility issues (GitHub #499)
- FCPXML is natively understood by more NLEs
- Our timeline structure is simple enough that direct XML generation is cleaner

### Reference: mazsola2k's FCPXML Implementation

The mazsola2k project's `export_resolve.py` (2,143 lines) provides a comprehensive reference for FCPXML generation. Key patterns we can learn from:

- **Rational fraction timing** via Python `fractions.Fraction` with `limit_denominator(1_000_000)` to prevent unbounded growth
- **Asset declarations** with `<media-rep>` for source file linking
- **`<asset-clip>` elements** with `offset` (timeline position), `start` (source in-point), `duration`
- **Cross-dissolve transitions** as `<transition>` elements referencing a shared `<effect>` resource
- **Audio muting** via `<adjust-volume amount="-96dB"/>` on video clips
- **File URI encoding** via `urllib.parse.quote()` with `safe='/'`
- **Watermark overlays** on a separate lane with `<adjust-transform>` and `<adjust-blend>`

Most of mazsola2k's complexity (speed ramping via `<timeMap>`, multi-track background music with keyframe automation, teaser sections, intro/outro assembly) is not needed for our initial implementation. We need the simpler subset: clips on a timeline with in/out points, transitions, and audio control.

---

## VX Data Available for Export

### EditorialStoryboard (models.py)

Each `Segment` provides:
| Field | FCPXML Mapping |
|-------|---------------|
| `clip_id` | Media asset reference |
| `in_sec` | `start` attribute (source in-point) |
| `out_sec` | Derived: `duration` = out_sec - in_sec |
| `transition` | `<transition>` element type |
| `audio_note` | `<adjust-volume>` configuration |
| `text_overlay` | Not mapped in v1 (Resolve strips text effects) |
| `purpose` | Clip `name` annotation for editor reference |
| `description` | Not mapped (editorial metadata only) |

### manifest.json

Each clip entry provides:
| Field | FCPXML Mapping |
|-------|---------------|
| `source_path` | `<asset>` `src` URI |
| `duration_sec` | `<asset>` `duration` |
| `fps_float` | `<format>` `frameDuration` |
| `width`, `height` | `<format>` dimensions |
| `codec` | Informational only |

### Clip Path Resolution

Priority order (from `rough_cut.py:_resolve_clip_source`):
1. `manifest.json` → `clip["source_path"]` (full-res original)
2. `clips/{clip_id}/source/` directory (legacy symlink/copy)
3. `clips/{clip_id}/proxy/` (offline/proxy mode fallback)

---

## Design Decisions

1. **Use `xml.etree.ElementTree`** — no external dependencies. The FCPXML structure is simple enough that OTIO or lxml add complexity without benefit.

2. **Reference original source files** — the FCPXML points to full-res originals (from manifest `source_path`), not proxies. This gives users full-quality editing in Resolve.

3. **Flat timeline** — single `<spine>` with `<asset-clip>` elements. No compound clips (fragile in Resolve).

4. **Support transitions** — map VX's `transition` field:
   | VX transition | FCPXML |
   |---------------|--------|
   | `cut` | No `<transition>` element (hard cut) |
   | `dissolve` | `<transition>` with Cross Dissolve effect |
   | `fade_in` | `<transition>` with Cross Dissolve at segment start (from gap) |
   | `fade_out` | `<transition>` with Cross Dissolve at segment end (to gap) |
   | `j_cut`, `l_cut` | Hard cut (audio overlap requires multi-track, deferred) |

5. **Honor audio_note**:
   | audio_note | FCPXML |
   |------------|--------|
   | `mute` | `<adjust-volume amount="-96dB"/>` |
   | `preserve_dialogue` | No volume adjustment (keep original) |
   | `music_bed` | `<adjust-volume amount="-12dB"/>` (lower for music overlay) |
   | `ambient` | `<adjust-volume amount="-6dB"/>` (slightly lower) |
   | `voice_over` | `<adjust-volume amount="-96dB"/>` (mute for VO) |

6. **Segment naming** — clip names include purpose for editor reference: `"C0059 — hook"`, `"C0073 — action"`.

---

## Implementation Plan

### New file: `src/ai_video_editor/fcpxml_export.py`

Core function: `export_fcpxml(storyboard, editorial_paths, output_path, output_format=None)`

**Structure:**
1. **Build source map** from manifest (reuse `_build_source_map` from `rough_cut.py`)
2. **Read clip metadata** from manifest entries (fps, duration, resolution)
3. **Generate FCPXML tree:**
   - `<resources>`: one `<format>` for the timeline, one `<effect>` for cross-dissolve, one `<asset>` per unique source clip
   - `<project>` → `<sequence>` → `<spine>`:
     - Walk `storyboard.segments` in order
     - For each segment: `<asset-clip>` with `ref`, `offset` (timeline position), `start` (in_sec as fraction), `duration` (segment duration as fraction)
     - Between segments with `transition="dissolve"`: insert `<transition>` element
     - Apply `<adjust-volume>` based on `audio_note`
4. **Write XML** with declaration and DOCTYPE

**Key helpers:**
- `_sec_to_frac(seconds, fps) -> str` — float seconds → rational fraction string (e.g., `"30030/1001s"`)
- `_to_file_uri(path) -> str` — absolute path → percent-encoded `file:///` URI
- `_build_format_element(...)` — create `<format>` resource
- `_build_asset_element(...)` — create `<asset>` resource from manifest clip info
- `_build_asset_clip(...)` — create `<asset-clip>` timeline element

**Reuse from rough_cut.py:**
- `_build_source_map()` (line 30) — clip_id → source path from manifest
- `_resolve_clip_source()` (line 43) — resolve actual file path
- `validate_edl()` (line 81) — validate segments before export

### CLI integration: add `export-xml` subcommand in `cli.py`

```
vx export-xml <project> [--storyboard VERSION] [--composition NAME] [--output PATH]
```

- Follows the same storyboard resolution pattern as `cmd_cut` (composition > --storyboard > latest)
- Default output: `exports/<project>.fcpxml` in the project directory
- Prints the output path so user can import into Resolve

### Files to modify
- `src/ai_video_editor/fcpxml_export.py` — **new file**, core FCPXML generation
- `src/ai_video_editor/cli.py` — add `export-xml` subcommand (~40 lines, mirroring `cmd_cut` pattern)

### Files to reference (read-only)
- `src/ai_video_editor/models.py` — `EditorialStoryboard`, `Segment`
- `src/ai_video_editor/rough_cut.py` — `_build_source_map`, `_resolve_clip_source`, `validate_edl`
- `src/ai_video_editor/config.py` — `EditorialProjectPaths`, `OutputFormat`
- `mazsola2k-ai-video-editor/export_resolve.py` — FCPXML structure patterns

---

## FCPXML Output Structure

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
  <resources>
    <format id="r0" name="FFVideoFormat1920x1080p2997"
            width="1920" height="1080" frameDuration="1001/30000s"/>
    <effect id="r1" name="Cross Dissolve"
            uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>

    <!-- one <asset> per unique source clip used in the storyboard -->
    <asset id="r2" name="20260330094906_C0059.MP4"
           src="file:///Volumes/Seagate%20Hub/VIDEO_LIBRARY/RAW/2026/20260330094906_C0059.MP4"
           start="0s" duration="23525/1000s"
           hasVideo="1" hasAudio="1" format="r0"
           audioChannels="2" audioSources="1">
      <media-rep src="file:///Volumes/Seagate%20Hub/VIDEO_LIBRARY/RAW/2026/20260330094906_C0059.MP4"
                 kind="original-media"/>
    </asset>
    <asset id="r3" name="20260330114125_C0073.MP4" .../>
  </resources>

  <project name="family-trip-hsinchu-2026">
    <sequence format="r0" tcStart="0s" tcFormat="NDF" duration="...">
      <spine>
        <!-- Segment 0: hook from C0059, 5.0s–15.0s -->
        <asset-clip ref="r2" name="C0059 — hook"
                    offset="0s" start="5000/1000s" duration="10000/1000s"
                    format="r0" enabled="1" tcFormat="NDF">
          <adjust-volume amount="-96dB"/>
        </asset-clip>

        <!-- Cross-dissolve transition (1 second) -->
        <transition name="Cross Dissolve" offset="9500/1000s" duration="1000/1000s">
          <filter-video ref="r1"/>
          <filter-audio ref="r1"/>
        </transition>

        <!-- Segment 1: establish from C0073, 0.0s–20.0s -->
        <asset-clip ref="r3" name="C0073 — establish"
                    offset="10000/1000s" start="0s" duration="20000/1000s"
                    format="r0" enabled="1" tcFormat="NDF"/>

        <!-- ... remaining segments ... -->
      </spine>
    </sequence>
  </project>
</fcpxml>
```

---

## Verification

1. **Unit test**: Generate FCPXML from a test storyboard, validate XML structure (well-formed, required elements present, asset refs resolve)
2. **Manual test**: Run `vx export-xml <project>` on an existing project, import the `.fcpxml` into DaVinci Resolve, verify:
   - All clips appear on timeline in correct order
   - In/out points match expected segments
   - Transitions render as cross-dissolves
   - Muted clips have no audio
   - Timeline duration roughly matches `estimated_duration_sec`
3. **Edge cases**: Empty storyboard, single segment, segments from same clip (reused asset), missing source files (warn and skip)

---

## Future Enhancements (Not in v1)

- **Background music track**: Add music assets on a separate audio lane, similar to mazsola2k's implementation
- **Speed ramping**: Support via `<timeMap>` for time-lapse segments
- **Watermark overlay**: Image asset on a separate video lane with `<adjust-blend>`
- **Text overlays / captions**: Export as separate SRT file alongside FCPXML (since Resolve strips FCPXML text effects)
- **J-cut / L-cut audio transitions**: Requires multi-track audio with offset audio clips
- **OTIO export**: Alternative format for broader NLE support
- **Proxy mode**: Generate FCPXML pointing to proxy files for offline editing, with relinking workflow
