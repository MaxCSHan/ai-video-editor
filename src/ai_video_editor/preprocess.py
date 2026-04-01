"""Video preprocessing pipeline — all operations via ffmpeg subprocess."""

import json
import math
import platform
import re
import subprocess
from pathlib import Path

from .config import PreprocessConfig, ProjectPaths


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def _parse_fps(fps_str: str) -> float:
    """Parse ffprobe fps fraction string (e.g. '30000/1001') to float."""
    if "/" in fps_str:
        num, den = fps_str.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(fps_str)
    except ValueError:
        return 0.0


def _classify_resolution(width: int, height: int) -> str:
    """Classify resolution by the longer dimension."""
    long_side = max(width, height)
    if long_side >= 3840:
        return "4K"
    if long_side >= 2560:
        return "QHD"
    if long_side >= 1920:
        return "FHD"
    if long_side >= 1280:
        return "HD"
    return "SD"


def _compute_aspect_ratio(width: int, height: int) -> str:
    """Compute simplified aspect ratio string (e.g. '16:9', '4:3')."""
    if width <= 0 or height <= 0:
        return "unknown"
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _detect_orientation(width: int, height: int) -> str:
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


# ---------------------------------------------------------------------------
# Hardware acceleration
# ---------------------------------------------------------------------------

_hwaccel_available: bool | None = None


def get_hwaccel_args() -> list[str]:
    """Return hardware-accelerated decode args for the current platform.

    Probes ffmpeg once to verify videotoolbox availability on macOS.
    """
    global _hwaccel_available
    if platform.system() != "Darwin":
        return []
    if _hwaccel_available is None:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hwaccels"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            _hwaccel_available = "videotoolbox" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            _hwaccel_available = False
    return ["-hwaccel", "videotoolbox"] if _hwaccel_available else []


def get_video_info(video_path: Path) -> dict:
    """Get video metadata (duration, resolution, codec, rotation, orientation) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), {})

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    codec = video_stream.get("codec_name", "unknown")
    fps_str = video_stream.get("r_frame_rate", "unknown")

    # Rotation: check side_data_list first, then tags.rotate
    rotation = 0
    for sd in video_stream.get("side_data_list", []):
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
            break
    if rotation == 0:
        rotation = abs(int(video_stream.get("tags", {}).get("rotate", 0)))

    # Display dimensions after rotation correction
    if rotation in (90, 270):
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height

    fps_float = _parse_fps(fps_str)
    orientation = _detect_orientation(display_width, display_height)
    aspect_ratio = _compute_aspect_ratio(display_width, display_height)
    resolution_class = _classify_resolution(display_width, display_height)

    # Color info for HDR detection
    color_transfer = video_stream.get("color_transfer", "")
    is_hdr = color_transfer in ("smpte2084", "arib-std-b67")

    return {
        # Original keys (backward compat)
        "duration_sec": float(data["format"]["duration"]),
        "width": width,
        "height": height,
        "codec": codec,
        "fps": fps_str,
        "filename": Path(data["format"]["filename"]).name,
        # New keys
        "rotation": rotation,
        "display_width": display_width,
        "display_height": display_height,
        "orientation": orientation,
        "aspect_ratio": aspect_ratio,
        "resolution_class": resolution_class,
        "fps_float": round(fps_float, 3),
        "bitrate": int(video_stream.get("bit_rate", 0) or 0),
        "pix_fmt": video_stream.get("pix_fmt", "unknown"),
        "color_transfer": color_transfer,
        "is_hdr": is_hdr,
    }


def ingest_source(video_path: Path, paths: ProjectPaths) -> Path:
    """Symlink source footage into the project's source/ directory.

    Uses a symlink instead of copying to avoid duplicating large 4K files.
    """
    dest = paths.source / video_path.name
    if not dest.exists():
        dest.symlink_to(video_path.resolve())
    return dest


def _rotation_vf(rotation: int) -> str:
    """Return ffmpeg video filter string for rotation correction, or empty string."""
    if rotation == 90:
        return "transpose=1,"
    if rotation == 180:
        return "hflip,vflip,"
    if rotation == 270:
        return "transpose=2,"
    return ""


def create_proxy(
    video_path: Path,
    paths: ProjectPaths,
    cfg: PreprocessConfig,
    rotation: int = 0,
) -> Path:
    """Downscale video to a lightweight proxy for AI analysis."""
    proxy_path = paths.proxy / f"{video_path.stem}_proxy.mp4"
    if proxy_path.exists():
        return proxy_path
    rot = _rotation_vf(rotation)
    # scale width to proxy_width, auto-calculate height preserving aspect ratio
    # -2 ensures even dimension for H.264 compatibility
    vf = f"{rot}scale={cfg.proxy_width}:-2,fps={cfg.proxy_fps}"
    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())
    cmd.extend(
        [
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            str(cfg.proxy_crf),
            "-c:a",
            "aac",
            "-b:a",
            cfg.proxy_audio_bitrate,
            "-movflags",
            "+faststart",
            str(proxy_path),
        ]
    )
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return proxy_path


def extract_frames(
    video_path: Path,
    paths: ProjectPaths,
    cfg: PreprocessConfig,
    rotation: int = 0,
) -> tuple[Path, dict]:
    """Extract frames at fixed intervals. Returns (frames_dir, manifest)."""
    frames_dir = paths.frames
    manifest_path = frames_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        return frames_dir, manifest
    rot = _rotation_vf(rotation)
    vf = f"fps=1/{cfg.frame_interval_sec},{rot}scale={cfg.frame_width}:-2"
    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())
    cmd.extend(
        [
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-q:v",
            str(cfg.frame_quality),
            str(frames_dir / "frame_%05d.jpg"),
        ]
    )
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    duration = get_video_duration(video_path)
    frame_files = sorted(frames_dir.glob("frame_*.jpg"))

    manifest = {
        "source": str(video_path.name),
        "duration_sec": duration,
        "interval_sec": cfg.frame_interval_sec,
        "frames": [],
    }
    for i, f in enumerate(frame_files):
        ts = i * cfg.frame_interval_sec
        manifest["frames"].append(
            {
                "index": i,
                "file": f.name,
                "timestamp_sec": ts,
                "timestamp_fmt": _fmt_timestamp(ts),
            }
        )

    manifest_path = frames_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return frames_dir, manifest


def detect_scenes(
    video_path: Path,
    paths: ProjectPaths,
    cfg: PreprocessConfig,
    rotation: int = 0,
) -> list[dict]:
    """Detect scene changes via ffmpeg's scene filter. Returns list of scene boundary info."""
    scenes_dir = paths.scenes
    manifest_path = scenes_dir / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())["scenes"]

    rot = _rotation_vf(rotation)
    vf = f"select='gt(scene,{cfg.scene_threshold})',showinfo,{rot}scale={cfg.frame_width}:-2"
    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())
    cmd.extend(
        [
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-vsync",
            "vfr",
            "-q:v",
            str(cfg.frame_quality),
            str(scenes_dir / "scene_%03d.jpg"),
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)

    scenes = []
    scene_files = sorted(scenes_dir.glob("scene_*.jpg"))
    pts_pattern = re.compile(r"pts_time:\s*([\d.]+)")
    matches = pts_pattern.findall(result.stderr)

    for i, (ts_str, scene_file) in enumerate(zip(matches, scene_files)):
        ts = float(ts_str)
        scenes.append(
            {
                "index": i,
                "file": scene_file.name,
                "timestamp_sec": ts,
                "timestamp_fmt": _fmt_timestamp(ts),
            }
        )

    scenes_manifest = {
        "source": str(video_path.name),
        "threshold": cfg.scene_threshold,
        "scene_count": len(scenes),
        "scenes": scenes,
    }
    (scenes_dir / "manifest.json").write_text(json.dumps(scenes_manifest, indent=2))
    return scenes


def extract_audio(video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig) -> Path:
    """Extract audio as mono WAV for transcription."""
    audio_path = paths.audio / f"{video_path.stem}.wav"
    if audio_path.exists():
        return audio_path
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(cfg.audio_sample_rate),
            "-ac",
            str(cfg.audio_channels),
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return audio_path


def generate_contact_sheet(
    video_path: Path,
    paths: ProjectPaths,
    cfg: PreprocessConfig,
    columns: int = 5,
    rows: int = 10,
    rotation: int = 0,
) -> Path:
    """Generate a single contact sheet image from scene-change keyframes."""
    sheet_path = paths.storyboard / "contact_sheet.jpg"
    if sheet_path.exists():
        return sheet_path
    rot = _rotation_vf(rotation)
    vf = f"select='gt(scene,{cfg.scene_threshold})',{rot}scale=180:-2,tile={columns}x{rows}"
    cmd = ["ffmpeg", "-y"]
    cmd.extend(get_hwaccel_args())
    cmd.extend(
        [
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(sheet_path),
        ]
    )
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return sheet_path


def run_full_preprocess(video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig) -> dict:
    """Run the complete preprocessing pipeline. Returns paths and metadata."""
    paths.ensure_dirs()

    video_info = get_video_info(video_path)
    rotation = video_info.get("rotation", 0)
    source_path = ingest_source(video_path, paths)
    proxy_path = create_proxy(source_path, paths, cfg, rotation=rotation)
    frames_dir, frames_manifest = extract_frames(source_path, paths, cfg, rotation=rotation)
    scenes = detect_scenes(source_path, paths, cfg, rotation=rotation)
    audio_path = extract_audio(source_path, paths, cfg)
    contact_sheet = generate_contact_sheet(source_path, paths, cfg, rotation=rotation)

    return {
        "video_info": video_info,
        "source_path": source_path,
        "proxy_path": proxy_path,
        "frames_dir": frames_dir,
        "frames_manifest": frames_manifest,
        "scenes": scenes,
        "audio_path": audio_path,
        "contact_sheet": contact_sheet,
    }


def _fmt_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
