"""FCPXML export for DaVinci Resolve / Final Cut Pro.

Generates FCPXML v1.9 from an EditorialStoryboard, enabling professional NLE editing
of AI-assembled rough cuts. Clips reference original source files at full resolution.

Usage:
    from .fcpxml_export import export_fcpxml
    export_fcpxml(storyboard, editorial_paths, output_path)
"""

import json
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from .config import EditorialProjectPaths, OutputFormat
from .models import EditorialStoryboard, Segment

log = logging.getLogger(__name__)

# Cross Dissolve effect UID (standard across FCP/Resolve)
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"

# Default transition duration in seconds
DEFAULT_TRANSITION_SEC = 1.0

# Audio volume mapping from storyboard audio_note to dB adjustment
AUDIO_VOLUME_MAP: dict[str, str | None] = {
    "mute": "-96dB",
    "voice_over": "-96dB",
    "music_bed": "-12dB",
    "ambient": "-6dB",
    "preserve_dialogue": None,  # no adjustment
    "": None,
}


def _sec_to_frac(seconds: float, fps: float) -> str:
    """Convert float seconds to FCPXML rational fraction string.

    Examples:
        _sec_to_frac(10.0, 29.97) -> "300300/30000s"
        _sec_to_frac(0.0, 29.97) -> "0/30000s"
    """
    frac = Fraction(seconds).limit_denominator(1_000_000)
    # Express in terms of the fps timebase for cleaner fractions
    fps_frac = Fraction(fps).limit_denominator(1_000_000)
    # Frame duration denominator becomes our timebase
    frame_dur = Fraction(1) / fps_frac
    # Convert seconds to timebase units
    frames = frac / frame_dur
    frames = frames.limit_denominator(1_000_000)
    # Result: (frames * frame_dur) expressed as num/den s
    result = (frames * frame_dur).limit_denominator(1_000_000)
    return f"{result.numerator}/{result.denominator}s"


def _to_file_uri(path: Path) -> str:
    """Convert an absolute path to a percent-encoded file:/// URI."""
    abs_path = str(path.resolve())
    encoded = urllib.parse.quote(abs_path, safe="/")
    return f"file://{encoded}"


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
    fps_frac = Fraction(fps).limit_denominator(1_000_000)
    frame_dur = (Fraction(1) / fps_frac).limit_denominator(1_000_000)
    return f"{frame_dur.numerator}/{frame_dur.denominator}s"


def _format_name(width: int, height: int, fps: float) -> str:
    """Generate a format name string like 'FFVideoFormat1920x1080p2997'."""
    fps_int = str(fps).replace(".", "")
    return f"FFVideoFormat{width}x{height}p{fps_int}"


def export_fcpxml(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    output_path: Path,
    output_format: OutputFormat | None = None,
    project_name: str | None = None,
) -> Path:
    """Generate an FCPXML v1.9 file from an EditorialStoryboard.

    Args:
        storyboard: The editorial storyboard with segments to export.
        editorial_paths: Project paths for resolving clip sources.
        output_path: Where to write the .fcpxml file.
        output_format: Target format (defaults to 1920x1080 29.97fps).
        project_name: Name for the FCPXML project element.

    Returns:
        The output path written.
    """
    fmt = output_format or OutputFormat()
    name = project_name or editorial_paths.root.name

    source_map = _build_source_map(editorial_paths)
    manifest_clips = _read_manifest_clips(editorial_paths)

    # Collect unique clips used in the storyboard
    used_clip_ids = list(dict.fromkeys(seg.clip_id for seg in storyboard.segments))

    # Verify sources exist
    clip_sources: dict[str, Path] = {}
    for clip_id in used_clip_ids:
        source = _resolve_clip_source(clip_id, editorial_paths, source_map)
        if source:
            clip_sources[clip_id] = source
        else:
            log.warning("Source not found for clip %s — will be skipped in export", clip_id)

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

    # Asset elements — one per unique source clip (r2, r3, ...)
    asset_id_map: dict[str, str] = {}  # clip_id -> resource id
    next_rid = 2

    for clip_id in used_clip_ids:
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

        # Build per-clip format if different from timeline
        clip_format_id = timeline_format_id
        if clip_fps != fmt.fps or clip_width != fmt.width or clip_height != fmt.height:
            clip_format_id = f"r{next_rid}"
            next_rid += 1
            ET.SubElement(
                resources,
                "format",
                id=clip_format_id,
                name=_format_name(clip_width, clip_height, clip_fps),
                width=str(clip_width),
                height=str(clip_height),
                frameDuration=_frame_duration_str(clip_fps),
            )

        file_uri = _to_file_uri(source_path)
        duration_str = _sec_to_frac(clip_duration, clip_fps) if clip_duration > 0 else "0s"

        asset_attrs = {
            "id": rid,
            "name": source_path.name,
            "src": file_uri,
            "start": "0s",
            "duration": duration_str,
            "hasVideo": "1",
            "hasAudio": "1",
            "format": clip_format_id,
            "audioChannels": "2",
            "audioSources": "1",
        }
        asset_el = ET.SubElement(resources, "asset", **asset_attrs)
        ET.SubElement(asset_el, "media-rep", src=file_uri, kind="original-media")

    # --- Project / Sequence / Spine ---
    project_el = ET.SubElement(fcpxml, "project", name=name)

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

    # Walk segments and build timeline
    timeline_offset = Fraction(0)

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
        clip_name = (
            f"{seg.clip_id.split('_')[-1] if '_' in seg.clip_id else seg.clip_id} — {seg.purpose}"
        )
        clip_el = ET.SubElement(
            spine,
            "asset-clip",
            ref=asset_id_map[seg.clip_id],
            name=clip_name,
            offset=_sec_to_frac(float(timeline_offset), fmt.fps),
            start=_sec_to_frac(seg.in_sec, fmt.fps),
            duration=_sec_to_frac(float(seg_duration), fmt.fps),
            format=timeline_format_id,
            enabled="1",
            tcFormat="NDF",
        )

        # Apply audio volume adjustment
        volume = AUDIO_VOLUME_MAP.get(seg.audio_note)
        if volume is not None:
            ET.SubElement(clip_el, "adjust-volume", amount=volume)

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

    log.info("FCPXML written to %s", output_path)
    return output_path


def export_srt_files(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    output_dir: Path,
) -> list[Path]:
    """Export a timeline-aligned SRT plus per-clip SRT files alongside the FCPXML.

    The timeline SRT is the primary output: subtitle cues are remapped to match the
    FCPXML timeline offsets, so importing one SRT file into DaVinci Resolve gives you
    subtitles that are already synced to the assembled edit. No manual association needed.

    Per-clip SRT files are also exported for reference or individual clip work.

    Returns list of SRT paths written.
    """
    from .transcribe import generate_srt
    from .versioning import resolve_transcript_path

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # Collect unique clip IDs that appear in the storyboard
    used_clip_ids = list(dict.fromkeys(seg.clip_id for seg in storyboard.segments))

    # Load all transcripts needed
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

    if not clip_transcripts:
        return written

    # --- Per-clip SRT files (reference) ---
    for clip_id, transcript in clip_transcripts.items():
        srt_path = output_dir / f"{clip_id}.srt"
        generate_srt(transcript, srt_path)
        written.append(srt_path)

    # --- Timeline-aligned SRT (primary output) ---
    timeline_srt_path = output_dir.parent / "timeline_subtitles.srt"
    timeline_entries: list[str] = []
    cue_index = 1
    timeline_offset = 0.0  # running offset on the assembled timeline

    for seg in storyboard.segments:
        seg_duration = seg.out_sec - seg.in_sec
        if seg_duration <= 0:
            continue

        transcript = clip_transcripts.get(seg.clip_id)
        if transcript:
            for tseg in transcript.get("segments", []):
                seg_type = tseg.get("type", "speech")
                text = tseg.get("text", "")
                if not text or seg_type == "silence":
                    continue

                # Only include cues that overlap the segment's in/out window
                cue_start = tseg.get("start", 0.0)
                cue_end = tseg.get("end", 0.0)

                # Skip cues entirely outside the segment window
                if cue_end <= seg.in_sec or cue_start >= seg.out_sec:
                    continue

                # Clamp to segment boundaries
                effective_start = max(cue_start, seg.in_sec)
                effective_end = min(cue_end, seg.out_sec)

                # Remap to timeline position
                tl_start = timeline_offset + (effective_start - seg.in_sec)
                tl_end = timeline_offset + (effective_end - seg.in_sec)

                # Format cue text with speaker/type markers
                speaker = tseg.get("speaker")
                if seg_type == "music":
                    cue_text = f"\u266a {text} \u266a"
                elif seg_type == "sound_effect":
                    cue_text = f"[{text}]"
                elif speaker:
                    cue_text = f"{speaker}: {text}"
                else:
                    cue_text = text

                timeline_entries.append(
                    f"{cue_index}\n"
                    f"{_srt_timecode(tl_start)} --> {_srt_timecode(tl_end)}\n"
                    f"{cue_text}\n"
                )
                cue_index += 1

        timeline_offset += seg_duration

    if timeline_entries:
        timeline_srt_path.write_text("\n".join(timeline_entries), encoding="utf-8")
        written.insert(0, timeline_srt_path)  # primary output first
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
