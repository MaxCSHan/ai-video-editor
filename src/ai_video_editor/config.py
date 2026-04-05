"""Central configuration for the AI Video Editor pipeline."""

from dataclasses import dataclass, field
from pathlib import Path


# Top-level library directory where all projects live
LIBRARY_DIR = Path("library")

# Supported video extensions for clip discovery
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mts", ".m4v"}


@dataclass
class PreprocessConfig:
    # Proxy video settings (for Gemini upload)
    proxy_width: int = 360
    proxy_height: int = 240
    proxy_fps: int = 1
    proxy_crf: int = 28
    proxy_audio_bitrate: str = "64k"

    # Frame extraction settings (for Claude analysis)
    frame_interval_sec: int = 5
    frame_width: int = 360
    frame_height: int = 240
    frame_quality: int = 5  # ffmpeg -q:v (2=best, 31=worst)

    # Scene detection
    scene_threshold: float = 0.3  # 0.0-1.0, lower = more sensitive

    # Audio extraction
    audio_sample_rate: int = 16000
    audio_channels: int = 1


@dataclass
class OutputFormat:
    """Target output format for rough cut assembly."""

    width: int = 1920
    height: int = 1080
    fps: float = 29.97
    orientation: str = "landscape"  # "landscape" | "portrait"
    codec: str = "auto"  # "auto" | "libx264" | "libx265"
    fit_mode: str = "pad"  # "pad" (black bars) | "crop" (fill frame)
    label: str = "FHD 1080p"
    # Color target: "auto" picks based on device mix, "sdr" forces BT.709,
    # "hlg" preserves/converts to HLG/BT.2020
    color_target: str = "auto"

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "orientation": self.orientation,
            "codec": self.codec,
            "fit_mode": self.fit_mode,
            "label": self.label,
            "color_target": self.color_target,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OutputFormat":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ClaudeConfig:
    model: str = "claude-sonnet-4-20250514"
    max_images_per_batch: int = 20
    temperature: float = 0.2
    phase2_temperature: float = 0.6
    max_tokens: int = 4096


@dataclass
class GeminiConfig:
    model: str = "gemini-3-flash-preview"
    phase2_model: str | None = "gemini-3-flash-preview"
    structuring_model: str = "gemini-2.5-flash-lite"  # cheap model for Call 2A.5
    temperature: float = 0.2
    phase2_temperature: float = 0.6
    phase2b_temperature: float = 0.3  # assembly is mechanical, not creative
    use_split_pipeline: bool = False  # migration toggle for multi-call Phase 2

    @property
    def phase2(self) -> str:
        """Model to use for Phase 2 (editorial assembly). Falls back to self.model."""
        return self.phase2_model or self.model


@dataclass
class TranscribeConfig:
    # mlx-whisper settings (local)
    model: str = "mlx-community/whisper-large-v3-turbo"
    word_timestamps: bool = True
    language: str | None = None  # None = auto-detect
    # Provider selection: "auto" | "mlx" | "gemini"
    provider: str = "auto"
    # Gemini transcription settings (cloud)
    gemini_model: str = "gemini-3-flash-preview"


@dataclass
class ProjectPaths:
    """Per-project (or per-clip) directory layout."""

    root: Path

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def proxy(self) -> Path:
        return self.root / "proxy"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def scenes(self) -> Path:
        return self.root / "scenes"

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def review(self) -> Path:
        """Phase 1 clip review outputs (editorial mode)."""
        return self.root / "review"

    @property
    def storyboard(self) -> Path:
        return self.root / "storyboard"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def ensure_dirs(self):
        """Create per-clip working dirs. Storyboard/exports are project-level concerns."""
        for p in [self.source, self.proxy, self.frames, self.scenes, self.audio, self.review]:
            p.mkdir(parents=True, exist_ok=True)

    def has_source(self) -> bool:
        return any(self.source.glob("*")) if self.source.exists() else False

    def has_proxy(self) -> bool:
        return any(self.proxy.glob("*.mp4")) if self.proxy.exists() else False

    def has_frames(self) -> bool:
        return (self.frames / "manifest.json").exists()

    def has_scenes(self) -> bool:
        return (self.scenes / "manifest.json").exists()

    def has_audio(self) -> bool:
        return any(self.audio.glob("*.wav")) if self.audio.exists() else False

    def has_review(self, provider: str = "gemini") -> bool:
        return (self.review / f"review_{provider}.json").exists()

    def has_transcript(self) -> bool:
        return (self.audio / "transcript_latest.json").exists() or (
            self.audio / "transcript.json"
        ).exists()

    def cache_status(self) -> dict[str, bool]:
        return {
            "source": self.has_source(),
            "proxy": self.has_proxy(),
            "frames": self.has_frames(),
            "scenes": self.has_scenes(),
            "audio": self.has_audio(),
        }


@dataclass
class EditorialProjectPaths:
    """Multi-clip editorial project layout.

    Supports experiment tracks: named tracks store AI-generated artifacts
    (storyboard, monologue, exports) in subdirectories, while sharing
    preprocessing, transcription, and briefing with the main track.
    """

    root: Path
    track: str = "main"

    @property
    def clips_dir(self) -> Path:
        """Parent directory for all per-clip subdirectories."""
        return self.root / "clips"

    @property
    def storyboard(self) -> Path:
        base = self.root / "storyboard"
        if self.track != "main":
            return base / self.track
        return base

    @property
    def exports(self) -> Path:
        base = self.root / "exports"
        if self.track != "main":
            return base / self.track
        return base

    @property
    def master_manifest(self) -> Path:
        return self.root / "manifest.json"

    def clip_paths(self, clip_id: str) -> ProjectPaths:
        """Get ProjectPaths for a specific clip."""
        return ProjectPaths(root=self.clips_dir / clip_id)

    def with_track(self, track: str) -> "EditorialProjectPaths":
        """Return a copy of this paths object pointing to a different track."""
        return EditorialProjectPaths(root=self.root, track=track)

    def discover_clips(self) -> list[str]:
        """List clip IDs that have been ingested.

        Accepts clips with either a source/ or proxy/ directory so that
        clips remain discoverable when the source drive is offline
        (broken symlinks in source/).
        """
        if not self.clips_dir.exists():
            return []
        return sorted(
            d.name
            for d in self.clips_dir.iterdir()
            if d.is_dir() and ((d / "source").exists() or (d / "proxy").exists())
        )

    def ensure_dirs(self):
        for p in [self.clips_dir, self.storyboard, self.exports]:
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class ReviewConfig:
    """Configuration for the Editorial Director review loop."""

    enabled: bool = True
    model: str = "gemini-2.5-flash"
    max_turns: int = 15
    max_fixes: int = 10
    max_review_cost_usd: float = 0.50
    wall_clock_timeout_sec: float = 180.0
    human_checkpoint_on_uncertainty: bool = True


@dataclass
class ReviewBudget:
    """Mutable budget tracker for a single review session."""

    max_turns: int = 15
    max_fixes: int = 10
    max_cost_usd: float = 0.50
    turns_used: int = 0
    fixes_used: int = 0
    cost_used_usd: float = 0.0

    def can_continue(self) -> bool:
        return (
            self.turns_used < self.max_turns
            and self.fixes_used < self.max_fixes
            and self.cost_used_usd < self.max_cost_usd
        )

    def remaining_summary(self) -> str:
        """Injected into system prompt so agent sees its budget."""
        return (
            f"Budget: {self.max_turns - self.turns_used} turns, "
            f"{self.max_fixes - self.fixes_used} fixes, "
            f"${self.max_cost_usd - self.cost_used_usd:.3f} remaining"
        )

    @classmethod
    def from_config(cls, cfg: "ReviewConfig") -> "ReviewBudget":
        return cls(
            max_turns=cfg.max_turns,
            max_fixes=cfg.max_fixes,
            max_cost_usd=cfg.max_review_cost_usd,
        )


@dataclass
class Config:
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    library_dir: Path = field(default_factory=lambda: LIBRARY_DIR)

    def project(self, name: str) -> ProjectPaths:
        """Get paths for a single-video project."""
        return ProjectPaths(root=self.library_dir / name)

    def editorial_project(self, name: str) -> EditorialProjectPaths:
        """Get paths for a multi-clip editorial project."""
        return EditorialProjectPaths(root=self.library_dir / name)


DEFAULT_CONFIG = Config()
