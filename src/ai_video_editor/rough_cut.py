"""Rough cut executor — load structured EDL, validate, assemble with ffmpeg."""

import json
import subprocess
from pathlib import Path

from .config import EditorialProjectPaths
from .models import EditorialStoryboard
from .preprocess import get_video_duration
from .render import render_html_preview
from .storyboard_format import format_duration
from .versioning import next_version, versioned_dir, update_latest_symlink


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _resolve_clip_source(clip_id: str, editorial_paths: EditorialProjectPaths) -> Path | None:
    clip_paths = editorial_paths.clip_paths(clip_id)
    source_dir = clip_paths.source
    if source_dir.exists():
        files = [f for f in source_dir.iterdir() if f.is_file()]
        if files:
            return files[0]
    return None


def validate_edl(storyboard: EditorialStoryboard, editorial_paths: EditorialProjectPaths) -> list[str]:
    """Validate segments against actual clip durations. Clamps out-of-bounds in-place. Returns warnings."""
    warnings = []
    clip_durations: dict[str, float] = {}

    for seg in storyboard.segments:
        # Get clip duration (cached)
        if seg.clip_id not in clip_durations:
            source = _resolve_clip_source(seg.clip_id, editorial_paths)
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
            warnings.append(f"#{seg.index} {seg.clip_id}: in_sec {seg.in_sec:.1f}s >= clip duration — skipped")
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

def _extract_segment(source_path: Path, in_sec: float, out_sec: float, output_path: Path) -> bool:
    duration = out_sec - in_sec
    if duration <= 0:
        return False
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(in_sec),
            "-i", str(source_path),
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def assemble_rough_cut(
    storyboard: EditorialStoryboard,
    editorial_paths: EditorialProjectPaths,
    version_dir: Path,
) -> tuple[Path, list[str]]:
    """Assemble a rough cut video from the structured storyboard. Returns (path, warnings)."""
    segments_dir = version_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    segment_files = []
    warnings = []

    for seg in storyboard.segments:
        source = _resolve_clip_source(seg.clip_id, editorial_paths)
        if not source:
            warnings.append(f"#{seg.index}: source not found for {seg.clip_id}")
            continue

        if seg.in_sec >= seg.out_sec:
            continue

        seg_path = segments_dir / f"seg_{seg.index:03d}_{seg.clip_id}.mp4"
        print(f"  [{seg.index}/{len(storyboard.segments)}] {seg.clip_id} "
              f"{seg.in_sec:.1f}s-{seg.out_sec:.1f}s ({seg.duration_sec:.1f}s) — {seg.purpose}")

        if seg_path.exists() and seg_path.stat().st_size > 0:
            segment_files.append(seg_path)
            continue

        ok = _extract_segment(source, seg.in_sec, seg.out_sec, seg_path)
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
    concat_list.write_text(
        "\n".join(f"file '{seg.resolve()}'" for seg in segment_files) + "\n"
    )

    print(f"\n  Concatenating {len(segment_files)} segments...")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(rough_cut_path),
        ],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    size_mb = rough_cut_path.stat().st_size / 1024 / 1024
    print(f"  Rough cut: {rough_cut_path} ({size_mb:.1f} MB)")
    return rough_cut_path, warnings


# ---------------------------------------------------------------------------
# Full pipeline (no LLM — pure execution)
# ---------------------------------------------------------------------------

def run_rough_cut(
    storyboard_json_path: Path,
    editorial_paths: EditorialProjectPaths,
) -> dict:
    """Load structured storyboard → validate → ffmpeg assembly → HTML preview."""
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json_path.read_text())

    # Version
    v = next_version(editorial_paths.root, "cut")
    vdir = versioned_dir(editorial_paths.exports, v)
    print(f"  Cut version: v{v}")
    print(f"  Loaded storyboard: {storyboard.title} ({len(storyboard.segments)} segments)")

    # Validate
    print("  Validating...")
    validation_warnings = validate_edl(storyboard, editorial_paths)
    if validation_warnings:
        for w in validation_warnings:
            print(f"    WARNING: {w}")
    else:
        print("    All segments valid")

    # Assemble
    print("\n  Extracting segments...")
    rough_cut_path, assembly_warnings = assemble_rough_cut(storyboard, editorial_paths, vdir)
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
