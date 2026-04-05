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

**DaVinci Resolve import quirks (battle-tested with Resolve 20):**
- **Bare `<title>` elements are stripped** on import — but titles that reference a known effect resource (e.g., "Middle" Lower Third `.moti` template) via `ref` survive. See Pitfall #11 below.
- Compound clips work but are fragile — **flatten the timeline instead**
- Use `tcFormat="NDF"` (non-drop-frame) — Resolve's default
- Mixed frame rates: set Resolve's conform method to "Final Cut Pro X" in project settings
- Import path: **File > Import > Timeline** (not File > Open or File > Import > Project)

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
| `text_overlay` | `<title>` on lane 1 via "Middle" Lower Third effect (see Pitfall #11) |
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

6. **Segment naming** — asset-clip `name` must match the source filename (see Pitfalls below).

---

## Pitfalls & Lessons Learned (DaVinci Resolve 20)

Hard-won knowledge from debugging FCPXML import failures. These are specific to DaVinci Resolve's FCPXML parser and are NOT documented in Apple's FCPXML reference or Blackmagic's docs.

### 1. Embedded timecodes MUST be in the `start` attribute (critical)

**Symptom:** Resolve imports audio but shows "Media Offline" for video on Sony XAVC clips, while iPhone MOV clips work fine from the same directory.

**Root cause:** Sony cameras embed a running timecode in the `tmcd` track (e.g., `19:13:13:04`). DaVinci Resolve uses the `start` attribute on `<asset>` and `<asset-clip>` to match the media's internal timecode. When we set `start="0/1s"`, Resolve couldn't match the video track (which starts at frame 19:13:13:04), though it could still find the audio track.

**Fix:** Probe each source file with ffprobe for embedded timecodes and convert to FCPXML rational fractions:
```
ffprobe -show_entries stream_tags=timecode → "19:13:13:04"
→ Convert: 19*3600*24 + 13*60*24 + 13*24 + 4 = 1,660,636 frames
→ At 23.976fps: 1,660,636 * 1001/24000 = 415574159/6000s
```

The `<asset-clip>` `start` must be the asset's base timecode PLUS the segment's `in_sec` offset.

Files without embedded timecodes (iPhone MOVs) correctly use `start="0/1s"`.

### 2. No `src` or `uid` on `<asset>` — only `<media-rep>`

**Symptom:** Resolve fails to locate media files despite correct `file:///` URIs.

**Root cause:** DaVinci Resolve 20's FCPXML parser reads the file path exclusively from the `<media-rep src="...">` child element, NOT from `src` or `uid` attributes on the `<asset>` element itself. This contradicts the FCPXML 1.9 spec (which documents `src` on `<asset>`) but matches Resolve's own FCPXML export behavior.

**Fix:** Omit `src` and `uid` from `<asset>`. Provide the file URI only via `<media-rep>`:
```xml
<!-- WRONG (Resolve ignores these) -->
<asset id="r1" name="clip.MP4" src="file:///path/to/clip.MP4" uid="file:///path/to/clip.MP4" .../>

<!-- CORRECT (Resolve reads this) -->
<asset id="r1" name="clip.MP4" ...>
    <media-rep src="file:///path/to/clip.MP4" kind="original-media"/>
</asset>
```

### 3. NTSC frame rates need standard fraction representations

**Symptom:** Resolve shows wrong frame rate or rejects format definitions.

**Root cause:** `Fraction(29.97)` in Python produces `100/2997` (from the float's binary representation), not the standard `1001/30000`. Resolve expects the industry-standard NTSC fractions.

**Fix:** Lookup table for common NTSC rates:
| Float fps | Standard frameDuration |
|-----------|----------------------|
| 23.976 | `1001/24000s` |
| 29.97 | `1001/30000s` |
| 59.94 | `1001/60000s` |

### 4. Asset-clip `name` must be the source filename

**Symptom:** Resolve creates "Media Offline" clips in the Media Pool with creative labels (e.g., `"C0003 — establish"`) instead of linking to the source file.

**Root cause:** Resolve uses the `<asset-clip>` `name` attribute for media matching/relinking in the Media Pool. If the name doesn't match the source filename, Resolve can't link the media even when the `ref` correctly points to a valid `<asset>`.

**Fix:** Set `name` on `<asset-clip>` to the actual source filename (e.g., `"20260315162915_C0003.MP4"`), matching the `<asset>` `name`.

### 5. Use `FFVideoFormatRateUndefined` for mixed-fps source assets

**Symptom:** Clips with non-matching fps formats fail to link.

**Root cause:** When each clip gets its own `<format>` element with a specific fps (e.g., 23.976fps for Sony, 30fps for iPhone), Resolve may fail to link clips whose format doesn't match the timeline. The mazsola2k reference project uses only two formats: the timeline format and `FFVideoFormatRateUndefined` for everything else.

**Fix:** Use the timeline format for matching clips, and `FFVideoFormatRateUndefined` for clips with different dimensions. Resolve detects the actual format from the file itself.

### 6. Auto-detect timeline format from source footage

**Symptom:** Timeline resolution defaults to 1080p 29.97fps when source footage is 4K 23.976fps.

**Root cause:** The rough cut `OutputFormat` defaults (1080p 29.97fps) are designed for quick preview rendering, not for NLE export where you want native resolution.

**Fix:** Auto-detect the dominant resolution and fps from the manifest clips using majority voting. For NLE export, the timeline should match the source footage's native format.

### 7. `<library>/<event>` wrapper is required

**Symptom:** Resolve shows "file type not supported" on import.

**Root cause:** FCPXML files with `<project>` directly under `<fcpxml>` (without the `<library>/<event>` wrapper) are not recognized by Resolve's "Import Timeline" dialog.

**Fix:** Always wrap in `<library><event name="..."><project name="...">`.

### 8. Duplicate clips in Resolve Media Pool (expected behavior)

**Symptom:** Clips used on the timeline appear twice in the Media Pool — once as the master clip, once as a "subclip."

**Root cause:** When an `<asset-clip>` has a different `start` value (base timecode + segment `in_sec` offset) than its referenced `<asset>`, Resolve 20 creates a separate subclip entry in the Media Pool. This is inherent to how Resolve handles FCPXML sub-clipping — it's distinguishing "full source clip" from "trimmed timeline clip."

**Not a bug.** The timeline works correctly. Clips used in multiple segments only create one subclip (not N), so it's not proportional to usage count. This is cosmetic and matches behavior when manually trimming clips in Resolve and exporting FCPXML.

### 9. Only manifest clips are exported (source directory is not scanned)

**Symptom:** Video files in the source directory that were never ingested into the VX project are missing from the FCPXML Media Pool.

**By design.** The FCPXML export uses `manifest.json` as the source of truth, not the filesystem. Files outside the VX project scope are not included. Users should add missing footage via `vx new` or preprocessing if they want it in the export.

### 10. `editorial_reasoning` must have a default value

**Symptom:** TUI crashes silently when exporting older projects.

**Root cause:** The `editorial_reasoning` field was added to `EditorialStoryboard` as a required field, but older storyboard JSONs don't have it. Pydantic validation fails with no visible error in the TUI.

**Fix:** Add `default=""` to the field definition. Wrap TUI export in try/except with user-visible error messages.

### 11. Title elements require an effect resource reference to survive Resolve import

**Symptom:** `<title>` elements in the FCPXML are silently stripped when imported into Resolve. No titles appear on the timeline.

**Root cause:** DaVinci Resolve strips bare `<title>` elements that lack a `ref` attribute pointing to a known effect resource. A title without `ref` is treated as an FCP-specific generator and discarded.

**Fix:** Register the "Middle" Lower Third effect as a `<effect>` resource with its `.moti` UID, and reference it from every `<title>` element:

```xml
<!-- In <resources>: -->
<effect id="r_title"
        uid=".../Titles.localized/Lower Thirds.localized/Middle.localized/Middle.moti"
        name="Middle"/>

<!-- In <asset-clip>: -->
<title ref="r_title" offset="..." enabled="1" start="0/30s"
       lane="1" name="overlay text" duration="3/1s">
    <text roll-up-height="0">
        <text-style ref="ts0">overlay text</text-style>
    </text>
    <text roll-up-height="0"/>  <!-- empty second field (Middle template) -->
    <text-style-def id="ts0">
        <text-style italic="0" alignment="center" fontColor="1 1 1 1"
                    fontSize="59" bold="1" strokeColor="0 0 0 1"
                    lineSpacing="1" strokeWidth="0" font="Open Sans"/>
    </text-style-def>
    <adjust-conform type="fit"/>
    <adjust-transform position="0 -8.148148" anchor="0 0" scale="1 1"/>
</title>
```

**Critical structural requirements:**
- `<text roll-up-height="0">` must come BEFORE `<text-style-def>` (Resolve parses in order)
- The "Middle" template has two `<text>` fields — second can be empty
- `<adjust-conform type="fit"/>` and `<adjust-transform>` are required
- `enabled="1"` must be explicit on the `<title>` element

### 12. Title positioning in Resolve uses percentage-based transform values

**Symptom:** Titles appear at the wrong vertical position despite correct `<adjust-transform>` values.

**Root cause:** Resolve multiplies the `<adjust-transform>` Y position value by `(timeline_height / 100)`. So for a 4K timeline (2160px), a Y value of `-8.148148` produces `-8.148148 × 21.6 ≈ -176px` from center — placing text in the lower third.

**Calibrated positions (4K / 2160p timeline):**
| Position | Y value | Pixel offset | Use case |
|----------|---------|-------------|----------|
| Lower third | `-8.148148` | ≈ -176px | Monologue overlays (lane 1) |
| Upper area | `62.5` | ≈ +1350px | Speech captions avoiding monologue (lane 2) |
| Center | `0` | 0px | Title cards (unused) |

### 13. Multi-layer text overlays use separate lanes

**Symptom:** Monologue and speech captions overlap visually when both are present.

**Solution:** Use two lanes with distinct positioning:

| Layer | Lane | Font size | Position Y | Content |
|-------|------|-----------|-----------|---------|
| Monologue | `lane="1"` | 59 | `-8.148148` | Text overlays from MonologuePlan |
| Captions | `lane="2"` | 55 | `-8.148148` or `62.5` | Speech from transcripts |

Captions detect collision with monologue intervals using the same overlap logic as `rough_cut.py`. When a caption's time range overlaps a monologue overlay on the same segment, it moves to the upper position (`62.5`) to avoid visual collision. Non-colliding captions stay at the lower-third position.

Caption titles are only generated when a monologue plan is present (matching `rough_cut.py`'s `burn_captions = monologue is not None` pattern).

A companion SRT export (`timeline_monologue.srt` + `timeline_subtitles.srt`) is also produced for editors who prefer subtitle-track workflows.

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

## FCPXML Output Structure (verified working with Resolve 20)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
  <resources>
    <!-- Timeline format (auto-detected from dominant source clip format) -->
    <format id="r0" name="FFVideoFormat3840x2160p2398"
            width="3840" height="2160" frameDuration="1001/24000s"/>
    <effect id="r1" name="Cross Dissolve"
            uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>
    <!-- FFVideoFormatRateUndefined for clips with different dimensions -->
    <format id="r4" name="FFVideoFormatRateUndefined"
            width="2160" height="3840" frameDuration="1001/24000s"/>

    <!-- ALL clips from manifest (not just timeline-used ones) -->
    <!-- No src/uid on asset — only media-rep. start = embedded timecode. -->
    <asset id="r2" name="20260315162915_C0003.MP4"
           duration="31031/1000s" audioChannels="2"
           start="415574159/6000s"
           format="r0" hasVideo="1" audioSources="1" hasAudio="1">
      <media-rep src="file:///Volumes/Seagate%20Hub/family-hiking-in-Shipai/20260315162915_C0003.MP4"
                 kind="original-media"/>
    </asset>
    <!-- iPhone MOV (no embedded timecode → start="0/1s") -->
    <asset id="r3" name="IMG_9346.MOV"
           duration="36/1s" audioChannels="2"
           start="0/1s"
           format="r0" hasVideo="1" audioSources="1" hasAudio="1">
      <media-rep src="file:///Volumes/Seagate%20Hub/family-hiking-in-Shipai/IMG_9346.MOV"
                 kind="original-media"/>
    </asset>
  </resources>

  <library>
    <event name="my-project">
      <project name="my-project">
        <sequence format="r0" tcStart="0s" tcFormat="NDF" duration="...">
          <spine>
            <!-- asset-clip name = source filename (NOT creative label) -->
            <!-- start = asset base timecode + segment in_sec offset -->
            <asset-clip ref="r3" name="IMG_9346.MOV"
                        offset="0/30s" start="8/1s" duration="8/1s"
                        format="r0" enabled="1" tcFormat="NDF">
              <adjust-volume amount="-6dB"/>
            </asset-clip>

            <asset-clip ref="r2" name="20260315162915_C0003.MP4"
                        offset="8/1s" start="415682159/6000s" duration="13013/1000s"
                        format="r0" enabled="1" tcFormat="NDF">
              <adjust-volume amount="-6dB"/>
            </asset-clip>

            <!-- ... remaining segments ... -->
          </spine>
        </sequence>
      </project>
    </event>
  </library>
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
- ~~**Text overlays / captions**~~: ✅ Implemented — monologue overlays as `<title>` elements (lane 1) + speech captions (lane 2) with collision avoidance, plus companion SRT files
- **J-cut / L-cut audio transitions**: Requires multi-track audio with offset audio clips
- **OTIO export**: Alternative format for broader NLE support
- **Proxy mode**: Generate FCPXML pointing to proxy files for offline editing, with relinking workflow
