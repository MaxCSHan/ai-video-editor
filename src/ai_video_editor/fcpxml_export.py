"""FCPXML export for DaVinci Resolve / Final Cut Pro.

Generates FCPXML v1.9 from an EditorialStoryboard, enabling professional NLE editing
of AI-assembled rough cuts. Clips reference original source files at full resolution.

Usage:
    from .fcpxml_export import export_fcpxml
    export_fcpxml(storyboard, editorial_paths, output_path)
"""

import json
import logging
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from .config import EditorialProjectPaths, OutputFormat
from .models import EditorialStoryboard, MonologuePlan, Segment, TextOverlayStyle

log = logging.getLogger(__name__)

# Cross Dissolve effect UID (standard across FCP/Resolve)
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"

# Default transition duration in seconds
DEFAULT_TRANSITION_SEC = 1.0

# Fixed monologue style — matches rough_cut.py's _MONOLOGUE_STYLE
_MONOLOGUE_STYLE = TextOverlayStyle()

# Audio volume mapping from storyboard audio_note to dB adjustment
AUDIO_VOLUME_MAP: dict[str, str | None] = {
    "mute": "-96dB",
    "voice_over": "-96dB",
    "music_bed": "-12dB",
    "ambient": "-6dB",
    "preserve_dialogue": None,  # no adjustment
    "": None,
}


# Standard NTSC frame rates — float fps values don't map to exact fractions,
# so we detect common rates and use the industry-standard representations
# that DaVinci Resolve and Final Cut Pro expect.
_NTSC_FPS_MAP: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "59.94": Fraction(60000, 1001),
}

_FRAC_LIMIT = 1_000_000


def _fps_to_fraction(fps: float) -> Fraction:
    """Convert fps float to exact Fraction, using NTSC lookup for common rates."""
    key = f"{fps:.3f}".rstrip("0").rstrip(".")
    if key in _NTSC_FPS_MAP:
        return _NTSC_FPS_MAP[key]
    # Also check 2-decimal form
    key2 = f"{fps:.2f}".rstrip("0").rstrip(".")
    if key2 in _NTSC_FPS_MAP:
        return _NTSC_FPS_MAP[key2]
    return Fraction(fps).limit_denominator(_FRAC_LIMIT)


def _sec_to_frac(seconds: float, fps: float) -> str:
    """Convert float seconds to FCPXML rational fraction string.

    Uses the fps timebase for frame-aligned fractions that NLEs expect.

    Examples:
        _sec_to_frac(10.0, 29.97) -> "300300/30000s"
        _sec_to_frac(0.0, 29.97) -> "0/30000s"
    """
    if seconds == 0.0:
        fps_frac = _fps_to_fraction(fps)
        # Use the fps denominator as our timebase for consistency
        frame_dur = Fraction(1) / fps_frac
        return f"0/{frame_dur.limit_denominator(_FRAC_LIMIT).denominator}s"

    frac = Fraction(seconds).limit_denominator(_FRAC_LIMIT)
    fps_frac = _fps_to_fraction(fps)
    frame_dur = Fraction(1) / fps_frac
    # Snap to nearest frame boundary
    frames = round(float(frac / frame_dur))
    result = (Fraction(frames) * frame_dur).limit_denominator(_FRAC_LIMIT)
    return f"{result.numerator}/{result.denominator}s"


def _to_file_uri(path: Path) -> str:
    """Convert an absolute path to a file:/// URI using Python's native method."""
    return path.resolve().as_uri()


def _probe_start_timecode(source_path: Path, fps: float) -> str | None:
    """Read the embedded start timecode from a video file and convert to FCPXML fraction.

    Sony XAVC files embed timecodes like 19:13:13:04 in a tmcd track.
    DaVinci Resolve uses this to match media — the asset `start` attribute must match.
    Returns None if no timecode is found (iPhone MOVs, etc.).
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "stream_tags=timecode",
                str(source_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            tc = stream.get("tags", {}).get("timecode")
            if tc:
                return _timecode_to_frac(tc, fps)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def _timecode_to_frac(timecode: str, fps: float) -> str:
    """Convert HH:MM:SS:FF timecode to FCPXML rational fraction string.

    Example: "19:13:13:04" at 23.976fps -> "415574159/6000s"
    """
    parts = timecode.replace(";", ":").split(":")
    if len(parts) != 4:
        return "0/1s"
    h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])

    # NDF timecode: integer fps (24 for 23.976, 30 for 29.97)
    fps_frac = _fps_to_fraction(fps)
    fps_int = round(float(fps_frac))  # 24 for 23.976, 30 for 29.97

    total_frames = h * 3600 * fps_int + m * 60 * fps_int + s * fps_int + f
    frame_dur = Fraction(1) / fps_frac
    result = (Fraction(total_frames) * frame_dur).limit_denominator(_FRAC_LIMIT)
    return f"{result.numerator}/{result.denominator}s"


def _build_source_map(editorial_paths: EditorialProjectPaths) -> dict[str, Path]:
    """Build clip_id -> original source path map from the master manifest."""
    manifest_path = editorial_paths.master_manifest
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        return {
            clip["clip_id"]: Path(clip["source_path"])
            for clip in manifest.get("clips", [])
            if "source_path" in clip
        }
    return {}


def _resolve_clip_source(
    clip_id: str,
    editorial_paths: EditorialProjectPaths,
    source_map: dict[str, Path],
) -> Path | None:
    """Resolve the original source file for a clip."""
    if clip_id in source_map:
        p = source_map[clip_id]
        if p.exists():
            return p

    # Fallback: legacy source/ dir
    clip_paths = editorial_paths.clip_paths(clip_id)
    source_dir = clip_paths.source
    if source_dir.exists():
        files = [f for f in source_dir.iterdir() if f.is_file()]
        if files:
            return files[0]

    return None


def _read_manifest_clips(editorial_paths: EditorialProjectPaths) -> dict[str, dict]:
    """Read clip metadata from the master manifest, keyed by clip_id."""
    manifest_path = editorial_paths.master_manifest
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text())
    return {clip["clip_id"]: clip for clip in manifest.get("clips", [])}


def _frame_duration_str(fps: float) -> str:
    """Compute FCPXML frameDuration from fps (e.g. 29.97 -> '1001/30000s')."""
    fps_frac = _fps_to_fraction(fps)
    frame_dur = (Fraction(1) / fps_frac).limit_denominator(_FRAC_LIMIT)
    return f"{frame_dur.numerator}/{frame_dur.denominator}s"


def _format_name(width: int, height: int, fps: float) -> str:
    """Generate a format name string like 'FFVideoFormat1920x1080p2997'."""
    # Use integer representation: 29.97 -> "2997", 24.0 -> "24", 30.0 -> "30"
    if fps == int(fps):
        fps_str = str(int(fps))
    else:
        fps_str = f"{fps:.2f}".replace(".", "")
    return f"FFVideoFormat{width}x{height}p{fps_str}"


def _detect_dominant_format(manifest_clips: dict[str, dict]) -> OutputFormat:
    """Pick timeline format from the most common resolution + fps across clips."""
    from collections import Counter

    combos = Counter()
    for clip in manifest_clips.values():
        w = clip.get("display_width", clip.get("width", 1920))
        h = clip.get("display_height", clip.get("height", 1080))
        fps = clip.get("fps_float", 29.97)
        combos[(w, h, fps)] += 1

    if not combos:
        return OutputFormat()

    (w, h, fps), _ = combos.most_common(1)[0]
    return OutputFormat(width=w, height=h, fps=fps)


def _add_title_overlay(
    parent_clip: ET.Element,
    overlay,
    clip_start: Fraction,
    clip_fps: float,
    title_effect_id: str,
) -> None:
    """Add a monologue text overlay as a connected title on an asset-clip.

    Uses the "Middle" Lower Third effect template which DaVinci Resolve
    recognises on FCPXML import.  Structure matches a known-working Resolve
    round-trip export (see example/family-hiking-in-Shipai.fcpxml).

    The title is placed on lane 1 (above the video).  ``offset`` is relative
    to the parent clip's local timeline (clip ``start`` + overlay appear_at).
    """
    style = _MONOLOGUE_STYLE

    text = overlay.text
    if style.case == "lowercase":
        text = text.lower()
    elif style.case == "sentence":
        text = text.capitalize()

    # Title offset is relative to the parent clip's start attribute
    title_offset = clip_start + Fraction(overlay.appear_at).limit_denominator(_FRAC_LIMIT)
    title_offset = title_offset.limit_denominator(_FRAC_LIMIT)
    title_dur = Fraction(overlay.duration_sec).limit_denominator(_FRAC_LIMIT)

    title_el = ET.SubElement(
        parent_clip,
        "title",
        ref=title_effect_id,
        offset=f"{title_offset.numerator}/{title_offset.denominator}s",
        enabled="1",
        start=_sec_to_frac(0, clip_fps),
        lane="1",
        name=text[:40],
        duration=_sec_to_frac(float(title_dur), clip_fps),
    )

    # --- Text content (must come before text-style-def to match Resolve's format) ---
    # The "Middle" Lower Third template has two text fields.
    # Field 1: the monologue overlay text.  Field 2: left empty.
    text_el = ET.SubElement(title_el, "text", **{"roll-up-height": "0"})
    ts_id = f"ts-mono-{overlay.index}"
    text_span = ET.SubElement(text_el, "text-style", ref=ts_id)
    text_span.text = text

    # Empty second text field (required by Middle template)
    ET.SubElement(title_el, "text", **{"roll-up-height": "0"})

    # --- Style definition ---
    if style.alignment == "center":
        alignment = "center"
    elif style.alignment == "right":
        alignment = "right"
    else:
        alignment = "left"

    ts_def = ET.SubElement(title_el, "text-style-def", id=ts_id)
    ET.SubElement(
        ts_def,
        "text-style",
        font="Open Sans",
        fontSize="59",
        fontColor="1 1 1 1",
        bold="1",
        italic="0",
        alignment=alignment,
        strokeColor="0 0 0 1",
        strokeWidth="0",
        lineSpacing="1",
    )

    # Conform + transform to lower-third position (required for Resolve import).
    # Resolve multiplies the Y value by (timeline_height / 100), so for a 4K
    # timeline (2160px): -8.148148 * 21.6 ≈ -176px, placing text in the lower third.
    ET.SubElement(title_el, "adjust-conform", type="fit")
    ET.SubElement(
        title_el,
        "adjust-transform",
        position="0 -8.148148",
        anchor="0 0",
        scale="1 1",
    )


def _add_caption_title(
    parent_clip: ET.Element,
    text: str,
    caption_start: Fraction,
    caption_end: Fraction,
    clip_start: Fraction,
    clip_fps: float,
    title_effect_id: str,
    caption_index: int,
    upper: bool = False,
) -> None:
    """Add a speech caption as a connected title on an asset-clip.

    When *upper* is True (caption collides with a monologue overlay), the title
    is placed at the upper position (lane 2, position ``0 62.5``, fontSize 55).
    Otherwise it sits at the default lower-third position (lane 2, position
    ``0 -8.148148``, fontSize 55).
    """
    # Caption offset is clip_start + segment-relative start of the cue
    title_offset = (clip_start + caption_start).limit_denominator(_FRAC_LIMIT)
    title_dur = (caption_end - caption_start).limit_denominator(_FRAC_LIMIT)

    cue_text = text.lower()

    title_el = ET.SubElement(
        parent_clip,
        "title",
        ref=title_effect_id,
        offset=f"{title_offset.numerator}/{title_offset.denominator}s",
        enabled="1",
        start=_sec_to_frac(0, clip_fps),
        lane="2",
        name=cue_text[:40],
        duration=_sec_to_frac(float(title_dur), clip_fps),
    )

    ts_id = f"ts-cap-{caption_index}"

    # Text content (before style def, matching Resolve format)
    text_el = ET.SubElement(title_el, "text", **{"roll-up-height": "0"})
    text_span = ET.SubElement(text_el, "text-style", ref=ts_id)
    text_span.text = cue_text

    # Empty second text field (Middle template)
    ET.SubElement(title_el, "text", **{"roll-up-height": "0"})

    # Style definition
    ts_def = ET.SubElement(title_el, "text-style-def", id=ts_id)
    ET.SubElement(
        ts_def,
        "text-style",
        font="Open Sans",
        fontSize="55",
        fontColor="1 1 1 1",
        bold="1",
        italic="0",
        alignment="center",
        strokeColor="0 0 0 1",
        strokeWidth="0",
        lineSpacing="1",
    )

    # Position: upper when colliding with monologue, lower-third otherwise
    y_pos = "62.5" if upper else "-8.148148"
    ET.SubElement(title_el, "adjust-conform", type="fit")
    ET.SubElement(
        title_el,
        "adjust-transform",
        position=f"0 {y_pos}",
        anchor="0 0",
        scale="1 1",
    )


def export_fcpxml(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    output_path: Path,
    output_format: OutputFormat | None = None,
    project_name: str | None = None,
    monologue: MonologuePlan | None = None,
) -> Path:
    """Generate an FCPXML v1.9 file from an EditorialStoryboard.

    When *monologue* is provided, overlays are embedded as ``<title>`` elements
    referencing the "Middle" Lower Third effect (confirmed working in DaVinci
    Resolve import).  A companion monologue SRT is also produced by
    ``export_srt_files`` for editors who prefer subtitle-track workflows.

    Args:
        storyboard: The editorial storyboard with segments to export.
        editorial_paths: Project paths for resolving clip sources.
        output_path: Where to write the .fcpxml file.
        output_format: Target format (defaults to 1920x1080 29.97fps).
        project_name: Name for the FCPXML project element.
        monologue: Optional monologue plan — overlays are exported as title
            elements on lane 1 above the video timeline.

    Returns:
        The output path written.
    """
    name = project_name or editorial_paths.root.name

    source_map = _build_source_map(editorial_paths)
    manifest_clips = _read_manifest_clips(editorial_paths)

    # Auto-detect timeline format from the dominant source clip resolution/fps,
    # since NLE editing should happen at native resolution (not the rough cut's 1080p).
    if output_format:
        fmt = output_format
    elif manifest_clips:
        fmt = _detect_dominant_format(manifest_clips)
    else:
        fmt = OutputFormat()

    # Collect ALL clips from manifest — user wants full footage in Media Pool,
    # not just the clips used in the timeline. Timeline-used clips come first.
    used_clip_ids = list(dict.fromkeys(seg.clip_id for seg in storyboard.segments))
    all_manifest_ids = [c["clip_id"] for c in manifest_clips.values()]
    # Merge: used clips first, then remaining manifest clips
    all_clip_ids = list(dict.fromkeys(used_clip_ids + all_manifest_ids))

    # Resolve sources for all clips
    clip_sources: dict[str, Path] = {}
    for clip_id in all_clip_ids:
        source = _resolve_clip_source(clip_id, editorial_paths, source_map)
        if source:
            clip_sources[clip_id] = source
        elif clip_id in used_clip_ids:
            log.warning("Source not found for clip %s — will be skipped in timeline", clip_id)

    # --- Build XML tree ---
    fcpxml = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(fcpxml, "resources")

    # Timeline format (r0)
    timeline_format_id = "r0"
    ET.SubElement(
        resources,
        "format",
        id=timeline_format_id,
        name=_format_name(fmt.width, fmt.height, fmt.fps),
        width=str(fmt.width),
        height=str(fmt.height),
        frameDuration=_frame_duration_str(fmt.fps),
    )

    # Cross Dissolve effect (r1)
    dissolve_effect_id = "r1"
    ET.SubElement(
        resources,
        "effect",
        id=dissolve_effect_id,
        name="Cross Dissolve",
        uid=CROSS_DISSOLVE_UID,
    )

    # "Middle" Lower Third title effect — confirmed working in Resolve import.
    # The .moti UID is the standard FCP/Resolve built-in template path.
    title_effect_id = ""
    overlay_map: dict[int, list] = {}
    if monologue:
        for ov in monologue.overlays:
            overlay_map.setdefault(ov.segment_index, []).append(ov)
    if overlay_map:
        title_effect_id = "r_title"
        ET.SubElement(
            resources,
            "effect",
            id=title_effect_id,
            uid=".../Titles.localized/Lower Thirds.localized/"
            "Middle.localized/Middle.moti",
            name="Middle",
        )

    # Asset elements — one per source clip, ALL clips from manifest (r2, r3, ...)
    asset_id_map: dict[str, str] = {}  # clip_id -> resource id
    asset_start_map: dict[str, str] = {}  # clip_id -> start timecode fraction
    asset_fps_map: dict[str, float] = {}  # clip_id -> native fps
    format_cache: dict[tuple, str] = {}  # (w, h) -> format id
    next_rid = 2

    for clip_id in all_clip_ids:
        if clip_id not in clip_sources:
            continue

        rid = f"r{next_rid}"
        next_rid += 1
        asset_id_map[clip_id] = rid

        source_path = clip_sources[clip_id]
        clip_meta = manifest_clips.get(clip_id, {})

        # Use manifest metadata if available, else sensible defaults
        clip_fps = clip_meta.get("fps_float", fmt.fps)
        clip_duration = clip_meta.get("duration_sec", 0)
        clip_width = clip_meta.get("display_width", clip_meta.get("width", fmt.width))
        clip_height = clip_meta.get("display_height", clip_meta.get("height", fmt.height))

        # Use a single "source media" format for all assets.
        # DaVinci Resolve detects actual format from the file — per-asset format
        # declarations with non-matching fps cause media linking failures in Resolve 20.
        # Following mazsola2k's pattern: use FFVideoFormatRateUndefined for source assets.
        if (clip_width, clip_height) == (fmt.width, fmt.height):
            clip_format_id = timeline_format_id
        else:
            undef_key = (clip_width, clip_height)
            if undef_key in format_cache:
                clip_format_id = format_cache[undef_key]
            else:
                clip_format_id = f"r{next_rid}"
                next_rid += 1
                format_cache[undef_key] = clip_format_id
                ET.SubElement(
                    resources,
                    "format",
                    id=clip_format_id,
                    name="FFVideoFormatRateUndefined",
                    width=str(clip_width),
                    height=str(clip_height),
                    frameDuration=_frame_duration_str(fmt.fps),
                )

        file_uri = _to_file_uri(source_path)
        duration_str = _sec_to_frac(clip_duration, clip_fps) if clip_duration > 0 else "0s"

        # Read embedded timecode (critical for Sony XAVC media linking in Resolve)
        tc_start = _probe_start_timecode(source_path, clip_fps)
        start_str = tc_start if tc_start else "0/1s"

        # Match Resolve's own FCPXML export: no src/uid on asset, only media-rep
        asset_attrs = {
            "id": rid,
            "name": source_path.name,
            "duration": duration_str,
            "audioChannels": "2",
            "start": start_str,
            "format": clip_format_id,
            "hasVideo": "1",
            "audioSources": "1",
            "hasAudio": "1",
        }
        asset_el = ET.SubElement(resources, "asset", **asset_attrs)
        ET.SubElement(asset_el, "media-rep", src=file_uri, kind="original-media")
        asset_start_map[clip_id] = start_str
        asset_fps_map[clip_id] = clip_fps

    # --- Library / Event / Project / Sequence / Spine ---
    library_el = ET.SubElement(fcpxml, "library")
    event_el = ET.SubElement(library_el, "event", name=name)
    project_el = ET.SubElement(event_el, "project", name=name)

    # Compute total timeline duration accounting for transitions
    timeline_duration = _compute_timeline_duration(storyboard.segments)
    timeline_dur_str = _sec_to_frac(timeline_duration, fmt.fps)

    sequence = ET.SubElement(
        project_el,
        "sequence",
        format=timeline_format_id,
        tcStart="0s",
        tcFormat="NDF",
        duration=timeline_dur_str,
    )
    spine = ET.SubElement(sequence, "spine")

    # Load transcripts for caption titles when monologue is present
    # (mirrors rough_cut.py: burn_captions=monologue is not None)
    caption_transcripts: dict[str, list] = {}
    if overlay_map:
        from .versioning import resolve_transcript_path

        for clip_id in used_clip_ids:
            clip_root = editorial_paths.clips_dir / clip_id
            tp = resolve_transcript_path(clip_root)
            if tp:
                data = json.loads(tp.read_text())
                if data.get("has_speech", False):
                    caption_transcripts[clip_id] = data.get("segments", [])

    # Build monologue intervals per segment for caption collision detection
    mono_intervals_by_seg: dict[int, list[tuple[float, float]]] = {}
    if monologue:
        for ov in monologue.overlays:
            mono_intervals_by_seg.setdefault(ov.segment_index, []).append(
                (ov.appear_at, ov.appear_at + ov.duration_sec)
            )

    # Walk segments and build timeline
    timeline_offset = Fraction(0)
    caption_counter = 0

    for i, seg in enumerate(storyboard.segments):
        if seg.clip_id not in asset_id_map:
            log.warning("Skipping segment #%d — no asset for clip %s", seg.index, seg.clip_id)
            continue

        seg_duration = Fraction(seg.out_sec - seg.in_sec).limit_denominator(1_000_000)
        if seg_duration <= 0:
            log.warning("Skipping segment #%d — zero or negative duration", seg.index)
            continue

        # Check if we need a transition INTO this segment
        needs_transition = seg.transition in ("dissolve", "fade_in") and i > 0
        transition_dur = Fraction(DEFAULT_TRANSITION_SEC).limit_denominator(1_000_000)

        if needs_transition:
            # Transition overlaps the end of previous clip and start of this clip
            # The transition element is placed before the asset-clip
            trans_offset_sec = float(timeline_offset) - float(transition_dur) / 2
            trans_el = ET.SubElement(
                spine,
                "transition",
                name="Cross Dissolve",
                offset=_sec_to_frac(max(0, trans_offset_sec), fmt.fps),
                duration=_sec_to_frac(float(transition_dur), fmt.fps),
            )
            ET.SubElement(trans_el, "filter-video", ref=dissolve_effect_id)
            ET.SubElement(trans_el, "filter-audio", ref=dissolve_effect_id)

        # Build asset-clip
        # `start` must be asset timecode base + segment in_sec offset
        # (Resolve uses this to find the right frame within the source media)
        clip_name = clip_sources[seg.clip_id].name
        asset_base = asset_start_map.get(seg.clip_id, "0/1s")
        clip_fps = asset_fps_map.get(seg.clip_id, fmt.fps)

        # Parse asset base timecode and add segment in_sec
        base_parts = asset_base.rstrip("s").split("/")
        base_frac = Fraction(int(base_parts[0]), int(base_parts[1])) if len(base_parts) == 2 else Fraction(0)
        in_frac = Fraction(seg.in_sec).limit_denominator(_FRAC_LIMIT)
        clip_start = (base_frac + in_frac).limit_denominator(_FRAC_LIMIT)
        clip_start_str = f"{clip_start.numerator}/{clip_start.denominator}s"

        clip_el = ET.SubElement(
            spine,
            "asset-clip",
            ref=asset_id_map[seg.clip_id],
            name=clip_name,
            offset=_sec_to_frac(float(timeline_offset), fmt.fps),
            start=clip_start_str,
            duration=_sec_to_frac(float(seg_duration), clip_fps),
            format=timeline_format_id,
            enabled="1",
            tcFormat="NDF",
        )

        # Apply audio volume adjustment
        volume = AUDIO_VOLUME_MAP.get(seg.audio_note)
        if volume is not None:
            ET.SubElement(clip_el, "adjust-volume", amount=volume)

        # Add monologue text overlays as connected title clips above the video
        seg_overlays = overlay_map.get(seg.index)
        if seg_overlays:
            for ov in seg_overlays:
                _add_title_overlay(clip_el, ov, clip_start, clip_fps, title_effect_id)

        # Add speech caption titles (lane 2) when monologue is present
        seg_mono_intervals = mono_intervals_by_seg.get(seg.index)
        transcript_segs = caption_transcripts.get(seg.clip_id)
        if transcript_segs and overlay_map:
            for tseg in transcript_segs:
                if tseg.get("type", "speech") != "speech":
                    continue
                cue_text = tseg.get("text", "").strip()
                if not cue_text:
                    continue

                cue_start = tseg.get("start", 0.0)
                cue_end = tseg.get("end", 0.0)

                # Skip cues outside the segment window
                if cue_end <= seg.in_sec or cue_start >= seg.out_sec:
                    continue

                # Clamp to segment boundaries and convert to segment-relative
                local_start = Fraction(max(0.0, cue_start - seg.in_sec)).limit_denominator(
                    _FRAC_LIMIT
                )
                local_end = Fraction(
                    min(float(seg_duration), cue_end - seg.in_sec)
                ).limit_denominator(_FRAC_LIMIT)

                if local_end - local_start < Fraction(2, 10):
                    continue

                # Check for collision with monologue overlays
                is_upper = False
                if seg_mono_intervals:
                    ls = float(local_start)
                    le = float(local_end)
                    for m_start, m_end in seg_mono_intervals:
                        if ls < m_end and le > m_start:
                            is_upper = True
                            break

                _add_caption_title(
                    clip_el,
                    cue_text,
                    local_start,
                    local_end,
                    clip_start,
                    clip_fps,
                    title_effect_id,
                    caption_counter,
                    upper=is_upper,
                )
                caption_counter += 1

        # Check for fade_out on this segment (transition at end)
        if seg.transition == "fade_out":
            fade_offset = float(timeline_offset + seg_duration) - float(transition_dur) / 2
            trans_el = ET.SubElement(
                spine,
                "transition",
                name="Cross Dissolve",
                offset=_sec_to_frac(max(0, fade_offset), fmt.fps),
                duration=_sec_to_frac(float(transition_dur), fmt.fps),
            )
            ET.SubElement(trans_el, "filter-video", ref=dissolve_effect_id)
            ET.SubElement(trans_el, "filter-audio", ref=dissolve_effect_id)

        timeline_offset += seg_duration

    # --- Write XML ---
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="  ")

    with open(output_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(b"<!DOCTYPE fcpxml>\n")
        tree.write(f, encoding="UTF-8", xml_declaration=False)

    overlay_count = sum(len(ovs) for ovs in overlay_map.values())
    parts = []
    if overlay_count:
        parts.append(f"{overlay_count} monologue overlays")
    if caption_counter:
        parts.append(f"{caption_counter} captions")
    if parts:
        log.info("FCPXML written to %s (%s)", output_path, ", ".join(parts))
    else:
        log.info("FCPXML written to %s", output_path)
    return output_path


def export_srt_files(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    output_dir: Path,
    monologue: MonologuePlan | None = None,
) -> list[Path]:
    """Export timeline-aligned SRT files alongside the FCPXML.

    Produces up to three kinds of SRT:

    1. **Monologue SRT** (``timeline_monologue.srt``) — text overlays from the
       monologue plan, timed to the assembled timeline.  Import this as a
       separate subtitle track in DaVinci Resolve so it lives on its own lane.
    2. **Caption SRT** (``timeline_subtitles.srt``) — speech-only captions from
       transcripts, remapped to timeline offsets.  Plain text (no speaker names).
    3. **Per-clip SRT** files — raw per-clip transcripts for reference.

    Because DaVinci Resolve ignores ``{\\an8}`` positioning tags in SRT, the
    collision-avoidance strategy is *track separation*: monologue and captions
    are exported as two independent SRT files so the editor can place them on
    different subtitle tracks/lanes in the NLE.

    Returns list of SRT paths written (monologue first, then timeline captions,
    then per-clip files).
    """
    from .transcribe import generate_srt
    from .versioning import resolve_transcript_path

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # ------------------------------------------------------------------
    # Monologue SRT — text overlays on a dedicated subtitle track
    # ------------------------------------------------------------------
    if monologue and monologue.overlays:
        # Build segment_index → timeline_offset lookup
        seg_timeline_offsets: dict[int, float] = {}
        offset = 0.0
        for seg in storyboard.segments:
            seg_timeline_offsets[seg.index] = offset
            dur = seg.out_sec - seg.in_sec
            if dur > 0:
                offset += dur

        style = _MONOLOGUE_STYLE
        mono_srt_path = output_dir.parent / "timeline_monologue.srt"
        mono_entries: list[str] = []
        for i, ov in enumerate(monologue.overlays, start=1):
            tl_base = seg_timeline_offsets.get(ov.segment_index, 0.0)
            tl_start = tl_base + ov.appear_at
            tl_end = tl_start + ov.duration_sec

            text = ov.text
            if style.case == "lowercase":
                text = text.lower()
            elif style.case == "sentence":
                text = text.capitalize()

            mono_entries.append(
                f"{i}\n"
                f"{_srt_timecode(tl_start)} --> {_srt_timecode(tl_end)}\n"
                f"{text}\n"
            )

        if mono_entries:
            mono_srt_path.write_text("\n".join(mono_entries), encoding="utf-8")
            written.append(mono_srt_path)
            log.info(
                "Monologue SRT written: %s (%d overlays)", mono_srt_path, len(mono_entries)
            )

    # ------------------------------------------------------------------
    # Speech captions — load transcripts
    # ------------------------------------------------------------------
    used_clip_ids = list(dict.fromkeys(seg.clip_id for seg in storyboard.segments))

    clip_transcripts: dict[str, dict] = {}
    for clip_id in used_clip_ids:
        clip_root = editorial_paths.clips_dir / clip_id
        transcript_path = resolve_transcript_path(clip_root)
        if not transcript_path:
            log.debug("No transcript for %s — skipping SRT", clip_id)
            continue
        transcript = json.loads(transcript_path.read_text())
        if transcript.get("has_speech", False):
            clip_transcripts[clip_id] = transcript

    if not clip_transcripts and not written:
        return written

    # --- Per-clip SRT files (reference) ---
    for clip_id, transcript in clip_transcripts.items():
        srt_path = output_dir / f"{clip_id}.srt"
        generate_srt(transcript, srt_path)
        written.append(srt_path)

    # --- Timeline-aligned caption SRT ---
    timeline_srt_path = output_dir.parent / "timeline_subtitles.srt"
    timeline_entries: list[str] = []
    cue_index = 1
    timeline_offset = 0.0

    for seg in storyboard.segments:
        seg_duration = seg.out_sec - seg.in_sec
        if seg_duration <= 0:
            continue

        transcript = clip_transcripts.get(seg.clip_id)
        if transcript:
            for tseg in transcript.get("segments", []):
                seg_type = tseg.get("type", "speech")
                text = tseg.get("text", "")
                if not text or seg_type != "speech":
                    continue

                cue_start = tseg.get("start", 0.0)
                cue_end = tseg.get("end", 0.0)

                if cue_end <= seg.in_sec or cue_start >= seg.out_sec:
                    continue

                effective_start = max(cue_start, seg.in_sec)
                effective_end = min(cue_end, seg.out_sec)

                tl_start = timeline_offset + (effective_start - seg.in_sec)
                tl_end = timeline_offset + (effective_end - seg.in_sec)

                # Plain text only — no speaker names, just the words spoken
                cue_text = text.lower()

                timeline_entries.append(
                    f"{cue_index}\n"
                    f"{_srt_timecode(tl_start)} --> {_srt_timecode(tl_end)}\n"
                    f"{cue_text}\n"
                )
                cue_index += 1

        timeline_offset += seg_duration

    if timeline_entries:
        timeline_srt_path.write_text("\n".join(timeline_entries), encoding="utf-8")
        written.append(timeline_srt_path)
        log.info("Timeline SRT written: %s (%d cues)", timeline_srt_path, cue_index - 1)

    return written


def _srt_timecode(seconds: float) -> str:
    """Convert seconds to SRT timecode format: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _compute_timeline_duration(segments: list[Segment]) -> float:
    """Compute total timeline duration from segments (simple sum, no overlap accounting)."""
    return sum(seg.out_sec - seg.in_sec for seg in segments if seg.out_sec > seg.in_sec)
