"""Analyze source clip formats and recommend output settings."""

from collections import Counter

from .config import OutputFormat


# ---------------------------------------------------------------------------
# Live Photo detection
# ---------------------------------------------------------------------------

# iPhone Live Photos: short duration, typically 4:3, often 60fps
LIVE_PHOTO_MAX_DURATION = 4.0  # seconds
LIVE_PHOTO_ASPECT_RATIOS = {"4:3", "3:4"}


def detect_live_photos(clip_metadata: list[dict]) -> list[str]:
    """Return clip_ids that look like iPhone Live Photo video components."""
    live_photos = []
    for clip in clip_metadata:
        duration = clip.get("duration_sec", 999)
        aspect = clip.get("aspect_ratio", "")
        if duration <= LIVE_PHOTO_MAX_DURATION and aspect in LIVE_PHOTO_ASPECT_RATIOS:
            live_photos.append(clip["clip_id"])
    return live_photos


# ---------------------------------------------------------------------------
# Format analysis
# ---------------------------------------------------------------------------

# Standard output resolutions
STANDARD_FORMATS = {
    "4K": {"width": 3840, "height": 2160, "label": "4K UHD (3840x2160)"},
    "QHD": {"width": 2560, "height": 1440, "label": "QHD (2560x1440)"},
    "FHD": {"width": 1920, "height": 1080, "label": "Full HD (1920x1080)"},
    "HD": {"width": 1280, "height": 720, "label": "HD (1280x720)"},
}


def analyze_source_formats(clip_metadata: list[dict]) -> dict:
    """Analyze format diversity across all clips.

    Returns a dict with format groups, flags, and dominant format info.
    """
    if not clip_metadata:
        return {"clips": [], "groups": [], "dominant": None}

    # Count by key attributes
    res_counter: Counter[str] = Counter()
    orient_counter: Counter[str] = Counter()
    codec_counter: Counter[str] = Counter()
    aspect_counter: Counter[str] = Counter()
    fps_values: list[float] = []
    has_hdr = False

    for clip in clip_metadata:
        res_class = clip.get("resolution_class", "unknown")
        orientation = clip.get("orientation", "unknown")
        codec = clip.get("codec", "unknown")
        aspect = clip.get("aspect_ratio", "unknown")
        fps_float = clip.get("fps_float", 0)

        res_counter[res_class] += 1
        orient_counter[orientation] += 1
        codec_counter[codec] += 1
        aspect_counter[aspect] += 1
        if fps_float > 0:
            fps_values.append(fps_float)
        if clip.get("is_hdr"):
            has_hdr = True

    # Build format groups (unique combos of resolution_class + aspect_ratio)
    groups: dict[str, list[dict]] = {}
    for clip in clip_metadata:
        key = f"{clip.get('resolution_class', '?')} {clip.get('aspect_ratio', '?')}"
        groups.setdefault(key, []).append(clip)

    # Dominant = most clips
    dominant_res = res_counter.most_common(1)[0][0] if res_counter else "FHD"
    dominant_orient = orient_counter.most_common(1)[0][0] if orient_counter else "landscape"
    dominant_aspect = aspect_counter.most_common(1)[0][0] if aspect_counter else "16:9"

    # Median fps (round to common values)
    median_fps = sorted(fps_values)[len(fps_values) // 2] if fps_values else 29.97
    common_fps = _snap_to_common_fps(median_fps)

    live_photos = detect_live_photos(clip_metadata)

    return {
        "clip_count": len(clip_metadata),
        "groups": {k: len(v) for k, v in groups.items()},
        "resolution_counts": dict(res_counter),
        "orientation_counts": dict(orient_counter),
        "codec_counts": dict(codec_counter),
        "aspect_counts": dict(aspect_counter),
        "has_mixed_resolutions": len(res_counter) > 1,
        "has_mixed_orientations": len(orient_counter) > 1,
        "has_mixed_codecs": len(codec_counter) > 1,
        "has_mixed_aspects": len(aspect_counter) > 1,
        "has_hevc": "hevc" in codec_counter,
        "has_hdr": has_hdr,
        "dominant_resolution": dominant_res,
        "dominant_orientation": dominant_orient,
        "dominant_aspect": dominant_aspect,
        "dominant_fps": common_fps,
        "live_photo_ids": live_photos,
    }


def _snap_to_common_fps(fps: float) -> float:
    """Snap to nearest common frame rate."""
    common = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
    return min(common, key=lambda c: abs(c - fps))


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def recommend_output_format(analysis: dict) -> tuple[OutputFormat, str]:
    """Return (recommended OutputFormat, rationale string)."""
    res = analysis["dominant_resolution"]
    orient = analysis["dominant_orientation"]
    fps = analysis["dominant_fps"]

    fmt = STANDARD_FORMATS.get(res, STANDARD_FORMATS["FHD"])
    width, height = fmt["width"], fmt["height"]

    # Flip for portrait
    if orient == "portrait":
        width, height = height, width

    label = fmt["label"]
    if orient == "portrait":
        label += " Portrait"

    output = OutputFormat(
        width=width,
        height=height,
        fps=fps,
        orientation=orient,
        label=label,
    )

    # Build rationale
    parts = []
    if analysis["has_mixed_resolutions"]:
        counts = analysis["resolution_counts"]
        parts.append(f"mixed resolutions ({', '.join(f'{k}: {v}' for k, v in counts.items())})")
    if analysis["has_mixed_aspects"]:
        counts = analysis["aspect_counts"]
        parts.append(f"mixed aspects ({', '.join(f'{k}: {v}' for k, v in counts.items())})")
    if analysis["has_hevc"]:
        parts.append("HEVC sources detected — hardware decode enabled")
    if analysis["has_hdr"]:
        parts.append("HDR clips detected — tone-mapping not applied, colors may differ")

    if parts:
        rationale = f"Recommended {label} @ {fps}fps. Notes: {'; '.join(parts)}."
    else:
        rationale = f"All clips are uniform — {label} @ {fps}fps."

    return output, rationale


# ---------------------------------------------------------------------------
# TUI helpers
# ---------------------------------------------------------------------------


def build_format_choices(analysis: dict) -> list[dict]:
    """Build list of format options for TUI selection.

    Each dict: {label, width, height, fps, orientation, is_recommended}.
    """
    recommended, _ = recommend_output_format(analysis)
    choices = []

    # Add recommended first
    choices.append(
        {
            "label": f"{recommended.label} @ {recommended.fps}fps (recommended)",
            "width": recommended.width,
            "height": recommended.height,
            "fps": recommended.fps,
            "orientation": recommended.orientation,
        }
    )

    # Add standard formats that differ from recommended
    orient = analysis["dominant_orientation"]
    fps = analysis["dominant_fps"]
    for res_class, fmt in STANDARD_FORMATS.items():
        w, h = fmt["width"], fmt["height"]
        if orient == "portrait":
            w, h = h, w
        if w == recommended.width and h == recommended.height:
            continue  # skip duplicate of recommended
        choices.append(
            {
                "label": f"{fmt['label']}{' Portrait' if orient == 'portrait' else ''} @ {fps}fps",
                "width": w,
                "height": h,
                "fps": fps,
                "orientation": orient,
            }
        )

    return choices


def format_summary_text(analysis: dict, clip_metadata: list[dict]) -> str:
    """Pretty-print format analysis for TUI display."""
    lines = []
    lines.append(f"  Source analysis ({analysis['clip_count']} clips):\n")

    # Format groups table
    for group_key, count in sorted(analysis["groups"].items(), key=lambda x: -x[1]):
        lines.append(f"    {group_key}: {count} clip{'s' if count != 1 else ''}")

    # Codec info
    codecs = analysis["codec_counts"]
    if len(codecs) > 1:
        codec_str = ", ".join(f"{k}: {v}" for k, v in codecs.items())
        lines.append(f"\n    Codecs: {codec_str}")
    else:
        lines.append(f"\n    Codec: {list(codecs.keys())[0]}")

    # Flags
    if analysis["has_hdr"]:
        lines.append("    HDR content detected")

    # Live photos
    live_ids = analysis["live_photo_ids"]
    if live_ids:
        lines.append(
            f"\n    Live Photos detected: {len(live_ids)} clip{'s' if len(live_ids) != 1 else ''}"
            f" ({', '.join(live_ids[:5])}{'...' if len(live_ids) > 5 else ''})"
        )

    return "\n".join(lines)
