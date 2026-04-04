"""Analyze source clip formats and recommend output settings."""

from collections import Counter
from dataclasses import dataclass

from .config import OutputFormat


# ---------------------------------------------------------------------------
# Device color profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceColorProfile:
    """Known color characteristics for a device family.

    Used to determine what colorspace conversion is needed when mixing
    footage from different devices in a single rough cut.
    """

    device: str
    label: str
    # Native color properties (what this device typically outputs)
    color_space: str  # "bt709" | "bt2020nc"
    color_transfer: str  # "bt709" | "arib-std-b67" (HLG) | "smpte2084" (PQ)
    color_primaries: str  # "bt709" | "bt2020"
    is_hdr: bool
    # ffmpeg filters to convert FROM this profile TO BT.709 SDR
    to_sdr_vf: tuple[str, ...] | None  # None = already SDR, no conversion needed
    # ffmpeg filters to convert FROM this profile TO HLG/BT.2020
    to_hlg_vf: tuple[str, ...] | None  # None = already HLG, no conversion needed


# Known device profiles — expand as needed
DEVICE_PROFILES: dict[str, DeviceColorProfile] = {
    "iphone_hlg": DeviceColorProfile(
        device="iphone",
        label="iPhone (HDR — HLG/BT.2020)",
        color_space="bt2020nc",
        color_transfer="arib-std-b67",
        color_primaries="bt2020",
        is_hdr=True,
        to_sdr_vf=(
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ),
        to_hlg_vf=None,  # already HLG
    ),
    "iphone_sdr": DeviceColorProfile(
        device="iphone",
        label="iPhone (SDR — BT.709)",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        is_hdr=False,
        to_sdr_vf=None,  # already SDR
        to_hlg_vf=(
            # Inverse tone-map SDR → HLG container (simple gamma lift, not true HDR)
            "zscale=t=arib-std-b67:m=bt2020nc:p=bt2020:r=tv",
        ),
    ),
    "sony_alpha_sdr": DeviceColorProfile(
        device="sony_alpha",
        label="Sony Alpha (SDR — BT.709/xvYCC)",
        color_space="bt709",
        color_transfer="bt709",  # iec61966-2-4 ≈ bt709 gamma
        color_primaries="bt709",
        is_hdr=False,
        to_sdr_vf=None,  # already SDR
        to_hlg_vf=("zscale=t=arib-std-b67:m=bt2020nc:p=bt2020:r=tv",),
    ),
    "sony_alpha_hlg": DeviceColorProfile(
        device="sony_alpha",
        label="Sony Alpha (HLG/BT.2020)",
        color_space="bt2020nc",
        color_transfer="arib-std-b67",
        color_primaries="bt2020",
        is_hdr=True,
        to_sdr_vf=(
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ),
        to_hlg_vf=None,
    ),
    "sony_alpha_slog3": DeviceColorProfile(
        device="sony_alpha",
        label="Sony Alpha (S-Log3/S-Gamut3)",
        color_space="bt2020nc",
        color_transfer="linear",  # S-Log3 reported variously; treat as log
        color_primaries="bt2020",
        is_hdr=False,
        to_sdr_vf=(
            # S-Log3 → linear → BT.709
            "colorspace=all=bt709:iall=bt709:itrc=linear:iprimaries=bt2020:ispace=bt2020nc:fast=0",
        ),
        to_hlg_vf=(
            "colorspace=all=bt2020nc:trc=arib-std-b67"
            ":itrc=linear:iprimaries=bt2020:ispace=bt2020nc:fast=0",
        ),
    ),
    "insta360_sdr": DeviceColorProfile(
        device="insta360",
        label="Insta360 (SDR — BT.709)",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        is_hdr=False,
        to_sdr_vf=None,
        to_hlg_vf=("zscale=t=arib-std-b67:m=bt2020nc:p=bt2020:r=tv",),
    ),
    "unknown_sdr": DeviceColorProfile(
        device="unknown",
        label="Unknown device (SDR — BT.709)",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        is_hdr=False,
        to_sdr_vf=None,
        to_hlg_vf=("zscale=t=arib-std-b67:m=bt2020nc:p=bt2020:r=tv",),
    ),
    "unknown_hdr": DeviceColorProfile(
        device="unknown",
        label="Unknown device (HDR)",
        color_space="bt2020nc",
        color_transfer="arib-std-b67",
        color_primaries="bt2020",
        is_hdr=True,
        to_sdr_vf=(
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ),
        to_hlg_vf=None,
    ),
}


def identify_color_profile(clip_info: dict) -> DeviceColorProfile:
    """Match a clip's metadata to a known device color profile.

    Uses device family + color transfer/primaries to pick the right profile.
    Falls back to generic SDR/HDR profiles for unknown devices.
    """
    device = clip_info.get("device", "unknown")
    transfer = clip_info.get("color_transfer", "")
    primaries = clip_info.get("color_primaries", "")
    is_hdr = clip_info.get("is_hdr", False)

    # iPhone
    if device == "iphone":
        if is_hdr or transfer in ("arib-std-b67", "smpte2084") or primaries == "bt2020":
            return DEVICE_PROFILES["iphone_hlg"]
        return DEVICE_PROFILES["iphone_sdr"]

    # Sony Alpha
    if device == "sony_alpha":
        if transfer in ("arib-std-b67", "smpte2084") or primaries == "bt2020":
            return DEVICE_PROFILES["sony_alpha_hlg"]
        # S-Log detection — ffprobe reports various names
        if (
            transfer
            and transfer.lower().replace("-", "").replace("_", "")
            in ("slog", "slog2", "slog3", "linear")
            and primaries == "bt2020"
        ):
            return DEVICE_PROFILES["sony_alpha_slog3"]
        return DEVICE_PROFILES["sony_alpha_sdr"]

    # Insta360
    if device == "insta360":
        return DEVICE_PROFILES["insta360_sdr"]

    # Unknown device — use HDR flag and color metadata
    if is_hdr or transfer in ("arib-std-b67", "smpte2084") or primaries == "bt2020":
        return DEVICE_PROFILES["unknown_hdr"]
    return DEVICE_PROFILES["unknown_sdr"]


def resolve_color_target(clip_metadata: list[dict]) -> str:
    """Determine the optimal output color target for a set of clips.

    Returns "sdr" or "hlg":
      - All clips share the same color space → use it (preserve quality)
      - Mixed HDR + SDR → "sdr" (normalize to the common denominator)
    """
    if not clip_metadata:
        return "sdr"

    profiles = [identify_color_profile(c) for c in clip_metadata]
    has_hdr = any(p.is_hdr for p in profiles)
    has_sdr = any(not p.is_hdr for p in profiles)

    if has_hdr and not has_sdr:
        return "hlg"
    # Mixed or all SDR → SDR
    return "sdr"


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
    device_counter: Counter[str] = Counter()
    fps_values: list[float] = []
    has_hdr = False

    for clip in clip_metadata:
        res_class = clip.get("resolution_class", "unknown")
        orientation = clip.get("orientation", "unknown")
        codec = clip.get("codec", "unknown")
        aspect = clip.get("aspect_ratio", "unknown")
        fps_float = clip.get("fps_float", 0)
        device = clip.get("device", "unknown")

        res_counter[res_class] += 1
        orient_counter[orientation] += 1
        codec_counter[codec] += 1
        aspect_counter[aspect] += 1
        device_counter[device] += 1
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

    # Resolve fps — cinematic-aware (see _resolve_fps docstring)
    fps_counter: Counter[float] = Counter()
    for clip in clip_metadata:
        fps_float = clip.get("fps_float", 0)
        if fps_float > 0:
            fps_counter[_snap_to_common_fps(fps_float)] += 1
    common_fps = _resolve_fps(fps_counter, clip_metadata)

    live_photos = detect_live_photos(clip_metadata)
    color_target = resolve_color_target(clip_metadata)

    return {
        "clip_count": len(clip_metadata),
        "groups": {k: len(v) for k, v in groups.items()},
        "resolution_counts": dict(res_counter),
        "orientation_counts": dict(orient_counter),
        "codec_counts": dict(codec_counter),
        "aspect_counts": dict(aspect_counter),
        "device_counts": dict(device_counter),
        "fps_counts": {str(k): v for k, v in fps_counter.items()},
        "has_mixed_resolutions": len(res_counter) > 1,
        "has_mixed_orientations": len(orient_counter) > 1,
        "has_mixed_codecs": len(codec_counter) > 1,
        "has_mixed_aspects": len(aspect_counter) > 1,
        "has_mixed_devices": len(device_counter) > 1,
        "has_mixed_fps": len(fps_counter) > 1,
        "has_hevc": "hevc" in codec_counter,
        "has_hdr": has_hdr,
        "dominant_resolution": dominant_res,
        "dominant_orientation": dominant_orient,
        "dominant_aspect": dominant_aspect,
        "dominant_fps": common_fps,
        "live_photo_ids": live_photos,
        "color_target": color_target,
    }


def _snap_to_common_fps(fps: float) -> float:
    """Snap to nearest common frame rate."""
    common = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
    return min(common, key=lambda c: abs(c - fps))


# Frame rates that are a deliberate cinematic / creative choice.
# Cameras never default to these — they must be manually selected.
_CINEMATIC_FPS = {23.976, 24.0, 25.0}

# Frame rates that are typically camera/phone defaults.
_DEFAULT_FPS = {29.97, 30.0, 59.94, 60.0}


def _resolve_fps(fps_counter: Counter, clip_metadata: list[dict]) -> float:
    """Pick the output frame rate for a mixed-fps project.

    Strategy (in priority order):

    1. **Uniform** — all clips share the same fps → use it.
    2. **Cinematic present** — if any clips use 23.976/24/25 fps, prefer that.
       These rates are always a deliberate creative choice (no camera defaults
       to them). Converting 30→24 is a clean frame drop; converting 24→30
       duplicates frames and destroys intentional motion blur.
    3. **All high fps** — if all clips are 50/60fps (slo-mo), use that.
    4. **Fallback** — use the most common fps across clips.
    """
    if not fps_counter:
        return 29.97

    rates = set(fps_counter.keys())

    # 1. Uniform
    if len(rates) == 1:
        return rates.pop()

    # 2. Cinematic present — prefer the cinematic rate
    cinematic = rates & _CINEMATIC_FPS
    if cinematic:
        # If multiple cinematic rates (unlikely), pick the one with more clips
        return max(cinematic, key=lambda r: fps_counter[r])

    # 3. All high-fps (slo-mo footage) — use the dominant high rate
    if rates <= {50.0, 59.94, 60.0}:
        return fps_counter.most_common(1)[0][0]

    # 4. Fallback — most common rate
    return fps_counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def recommend_output_format(analysis: dict) -> tuple[OutputFormat, str]:
    """Return (recommended OutputFormat, rationale string)."""
    res = analysis["dominant_resolution"]
    orient = analysis["dominant_orientation"]
    fps = analysis["dominant_fps"]
    color_target = analysis.get("color_target", "sdr")

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
        color_target=color_target,
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
    if analysis.get("has_mixed_devices"):
        counts = analysis["device_counts"]
        parts.append(f"mixed devices ({', '.join(f'{k}: {v}' for k, v in counts.items())})")
    if analysis.get("has_mixed_fps"):
        fps_counts = analysis["fps_counts"]
        fps_desc = ", ".join(f"{k}fps: {v}" for k, v in fps_counts.items())
        if fps in _CINEMATIC_FPS:
            parts.append(
                f"mixed frame rates ({fps_desc}) — using {fps}fps to preserve cinematic motion"
            )
        else:
            parts.append(f"mixed frame rates ({fps_desc})")
    if analysis["has_hdr"]:
        if color_target == "hlg":
            parts.append("all sources HDR — preserving HLG/BT.2020 output")
        else:
            parts.append("mixed HDR + SDR — normalizing to BT.709 SDR (HDR clips tone-mapped)")

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

    # Device info
    devices = analysis.get("device_counts", {})
    if devices:
        dev_str = ", ".join(f"{k}: {v}" for k, v in devices.items())
        lines.append(f"\n    Devices: {dev_str}")

    # FPS info
    if analysis.get("has_mixed_fps"):
        fps_counts = analysis.get("fps_counts", {})
        fps_str = ", ".join(f"{k}fps: {v}" for k, v in fps_counts.items())
        target = analysis["dominant_fps"]
        if target in _CINEMATIC_FPS:
            lines.append(f"\n    Frame rates: {fps_str}")
            lines.append(f"    → Using {target}fps (cinematic — preserves motion blur)")
        else:
            lines.append(f"\n    Frame rates: {fps_str}")

    # Flags
    if analysis["has_hdr"]:
        color_target = analysis.get("color_target", "sdr")
        if color_target == "hlg":
            lines.append("    HDR content detected — preserving HLG/BT.2020")
        else:
            lines.append("    HDR content detected — will tone-map to SDR for mixed-device compat")

    # Live photos
    live_ids = analysis["live_photo_ids"]
    if live_ids:
        lines.append(
            f"\n    Live Photos detected: {len(live_ids)} clip{'s' if len(live_ids) != 1 else ''}"
            f" ({', '.join(live_ids[:5])}{'...' if len(live_ids) > 5 else ''})"
        )

    return "\n".join(lines)
