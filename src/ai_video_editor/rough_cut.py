"""Rough cut executor — load structured EDL, validate, assemble with ffmpeg."""

import json
import subprocess
from pathlib import Path

from .config import EditorialProjectPaths, OutputFormat
from .models import EditorialStoryboard
from .preprocess import get_hwaccel_args, get_video_duration
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


def _extract_segment(
    source_path: Path,
    in_sec: float,
    out_sec: float,
    output_path: Path,
    output_format: OutputFormat | None = None,
    clip_info: dict | None = None,
) -> bool:
    """Extract a single segment with optional format normalization."""
    duration = out_sec - in_sec
    if duration <= 0:
        return False

    vf = _build_segment_vf(clip_info, output_format)

    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())

    # Disable autorotate when we handle rotation explicitly
    if clip_info and clip_info.get("rotation", 0) != 0:
        cmd.append("-noautorotate")

    cmd.extend(["-ss", str(in_sec), "-i", str(source_path), "-t", str(duration)])

    if vf:
        cmd.extend(["-vf", vf])

    # Codec selection
    codec = output_format.codec if output_format else "libx264"
    cmd.extend(["-c:v", codec, "-preset", "fast", "-crf", "23"])
    cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    cmd.extend(["-movflags", "+faststart", str(output_path)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def assemble_rough_cut(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    version_dir: Path,
    source_map: dict[str, Path] | None = None,
    output_format: OutputFormat | None = None,
    clip_format_map: dict[str, dict] | None = None,
) -> tuple[Path, list[str]]:
    """Assemble a rough cut video from the structured storyboard. Returns (path, warnings)."""
    segments_dir = version_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    segment_files = []
    warnings = []

    for seg in storyboard.segments:
        source = _resolve_clip_source(seg.clip_id, editorial_paths, source_map)
        if not source:
            warnings.append(f"#{seg.index}: source not found for {seg.clip_id}")
            continue

        if seg.in_sec >= seg.out_sec:
            continue

        seg_path = segments_dir / f"seg_{seg.index:03d}_{seg.clip_id}.mp4"
        print(
            f"  [{seg.index}/{len(storyboard.segments)}] {seg.clip_id} "
            f"{seg.in_sec:.1f}s-{seg.out_sec:.1f}s ({seg.duration_sec:.1f}s) — {seg.purpose}"
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
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
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
        print(
            f"  Output format: {output_format.label} ({output_format.width}x{output_format.height}"
            f" @ {output_format.fps}fps, {output_format.codec}, fit={output_format.fit_mode})"
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
    print("\n  Extracting segments...")
    rough_cut_path, assembly_warnings = assemble_rough_cut(
        storyboard,
        editorial_paths,
        vdir,
        source_map,
        output_format=output_format,
        clip_format_map=clip_format_map,
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
