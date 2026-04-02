"""Rough cut executor — load structured EDL, validate, assemble with ffmpeg."""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import EditorialProjectPaths, OutputFormat
from .models import EditorialStoryboard
from .preprocess import get_hwaccel_args, get_hwenc_codec, get_video_duration
from .render import render_html_preview
from .versioning import next_version, versioned_dir, update_latest_symlink

log = logging.getLogger(__name__)


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
# Layer 1–3: Post-encode validation
# ---------------------------------------------------------------------------


@dataclass
class SegmentProbe:
    """ffprobe result for a single encoded segment."""

    path: Path
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    fps: float = 0.0
    duration: float = 0.0
    audio_sample_rate: int = 0
    audio_channels: int = 0
    has_video: bool = False
    has_audio: bool = False
    file_size: int = 0


def _probe_segment(path: Path) -> SegmentProbe:
    """Layer 1: Probe an encoded segment and extract all stream parameters.

    Uses ffprobe to read both video and audio stream metadata. This is the
    foundation for per-segment validation and cross-segment compatibility checks.
    """
    probe = SegmentProbe(path=path)
    if not path.exists():
        return probe
    probe.file_size = path.stat().st_size

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return probe
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return probe

    probe.duration = float(data.get("format", {}).get("duration", 0))

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not probe.has_video:
            probe.has_video = True
            probe.video_codec = stream.get("codec_name", "")
            probe.width = int(stream.get("width", 0))
            probe.height = int(stream.get("height", 0))
            probe.pix_fmt = stream.get("pix_fmt", "")
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                probe.fps = round(float(num) / float(den), 3) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                probe.fps = 0.0

        elif stream.get("codec_type") == "audio" and not probe.has_audio:
            probe.has_audio = True
            probe.audio_codec = stream.get("codec_name", "")
            probe.audio_sample_rate = int(stream.get("sample_rate", 0))
            probe.audio_channels = int(stream.get("channels", 0))

    return probe


def _validate_segment(
    probe: SegmentProbe,
    expected_duration: float,
    output_format: OutputFormat | None,
    label: str,
) -> list[str]:
    """Layer 1: Validate a single segment's probe result against expectations.

    Returns a list of error strings. Empty list = segment is healthy.
    """
    errors = []

    if probe.file_size == 0:
        errors.append(f"{label}: output file is empty (0 bytes)")
        return errors  # no point checking further

    if not probe.has_video:
        errors.append(f"{label}: no video stream found")
    if not probe.has_audio:
        errors.append(f"{label}: no audio stream found")

    if probe.has_video and probe.pix_fmt and probe.pix_fmt != "yuv420p":
        errors.append(f"{label}: unexpected pixel format '{probe.pix_fmt}' (expected yuv420p)")

    if output_format and probe.has_video:
        if probe.width != output_format.width or probe.height != output_format.height:
            errors.append(
                f"{label}: resolution {probe.width}x{probe.height} "
                f"!= expected {output_format.width}x{output_format.height}"
            )

    if expected_duration > 0 and probe.duration > 0:
        drift = abs(probe.duration - expected_duration)
        if drift > 1.0:
            errors.append(
                f"{label}: duration {probe.duration:.1f}s vs expected {expected_duration:.1f}s "
                f"(drift {drift:.1f}s)"
            )

    return errors


def _check_segment_compatibility(
    probes: list[SegmentProbe],
) -> tuple[list[str], list[int]]:
    """Layer 2: Cross-segment compatibility matrix check.

    Compares all segments against each other to find parameter mismatches that
    would cause concat -c:v copy to produce a corrupt container.

    Returns (warnings, indices_of_incompatible_segments).
    Incompatible segments need re-encoding before concat.
    """
    if len(probes) < 2:
        return [], []

    warnings = []
    incompatible_indices = []

    # Determine the "majority" parameters — the most common values across segments.
    # Segments that disagree with the majority are flagged for re-encode.
    def _majority(values: list) -> object:
        if not values:
            return None
        from collections import Counter

        counts = Counter(values)
        return counts.most_common(1)[0][0]

    video_probes = [p for p in probes if p.has_video]
    if not video_probes:
        return ["No segments have a video stream"], []

    ref_codec = _majority([p.video_codec for p in video_probes])
    ref_res = _majority([(p.width, p.height) for p in video_probes])
    ref_pix = _majority([p.pix_fmt for p in video_probes])
    ref_fps = _majority([round(p.fps, 1) for p in video_probes])
    ref_asr = _majority([p.audio_sample_rate for p in video_probes if p.has_audio])
    ref_ach = _majority([p.audio_channels for p in video_probes if p.has_audio])

    for i, p in enumerate(probes):
        mismatches = []

        if p.video_codec != ref_codec:
            mismatches.append(f"video codec {p.video_codec} != {ref_codec}")
        if (p.width, p.height) != ref_res:
            mismatches.append(f"resolution {p.width}x{p.height} != {ref_res[0]}x{ref_res[1]}")
        if p.pix_fmt != ref_pix:
            mismatches.append(f"pix_fmt {p.pix_fmt} != {ref_pix}")
        if p.has_video and ref_fps and abs(round(p.fps, 1) - ref_fps) > 0.5:
            mismatches.append(f"fps {p.fps:.1f} != {ref_fps}")
        if p.has_audio and ref_asr and p.audio_sample_rate != ref_asr:
            mismatches.append(f"audio sample rate {p.audio_sample_rate} != {ref_asr}")
        if p.has_audio and ref_ach and p.audio_channels != ref_ach:
            mismatches.append(f"audio channels {p.audio_channels} != {ref_ach}")

        if mismatches:
            seg_name = p.path.stem
            warnings.append(f"Segment {seg_name}: {', '.join(mismatches)}")
            incompatible_indices.append(i)

    return warnings, incompatible_indices


def _reencode_segment(path: Path, output_format: OutputFormat | None) -> bool:
    """Re-encode a segment in-place to match the expected output parameters.

    Used when Layer 2 detects a segment that is individually valid but
    incompatible with the majority of other segments.
    """
    tmp = path.with_suffix(".reenc.mp4")
    target_w = output_format.width if output_format else 1920
    target_h = output_format.height if output_format else 1080
    target_fps = output_format.fps if output_format else 29.97

    sw_codec = output_format.codec if output_format else "libx264"
    if sw_codec == "auto":
        sw_codec = "libx264"
    codec = get_hwenc_codec(sw_codec)
    is_vt = codec.endswith("_videotoolbox")

    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())
    cmd.extend(["-i", str(path)])
    cmd.extend(
        [
            "-vf",
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,fps={target_fps}",
        ]
    )
    cmd.extend(["-c:v", codec])
    if is_vt:
        cmd.extend(["-q:v", "65", "-allow_sw", "1"])
        if codec == "hevc_videotoolbox":
            cmd.extend(["-tag:v", "hvc1"])
    else:
        cmd.extend(["-preset", "fast", "-crf", "23"])
        if codec == "libx264":
            cmd.extend(["-profile:v", "high", "-level", "4.2"])
    cmd.extend(["-force_key_frames", "expr:eq(n,0)"])
    cmd.extend(["-pix_fmt", "yuv420p"])
    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"])
    cmd.extend(["-movflags", "+faststart", str(tmp)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)
        return True
    if tmp.exists():
        tmp.unlink()
    return False


def _verify_rough_cut(
    rough_cut_path: Path,
    expected_duration: float,
    segment_count: int,
) -> list[str]:
    """Layer 3: Post-concat integrity verification.

    Checks that the final rough cut is a valid, playable MP4:
    - Has both video and audio streams
    - Duration matches sum of segments (±2s tolerance for concat rounding)
    - File size is reasonable (not truncated)
    - moov atom is present (faststart worked) via a fast seek test
    """
    errors = []

    if not rough_cut_path.exists():
        return ["Rough cut file does not exist"]

    size = rough_cut_path.stat().st_size
    if size == 0:
        return ["Rough cut file is empty (0 bytes)"]

    # Minimum sanity: at least 10KB per segment
    min_expected = segment_count * 10 * 1024
    if size < min_expected:
        errors.append(
            f"Rough cut suspiciously small: {size / 1024:.0f} KB "
            f"(expected at least {min_expected / 1024:.0f} KB for {segment_count} segments)"
        )

    # Probe the final output
    probe = _probe_segment(rough_cut_path)
    if not probe.has_video:
        errors.append("Rough cut has no video stream")
    if not probe.has_audio:
        errors.append("Rough cut has no audio stream")

    if expected_duration > 0 and probe.duration > 0:
        drift = abs(probe.duration - expected_duration)
        if drift > 2.0:
            errors.append(
                f"Rough cut duration {probe.duration:.1f}s vs expected {expected_duration:.1f}s "
                f"(drift {drift:.1f}s)"
            )

    # Seek tests: validate the file is playable at start and midpoint.
    # Use a generous timeout — large 4K files need time even with faststart.
    seek_timeout = max(30, int(size / (200 * 1024 * 1024)))  # 30s or 1s per 200MB

    for label, interval in [("start", "%+0.5"), ("midpoint", f"{probe.duration / 2}%+0.5")]:
        if probe.duration < 1.0:
            break
        try:
            seek_result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-read_intervals",
                    interval,
                    "-select_streams",
                    "v:0",
                    "-show_frames",
                    "-show_entries",
                    "frame=pkt_pts_time",
                    "-of",
                    "csv=p=0",
                    str(rough_cut_path),
                ],
                capture_output=True,
                text=True,
                timeout=seek_timeout,
            )
            if seek_result.returncode != 0:
                errors.append(
                    f"Seek test failed at {label}: ffprobe error "
                    f"(rc={seek_result.returncode}, stderr={seek_result.stderr[:200]})"
                )
            elif not seek_result.stdout.strip():
                errors.append(
                    f"Seek test failed at {label}: no decodable frames found "
                    f"— possible moov atom corruption or missing keyframes"
                )
        except subprocess.TimeoutExpired:
            errors.append(
                f"Seek test timed out at {label} ({seek_timeout}s) "
                f"— moov atom may be at end of file (faststart failed?)"
            )
        except FileNotFoundError:
            errors.append("ffprobe not found")
            break

    return errors


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

    # 3. FPS normalization — always apply to ensure uniform timebase across segments,
    # even when source FPS matches target (VFR sources, different timebases)
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


def _contains_cjk(text: str) -> bool:
    """Return True if text contains any CJK ideograph, kana, or hangul character."""
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
            or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
            or 0x20000 <= cp <= 0x2A6DF  # CJK Extension B
            or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
            or 0x3000 <= cp <= 0x303F  # CJK Symbols and Punctuation
            or 0x3040 <= cp <= 0x309F  # Hiragana
            or 0x30A0 <= cp <= 0x30FF  # Katakana
            or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables
        ):
            return True
    return False


def _intervals_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    """Return True if two time intervals overlap."""
    return a_start < b_end and b_start < a_end


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


def _resolve_macos_latin_font(style: str = "sans-serif") -> str:
    """Find a Latin-optimized font on macOS.

    Avenir Next is a clean geometric sans-serif ideal for English text.
    Falls back to CJK font if nothing found (CJK fonts render Latin fine).
    """
    if style in ("serif", "handwritten"):
        candidates = [
            "/System/Library/Fonts/Supplemental/Didot.ttc",
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Avenir Next.ttc",
            "/System/Library/Fonts/Supplemental/Avenir Next.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    for path in candidates:
        if Path(path).exists():
            return path
    return _resolve_macos_cjk_font(style)


def _resolve_macos_cjk_font(style: str = "sans-serif") -> str:
    """Find a CJK-capable font on macOS.

    PingFang is the primary system CJK sans-serif since El Capitan (10.11).
    Falls back through other CJK-capable system fonts.
    """
    if style == "serif":
        candidates = [
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
        ]
    for path in candidates:
        if Path(path).exists():
            return path
    # Absolute last resort — Avenir has no CJK but at least renders Latin
    return "/System/Library/Fonts/Avenir Next.ttc"


def _build_overlay_drawtext(overlays, output_format: OutputFormat | None = None) -> list[str]:
    """Build ffmpeg drawtext filter strings for a list of MonologueOverlay objects.

    Styled after Korean silent vlog typography: clean sans-serif, bright white,
    positioned in the lower portion of the frame with soft shadow for readability.
    """
    import platform

    # Font resolution — CJK-capable fonts for Chinese/Japanese/Korean, Latin fonts otherwise
    if platform.system() == "Darwin":
        cjk_font_map = {
            "sans-serif": _resolve_macos_cjk_font(),
            "handwritten": _resolve_macos_cjk_font(style="serif"),
        }
        latin_font_map = {
            "sans-serif": _resolve_macos_latin_font(),
            "handwritten": _resolve_macos_latin_font(style="handwritten"),
        }
    else:
        cjk_font_map = {
            "sans-serif": _resolve_font_path("sans-serif:lang=zh"),
            "handwritten": _resolve_font_path("serif:lang=zh"),
        }
        latin_font_map = {
            "sans-serif": _resolve_font_path("sans-serif"),
            "handwritten": _resolve_font_path("serif"),
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
        fmap = cjk_font_map if _contains_cjk(ov.text) else latin_font_map
        font_file = fmap.get(ov.style.font, fmap["sans-serif"])
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
    monologue_intervals: list[tuple[float, float]] | None = None,
) -> list[str]:
    """Build ffmpeg drawtext filters for speech captions from transcript segments.

    Only renders speech segments (no speaker labels). Timestamps are converted
    from clip-absolute to segment-relative.

    When a caption overlaps temporally with a monologue overlay, it is rendered
    in a subordinate style (smaller, top-positioned, slightly transparent) so the
    monologue takes visual priority while the caption remains readable.
    """
    import platform

    if platform.system() == "Darwin":
        cjk_font = _resolve_macos_cjk_font()
        latin_font = _resolve_macos_latin_font()
    else:
        cjk_font = _resolve_font_path("sans-serif:lang=zh")
        latin_font = _resolve_font_path("sans-serif")

    base_h = output_format.height if output_format else 1080
    normal_font_size = max(28, int(base_h * 0.038))
    subordinate_font_size = max(22, int(base_h * 0.030))

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

        # Check for temporal collision with monologue overlays
        is_colliding = False
        if monologue_intervals:
            for m_start, m_end in monologue_intervals:
                if _intervals_overlap(local_start, local_end, m_start, m_end):
                    is_colliding = True
                    break

        # Select font based on text content
        font_file = cjk_font if _contains_cjk(text) else latin_font

        # Lowercase to match monologue style
        escaped = _escape_drawtext(text.lower())

        if is_colliding:
            # Subordinate style: smaller, top-positioned, slightly transparent
            f = (
                f"drawtext=text='{escaped}'"
                f":fontfile='{font_file}'"
                f":fontsize={subordinate_font_size}"
                f":fontcolor=white@0.85"
                f":shadowcolor=black@0.5:shadowx=2:shadowy=2"
                f":x=(w-tw)/2:y=h*0.08"
                f":enable='between(t,{local_start:.2f},{local_end:.2f})'"
            )
        else:
            # Normal style: standard lower-third caption
            f = (
                f"drawtext=text='{escaped}'"
                f":fontfile='{font_file}'"
                f":fontsize={normal_font_size}"
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
    monologue_intervals: list[tuple[float, float]] = []
    if overlays:
        extra_filters.extend(_build_overlay_drawtext(overlays, output_format))
        for ov in overlays:
            monologue_intervals.append((ov.appear_at, ov.appear_at + ov.duration_sec))
    if caption_segments:
        extra_filters.extend(
            _build_caption_drawtext(
                caption_segments,
                in_sec,
                out_sec,
                output_format=output_format,
                monologue_intervals=monologue_intervals or None,
            )
        )

    if extra_filters:
        if vf:
            vf = vf + "," + ",".join(extra_filters)
        else:
            vf = ",".join(extra_filters)

    cmd = ["ffmpeg", "-y"]
    # NOTE: no -hwaccel videotoolbox here — HW decoder drops frames when
    # fast-seeking (-ss before -i), producing corrupt segments. Software
    # decode is fast enough since we only decode a few seconds per segment.
    # HW *encoding* (h264_videotoolbox) is still used for output.

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
    # Guarantee first frame is an IDR keyframe — required for concat -c:v copy
    cmd.extend(["-force_key_frames", "expr:eq(n,0)"])
    cmd.extend(["-pix_fmt", "yuv420p"])
    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"])
    cmd.extend(["-movflags", "+faststart", str(output_path)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning(
            "ffmpeg segment extraction failed for %s: %s", output_path.name, result.stderr[:500]
        )
    elif result.stderr:
        # Log warnings even on success — ffmpeg often warns about issues that
        # produce a technically valid but subtly broken file.
        # Filter out the version/config banner to surface only meaningful lines.
        warn_keywords = ("discarding", "discarded", "non monoton", "error", "invalid")
        relevant = [
            line
            for line in result.stderr.splitlines()
            if not line.startswith(("ffmpeg version", "  built with", "  configuration:", "  lib"))
            and any(kw in line.lower() for kw in warn_keywords)
        ]
        if relevant:
            log.warning(
                "ffmpeg warnings for %s:\n  %s", output_path.name, "\n  ".join(relevant[:10])
            )
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

    expected_durations: dict[int, float] = {}  # index → expected duration for Layer 1

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
            expected_durations[len(segment_files) - 1] = seg.duration_sec
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
            segment_files.append(seg_path)
            expected_durations[len(segment_files) - 1] = seg.duration_sec
        else:
            warnings.append(f"#{seg.index}: ffmpeg extraction failed")

    if not segment_files:
        raise RuntimeError("No segments extracted — cannot assemble rough cut")

    # -----------------------------------------------------------------------
    # Layer 1: Per-segment validation
    # -----------------------------------------------------------------------
    print(f"\n  Validating {len(segment_files)} segments...")
    probes: list[SegmentProbe] = []
    for i, seg_path in enumerate(segment_files):
        probe = _probe_segment(seg_path)
        probes.append(probe)
        seg_errors = _validate_segment(
            probe,
            expected_duration=expected_durations.get(i, 0),
            output_format=output_format,
            label=seg_path.stem,
        )
        for e in seg_errors:
            warnings.append(f"VALIDATION: {e}")

    valid_count = sum(1 for p in probes if p.has_video and p.has_audio)
    print(f"    {valid_count}/{len(probes)} segments have both video + audio streams")

    # -----------------------------------------------------------------------
    # Layer 2: Pre-concat compatibility matrix
    # -----------------------------------------------------------------------
    compat_warnings, incompat_indices = _check_segment_compatibility(probes)
    if compat_warnings:
        print(f"    {len(incompat_indices)} segment(s) have parameter mismatches:")
        for w in compat_warnings:
            print(f"      {w}")
            warnings.append(f"COMPAT: {w}")

        # Re-encode incompatible segments to match the majority
        for idx in incompat_indices:
            seg_path = segment_files[idx]
            print(f"    Re-encoding {seg_path.stem} for compatibility...")
            if _reencode_segment(seg_path, output_format):
                probes[idx] = _probe_segment(seg_path)
                print(
                    f"      OK — now {probes[idx].video_codec} {probes[idx].width}x{probes[idx].height}"
                )
            else:
                warnings.append(f"COMPAT: re-encode failed for {seg_path.stem}")
    else:
        print("    All segments are compatible for concatenation")

    # Concatenate
    rough_cut_path = version_dir / "rough_cut.mp4"
    concat_list = segments_dir / "concat_list.txt"
    concat_list.write_text("\n".join(f"file '{seg.resolve()}'" for seg in segment_files) + "\n")

    total_expected_dur = sum(p.duration for p in probes)
    print(f"\n  Concatenating {len(segment_files)} segments (~{total_expected_dur:.0f}s total)...")
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
            "-avoid_negative_ts",
            "make_zero",
            "-i",
            str(concat_list),
            "-c:v",
            "copy",
            "-c:a",
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

    # -----------------------------------------------------------------------
    # Layer 3: Post-concat integrity verification
    # -----------------------------------------------------------------------
    print("  Verifying rough cut integrity...")
    integrity_errors = _verify_rough_cut(rough_cut_path, total_expected_dur, len(segment_files))
    if integrity_errors:
        for e in integrity_errors:
            print(f"    WARNING: {e}")
            warnings.append(f"INTEGRITY: {e}")
    else:
        print("    Rough cut passed all integrity checks")

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
