"""Rough cut executor — load structured EDL, validate, assemble with ffmpeg."""

import json
import subprocess
from pathlib import Path

from .config import EditorialProjectPaths, OutputFormat
from .models import EditorialStoryboard
from .preprocess import get_hwaccel_args, get_hwenc_codec, get_video_duration
from .render import render_html_preview
from .storyboard_format import format_duration
from .versioning import next_version, versioned_dir, update_latest_symlink


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _build_source_map(editorial_paths: EditorialProjectPaths) -> dict[str, Path]:
    """Build clip_id → original source path map from the master manifest."""
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
    source_map: dict[str, Path] | None = None,
) -> Path | None:
    """Resolve the original source file for a clip.

    Prefers source_path from manifest (no copy needed). Falls back to
    the legacy source/ symlink/copy dir for older projects.
    """
    if source_map and clip_id in source_map:
        p = source_map[clip_id]
        if p.exists():
            return p

    # Fallback: legacy source/ dir (symlink or copy)
    clip_paths = editorial_paths.clip_paths(clip_id)
    source_dir = clip_paths.source
    if source_dir.exists():
        files = [f for f in source_dir.iterdir() if f.is_file()]
        if files:
            return files[0]
    return None


def validate_edl(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    source_map: dict[str, Path] | None = None,
) -> list[str]:
    """Validate segments against actual clip durations. Clamps out-of-bounds in-place. Returns warnings."""
    warnings = []
    clip_durations: dict[str, float] = {}

    for seg in storyboard.segments:
        # Get clip duration (cached)
        if seg.clip_id not in clip_durations:
            source = _resolve_clip_source(seg.clip_id, editorial_paths, source_map)
            if source:
                clip_durations[seg.clip_id] = get_video_duration(source)
            else:
                warnings.append(f"#{seg.index}: source not found for {seg.clip_id}")
                continue

        clip_dur = clip_durations[seg.clip_id]

        if seg.out_sec > clip_dur:
            warnings.append(
                f"#{seg.index} {seg.clip_id}: out_sec {seg.out_sec:.1f}s > clip duration "
                f"{clip_dur:.1f}s — clamped"
            )
            seg.out_sec = clip_dur

        if seg.in_sec >= clip_dur:
            warnings.append(
                f"#{seg.index} {seg.clip_id}: in_sec {seg.in_sec:.1f}s >= clip duration — skipped"
            )
            continue

        if seg.in_sec >= seg.out_sec:
            warnings.append(f"#{seg.index} {seg.clip_id}: in_sec >= out_sec — skipped")
            continue

        if seg.duration_sec < 0.5:
            warnings.append(f"#{seg.index} {seg.clip_id}: very short ({seg.duration_sec:.2f}s)")

    return warnings


# ---------------------------------------------------------------------------
# ffmpeg assembly
# ---------------------------------------------------------------------------


def _build_segment_vf(
    clip_info: dict | None,
    output_format: OutputFormat | None,
) -> str | None:
    """Build the -vf filter chain for a segment, or None if no filtering needed.

    Handles rotation, scaling, padding/cropping, and fps normalization.
    """
    if not output_format or not clip_info:
        return None

    target_w = output_format.width
    target_h = output_format.height
    target_fps = output_format.fps
    fit_mode = output_format.fit_mode

    # Source effective dimensions (after rotation)
    src_w = clip_info.get("display_width", clip_info.get("width", 0))
    src_h = clip_info.get("display_height", clip_info.get("height", 0))
    rotation = clip_info.get("rotation", 0)

    if src_w <= 0 or src_h <= 0:
        return None

    filters = []

    # 1. Rotation correction
    if rotation == 90:
        filters.append("transpose=1")
    elif rotation == 180:
        filters.append("hflip,vflip")
    elif rotation == 270:
        filters.append("transpose=2")

    # 2. Determine scaling strategy
    src_orientation = "landscape" if src_w >= src_h else "portrait"
    target_orientation = output_format.orientation

    src_ratio = src_w / src_h
    target_ratio = target_w / target_h
    ratios_match = abs(src_ratio - target_ratio) < 0.01

    if src_w == target_w and src_h == target_h and rotation == 0:
        # Exact match — only need fps normalization if needed
        pass
    elif src_orientation != target_orientation:
        # Cross orientation (e.g. portrait in landscape) — always pad
        filters.append(f"scale=-2:{target_h}")
        filters.append(f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black")
    elif ratios_match:
        # Same aspect ratio, just scale
        filters.append(f"scale={target_w}:{target_h}")
    else:
        # Different aspect ratio, same orientation
        if fit_mode == "crop":
            filters.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase")
            filters.append(f"crop={target_w}:{target_h}")
        else:
            # pad (default)
            filters.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease")
            filters.append(f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black")

    # 3. FPS normalization
    src_fps = clip_info.get("fps_float", 0)
    if src_fps > 0 and abs(src_fps - target_fps) > 0.5:
        filters.append(f"fps={target_fps}")

    return ",".join(filters) if filters else None


def _escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext filter.

    ffmpeg drawtext requires escaping of special characters: backslash, colon,
    single-quote, semicolon, brackets, and equals sign. Newlines are converted
    to spaces to avoid breaking the filter chain.
    """
    text = text.replace("\n", " ").replace("\r", "")
    # Order matters: backslash first
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\u2019")  # curly apostrophe — avoids shell/ffmpeg quoting hell
    text = text.replace(":", "\\:")
    text = text.replace(";", "\\;")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


def _resolve_font_path(font_name: str) -> str:
    """Resolve a logical font name to an actual file path using fc-match.

    Falls back to a known CJK-capable font path to ensure Chinese/Japanese/Korean
    characters render correctly.
    """
    import shutil
    import subprocess as _sp

    # Try fc-match first (works on Linux, sometimes macOS with fontconfig)
    if shutil.which("fc-match"):
        try:
            result = _sp.run(
                ["fc-match", "-f", "%{file}", font_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            path = result.stdout.strip()
            if path and Path(path).exists():
                return path
        except Exception:
            pass

    # Static fallback paths for CJK-capable fonts
    for candidate in [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if Path(candidate).exists():
            return candidate

    return font_name  # last resort: pass as-is


def _build_overlay_drawtext(overlays, output_format: OutputFormat | None = None) -> list[str]:
    """Build ffmpeg drawtext filter strings for a list of MonologueOverlay objects.

    Styled after Korean silent vlog typography: clean sans-serif, bright white,
    positioned in the lower portion of the frame with soft shadow for readability.
    """
    import platform

    # Font resolution — prefer modern geometric sans-serif with CJK support
    if platform.system() == "Darwin":
        font_map = {
            "sans-serif": "/System/Library/Fonts/Avenir Next.ttc",
            "handwritten": "/System/Library/Fonts/Noteworthy.ttc",
        }
    else:
        font_map = {
            "sans-serif": _resolve_font_path("sans-serif:lang=zh"),
            "handwritten": _resolve_font_path("serif:lang=zh"),
        }

    # Size mapping — sized to match typical silent vlog text (large, readable)
    base_h = output_format.height if output_format else 1080
    size_map = {
        "small": max(28, int(base_h * 0.035)),
        "medium": max(38, int(base_h * 0.046)),
        "large": max(50, int(base_h * 0.056)),
    }

    filters = []
    for ov in overlays:
        font_file = font_map.get(ov.style.font, font_map["sans-serif"])
        font_size = size_map.get(ov.style.size, size_map["medium"])

        # Position — lower_third sits at ~88% from top (above playback controls)
        if ov.style.position == "center":
            y_expr = "(h-th)/2"
        elif ov.style.position == "upper_third":
            y_expr = "h*0.15"
        else:  # lower_third (default)
            y_expr = "h*0.88-th"

        # Alignment — default center for silent vlog aesthetic
        if ov.style.alignment == "center":
            x_expr = "(w-tw)/2"
        elif ov.style.alignment == "right":
            x_expr = "w-tw-40"
        else:  # left
            x_expr = "40"

        # Apply case transformation
        text = ov.text
        if ov.style.case == "lowercase":
            text = text.lower()
        elif ov.style.case == "sentence":
            text = text.capitalize()

        escaped = _escape_drawtext(text)

        end_t = ov.appear_at + ov.duration_sec
        f = (
            f"drawtext=text='{escaped}'"
            f":fontfile='{font_file}'"
            f":fontsize={font_size}"
            f":fontcolor=white"
            f":shadowcolor=black@0.6:shadowx=3:shadowy=3"
            f":x={x_expr}:y={y_expr}"
            f":enable='between(t,{ov.appear_at:.2f},{end_t:.2f})'"
        )
        filters.append(f)

    return filters


def _build_caption_drawtext(
    transcript_segments: list,
    clip_in_sec: float,
    clip_out_sec: float,
    output_format: OutputFormat | None = None,
) -> list[str]:
    """Build ffmpeg drawtext filters for speech captions from transcript segments.

    Only renders speech segments (no speaker labels). Timestamps are converted
    from clip-absolute to segment-relative.
    """
    import platform

    if platform.system() == "Darwin":
        font_file = "/System/Library/Fonts/Avenir Next.ttc"
    else:
        font_file = _resolve_font_path("sans-serif:lang=zh")

    base_h = output_format.height if output_format else 1080
    font_size = max(28, int(base_h * 0.038))

    filters = []
    for ts in transcript_segments:
        # Only speech segments
        if ts.get("type", "speech") != "speech":
            continue

        text = ts.get("text", "").strip()
        if not text:
            continue

        seg_start = ts["start"]
        seg_end = ts["end"]

        # Clip to the segment's time range
        if seg_end <= clip_in_sec or seg_start >= clip_out_sec:
            continue

        # Convert to segment-relative time
        local_start = max(0.0, seg_start - clip_in_sec)
        local_end = min(clip_out_sec - clip_in_sec, seg_end - clip_in_sec)

        if local_end - local_start < 0.2:
            continue

        # Lowercase to match monologue style
        escaped = _escape_drawtext(text.lower())

        f = (
            f"drawtext=text='{escaped}'"
            f":fontfile='{font_file}'"
            f":fontsize={font_size}"
            f":fontcolor=white"
            f":shadowcolor=black@0.6:shadowx=3:shadowy=3"
            f":x=(w-tw)/2:y=h*0.88-th"
            f":enable='between(t,{local_start:.2f},{local_end:.2f})'"
        )
        filters.append(f)

    return filters


def _extract_segment(
    source_path: Path,
    in_sec: float,
    out_sec: float,
    output_path: Path,
    output_format: OutputFormat | None = None,
    clip_info: dict | None = None,
    overlays: list | None = None,
    caption_segments: list | None = None,
) -> bool:
    """Extract a single segment with optional format normalization, text overlays, and captions."""
    duration = out_sec - in_sec
    if duration <= 0:
        return False

    vf = _build_segment_vf(clip_info, output_format)

    # Collect all drawtext filters (monologue overlays + speech captions)
    extra_filters = []
    if overlays:
        extra_filters.extend(_build_overlay_drawtext(overlays, output_format))
    if caption_segments:
        extra_filters.extend(
            _build_caption_drawtext(caption_segments, in_sec, out_sec, output_format)
        )

    if extra_filters:
        if vf:
            vf = vf + "," + ",".join(extra_filters)
        else:
            vf = ",".join(extra_filters)

    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())

    # Disable autorotate when we handle rotation explicitly
    if clip_info and clip_info.get("rotation", 0) != 0:
        cmd.append("-noautorotate")

    cmd.extend(["-ss", str(in_sec), "-i", str(source_path), "-t", str(duration)])

    if vf:
        cmd.extend(["-vf", vf])

    # Codec selection — resolve "auto" to HW encoder, force yuv420p for iPhone compat
    sw_codec = output_format.codec if output_format else "libx264"
    if sw_codec == "auto":
        sw_codec = "libx264"
    codec = get_hwenc_codec(sw_codec)
    is_vt = codec.endswith("_videotoolbox")

    cmd.extend(["-c:v", codec])
    if is_vt:
        # VideoToolbox: use quality-based VBR (65 ≈ CRF 20-23 visual quality)
        # -allow_sw 1 falls back to software if HW engine is busy
        cmd.extend(["-q:v", "65", "-allow_sw", "1"])
        if codec == "hevc_videotoolbox":
            cmd.extend(["-tag:v", "hvc1"])  # iPhone requires hvc1 tag for HEVC
    else:
        # Software encoder: use CRF for quality
        cmd.extend(["-preset", "fast", "-crf", "23"])
        if codec == "libx264":
            cmd.extend(["-profile:v", "high", "-level", "4.2"])
    cmd.extend(["-pix_fmt", "yuv420p"])
    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"])
    cmd.extend(["-movflags", "+faststart", str(output_path)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def _load_clip_transcript(editorial_paths: EditorialProjectPaths, clip_id: str) -> list | None:
    """Load transcript segments for a clip, or None if unavailable."""
    transcript_path = editorial_paths.clip_paths(clip_id).root / "audio" / "transcript.json"
    if transcript_path.exists():
        data = json.loads(transcript_path.read_text())
        return data.get("segments", [])
    return None


def assemble_rough_cut(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    version_dir: Path,
    source_map: dict[str, Path] | None = None,
    output_format: OutputFormat | None = None,
    clip_format_map: dict[str, dict] | None = None,
    monologue=None,
    burn_captions: bool = False,
) -> tuple[Path, list[str]]:
    """Assemble a rough cut video from the structured storyboard. Returns (path, warnings)."""
    segments_dir = version_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    # Build overlay lookup: segment_index → list of overlays
    overlay_map: dict[int, list] = {}
    if monologue:
        for ov in monologue.overlays:
            overlay_map.setdefault(ov.segment_index, []).append(ov)

    # Load transcripts for caption burning
    transcript_cache: dict[str, list | None] = {}
    has_text = monologue is not None or burn_captions

    segment_files = []
    warnings = []

    for seg in storyboard.segments:
        source = _resolve_clip_source(seg.clip_id, editorial_paths, source_map)
        if not source:
            warnings.append(f"#{seg.index}: source not found for {seg.clip_id}")
            continue

        if seg.in_sec >= seg.out_sec:
            continue

        # Load transcript for this clip's captions (cached per clip)
        caption_segments = None
        if burn_captions:
            if seg.clip_id not in transcript_cache:
                transcript_cache[seg.clip_id] = _load_clip_transcript(editorial_paths, seg.clip_id)
            caption_segments = transcript_cache[seg.clip_id]

        # Use different filename when text overlays present to avoid caching conflicts
        seg_overlays = overlay_map.get(seg.index)
        suffix = "_txt" if has_text else ""
        seg_path = segments_dir / f"seg_{seg.index:03d}_{seg.clip_id}{suffix}.mp4"

        overlay_count = len(seg_overlays) if seg_overlays else 0
        labels = []
        if overlay_count:
            labels.append(f"+{overlay_count} text")
        if caption_segments:
            labels.append("+captions")
        label_str = f" ({', '.join(labels)})" if labels else ""
        print(
            f"  [{seg.index}/{len(storyboard.segments)}] {seg.clip_id} "
            f"{seg.in_sec:.1f}s-{seg.out_sec:.1f}s ({seg.duration_sec:.1f}s) "
            f"— {seg.purpose}{label_str}"
        )

        if seg_path.exists() and seg_path.stat().st_size > 0:
            segment_files.append(seg_path)
            continue

        clip_info = clip_format_map.get(seg.clip_id) if clip_format_map else None
        ok = _extract_segment(
            source,
            seg.in_sec,
            seg.out_sec,
            seg_path,
            output_format=output_format,
            clip_info=clip_info,
            overlays=seg_overlays,
            caption_segments=caption_segments,
        )
        if ok and seg_path.exists():
            actual_dur = get_video_duration(seg_path)
            if abs(actual_dur - seg.duration_sec) > 1.0:
                warnings.append(
                    f"#{seg.index} {seg.clip_id}: expected {seg.duration_sec:.1f}s, got {actual_dur:.1f}s"
                )
            segment_files.append(seg_path)
        else:
            warnings.append(f"#{seg.index}: ffmpeg extraction failed")

    if not segment_files:
        raise RuntimeError("No segments extracted — cannot assemble rough cut")

    # Concatenate
    rough_cut_path = version_dir / "rough_cut.mp4"
    concat_list = segments_dir / "concat_list.txt"
    concat_list.write_text("\n".join(f"file '{seg.resolve()}'" for seg in segment_files) + "\n")

    print(f"\n  Concatenating {len(segment_files)} segments...")
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-fflags",
            "+genpts",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(rough_cut_path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    size_mb = rough_cut_path.stat().st_size / 1024 / 1024
    print(f"  Rough cut: {rough_cut_path} ({size_mb:.1f} MB)")
    return rough_cut_path, warnings


# ---------------------------------------------------------------------------
# Full pipeline (no LLM — pure execution)
# ---------------------------------------------------------------------------


def _load_output_format(editorial_paths: EditorialProjectPaths) -> OutputFormat | None:
    """Load output format from project.json, or None if not configured."""
    project_json = editorial_paths.root / "project.json"
    if project_json.exists():
        meta = json.loads(project_json.read_text())
        if "output_format" in meta:
            return OutputFormat.from_dict(meta["output_format"])
    return None


def _build_clip_format_map(editorial_paths: EditorialProjectPaths) -> dict[str, dict]:
    """Build clip_id → format metadata dict from manifest."""
    manifest_path = editorial_paths.master_manifest
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        return {clip["clip_id"]: clip for clip in manifest.get("clips", [])}
    return {}


def run_rough_cut(
    storyboard_json_path: Path,
    editorial_paths: EditorialProjectPaths,
    monologue=None,
) -> dict:
    """Load structured storyboard → validate → ffmpeg assembly → HTML preview.

    Writes into the same exports/vN/ dir as the storyboard's analyze version.
    """
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json_path.read_text())

    # Build source map from manifest (clip_id → original file path)
    source_map = _build_source_map(editorial_paths)

    # Load output format and clip format info
    output_format = _load_output_format(editorial_paths)
    clip_format_map = _build_clip_format_map(editorial_paths)

    # Derive version from storyboard JSON filename (editorial_gemini_v4.json → 4)
    import re

    v_match = re.search(r"_v(\d+)\.", storyboard_json_path.name)
    if v_match:
        v = int(v_match.group(1))
    else:
        v = next_version(editorial_paths.root, "cut")
    vdir = versioned_dir(editorial_paths.exports, v)
    print(f"  Export version: v{v}")
    print(f"  Loaded storyboard: {storyboard.title} ({len(storyboard.segments)} segments)")
    if output_format:
        sw_codec = output_format.codec if output_format.codec != "auto" else "libx264"
        resolved_enc = get_hwenc_codec(sw_codec)
        enc_label = (
            f"{resolved_enc} (hardware-accelerated)"
            if resolved_enc.endswith("_videotoolbox")
            else resolved_enc
        )
        print(
            f"  Output format: {output_format.label} ({output_format.width}x{output_format.height}"
            f" @ {output_format.fps}fps, {enc_label}, fit={output_format.fit_mode})"
        )
    else:
        print("  Output format: default (no normalization)")

    # Validate
    print("  Validating...")
    validation_warnings = validate_edl(storyboard, editorial_paths, source_map)
    if validation_warnings:
        for w in validation_warnings:
            print(f"    WARNING: {w}")
    else:
        print("    All segments valid")

    # Assemble
    overlay_label = " (with text overlays + captions)" if monologue else ""
    print(f"\n  Extracting segments{overlay_label}...")
    rough_cut_path, assembly_warnings = assemble_rough_cut(
        storyboard,
        editorial_paths,
        vdir,
        source_map,
        output_format=output_format,
        clip_format_map=clip_format_map,
        monologue=monologue,
        burn_captions=monologue is not None,
    )
    all_warnings = validation_warnings + assembly_warnings

    # Render HTML preview (with video embed)
    print("\n  Generating preview...")
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=vdir,
        warnings=all_warnings,
        rough_cut_path=rough_cut_path,
    )
    preview_path = vdir / "preview.html"
    preview_path.write_text(html)

    # Symlink latest
    update_latest_symlink(vdir)

    return {
        "version": v,
        "rough_cut": rough_cut_path,
        "preview": preview_path,
        "warnings": all_warnings,
    }
