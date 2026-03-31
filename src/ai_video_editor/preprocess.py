"""Video preprocessing pipeline — all operations via ffmpeg subprocess."""

import json
import re
import subprocess
from pathlib import Path

from .config import PreprocessConfig, ProjectPaths


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def get_video_info(video_path: Path) -> dict:
    """Get video metadata (duration, resolution, codec) via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    video_stream = next(
        (s for s in data["streams"] if s["codec_type"] == "video"), {}
    )
    return {
        "duration_sec": float(data["format"]["duration"]),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "codec": video_stream.get("codec_name", "unknown"),
        "fps": video_stream.get("r_frame_rate", "unknown"),
        "filename": Path(data["format"]["filename"]).name,
    }


def ingest_source(video_path: Path, paths: ProjectPaths) -> Path:
    """Copy or link source footage into the project's source/ directory."""
    import shutil
    dest = paths.source / video_path.name
    if not dest.exists():
        shutil.copy2(video_path, dest)
    return dest


def create_proxy(video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig) -> Path:
    """Downscale video to a lightweight proxy for AI analysis."""
    proxy_path = paths.proxy / f"{video_path.stem}_proxy.mp4"
    if proxy_path.exists():
        return proxy_path
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"scale={cfg.proxy_width}:{cfg.proxy_height},fps={cfg.proxy_fps}",
            "-c:v", "libx264", "-preset", "fast", "-crf", str(cfg.proxy_crf),
            "-c:a", "aac", "-b:a", cfg.proxy_audio_bitrate,
            "-movflags", "+faststart",
            str(proxy_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return proxy_path


def extract_frames(
    video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig
) -> tuple[Path, dict]:
    """Extract frames at fixed intervals. Returns (frames_dir, manifest)."""
    frames_dir = paths.frames
    manifest_path = frames_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        return frames_dir, manifest
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps=1/{cfg.frame_interval_sec},scale={cfg.frame_width}:{cfg.frame_height}",
            "-q:v", str(cfg.frame_quality),
            str(frames_dir / "frame_%05d.jpg"),
        ],
        capture_output=True, text=True, check=True,
    )

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
        manifest["frames"].append({
            "index": i,
            "file": f.name,
            "timestamp_sec": ts,
            "timestamp_fmt": _fmt_timestamp(ts),
        })

    manifest_path = frames_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return frames_dir, manifest


def detect_scenes(
    video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig
) -> list[dict]:
    """Detect scene changes via ffmpeg's scene filter. Returns list of scene boundary info."""
    scenes_dir = paths.scenes
    manifest_path = scenes_dir / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())["scenes"]

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf",
            f"select='gt(scene,{cfg.scene_threshold})',showinfo,scale={cfg.frame_width}:{cfg.frame_height}",
            "-vsync", "vfr",
            "-q:v", str(cfg.frame_quality),
            str(scenes_dir / "scene_%03d.jpg"),
        ],
        capture_output=True, text=True,
    )

    scenes = []
    scene_files = sorted(scenes_dir.glob("scene_*.jpg"))
    pts_pattern = re.compile(r"pts_time:\s*([\d.]+)")
    matches = pts_pattern.findall(result.stderr)

    for i, (ts_str, scene_file) in enumerate(zip(matches, scene_files)):
        ts = float(ts_str)
        scenes.append({
            "index": i,
            "file": scene_file.name,
            "timestamp_sec": ts,
            "timestamp_fmt": _fmt_timestamp(ts),
        })

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
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(cfg.audio_sample_rate),
            "-ac", str(cfg.audio_channels),
            str(audio_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return audio_path


def generate_contact_sheet(
    video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig, columns: int = 5, rows: int = 10
) -> Path:
    """Generate a single contact sheet image from scene-change keyframes."""
    sheet_path = paths.storyboard / "contact_sheet.jpg"
    if sheet_path.exists():
        return sheet_path
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf",
            f"select='gt(scene,{cfg.scene_threshold})',scale=180:120,tile={columns}x{rows}",
            "-frames:v", "1",
            "-q:v", "3",
            str(sheet_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return sheet_path


def run_full_preprocess(video_path: Path, paths: ProjectPaths, cfg: PreprocessConfig) -> dict:
    """Run the complete preprocessing pipeline. Returns paths and metadata."""
    paths.ensure_dirs()

    video_info = get_video_info(video_path)
    source_path = ingest_source(video_path, paths)
    proxy_path = create_proxy(source_path, paths, cfg)
    frames_dir, frames_manifest = extract_frames(source_path, paths, cfg)
    scenes = detect_scenes(source_path, paths, cfg)
    audio_path = extract_audio(source_path, paths, cfg)
    contact_sheet = generate_contact_sheet(source_path, paths, cfg)

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
