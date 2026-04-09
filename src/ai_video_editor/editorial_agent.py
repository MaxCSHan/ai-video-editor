"""Editorial Storyboard Agent — multi-clip analysis and creative assembly planning."""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import (
    VIDEO_EXTENSIONS,
    Config,
    EditorialProjectPaths,
    PreprocessConfig,
    TranscribeConfig,
    DEFAULT_CONFIG,
)
from .preprocess import (
    create_proxy,
    extract_frames,
    detect_scenes,
    extract_audio,
    get_video_info,
    ingest_source,
)
from .storyboard_format import format_duration


# ---------------------------------------------------------------------------
# Clip discovery
# ---------------------------------------------------------------------------


def discover_source_clips(source_dir: Path) -> list[Path]:
    """Find all video files in a directory, sorted by name."""
    clips = [
        f
        for f in sorted(source_dir.iterdir())
        if f.is_file()
        and f.suffix.lower() in VIDEO_EXTENSIONS
        and not f.name.startswith("._")  # macOS resource fork files
    ]
    return clips


def discover_clips_from_manifest(
    editorial_paths: "EditorialProjectPaths",
) -> tuple[list[dict], dict]:
    """Load clip metadata from manifest.json for offline mode.

    Returns (clip_metadata_list, manifest_dict).  Both are empty when
    no manifest exists (project was never preprocessed).
    """
    manifest_path = editorial_paths.master_manifest
    if not manifest_path.exists():
        return [], {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        print(f"  WARN: corrupt manifest.json ({e}), treating as empty")
        return [], {}
    return manifest.get("clips", []), manifest


# ---------------------------------------------------------------------------
# Multi-clip preprocessing
# ---------------------------------------------------------------------------


def _preprocess_single_clip(
    clip_file: Path,
    editorial_paths: EditorialProjectPaths,
    cfg: PreprocessConfig,
    index: int,
    total: int,
) -> dict:
    """Preprocess a single clip. Returns clip metadata dict."""
    clip_id = clip_file.stem
    clip_paths = editorial_paths.clip_paths(clip_id)
    clip_paths.ensure_dirs()

    cache = clip_paths.cache_status()
    all_cached = all(cache.values())

    label = f"[{index}/{total}] {clip_id}"
    if all_cached:
        print(f"  {label}: cached")
    else:
        print(f"  {label}: preprocessing...")

    source = ingest_source(clip_file, clip_paths)
    video_info = get_video_info(source)
    rotation = video_info.get("rotation", 0)
    proxy_path = create_proxy(source, clip_paths, cfg, rotation=rotation)
    extract_frames(source, clip_paths, cfg, rotation=rotation)
    detect_scenes(source, clip_paths, cfg, rotation=rotation)
    extract_audio(source, clip_paths, cfg)

    if not all_cached:
        print(f"  {label}: done")

    return {
        "clip_id": clip_id,
        "filename": clip_file.name,
        "source_path": str(clip_file.resolve()),
        "duration_sec": video_info["duration_sec"],
        "width": video_info["width"],
        "height": video_info["height"],
        "resolution": f"{video_info['display_width']}x{video_info['display_height']}",
        "codec": video_info["codec"],
        "fps": video_info["fps"],
        "proxy_path": str(proxy_path),
        # Format-aware fields
        "rotation": video_info["rotation"],
        "display_width": video_info["display_width"],
        "display_height": video_info["display_height"],
        "orientation": video_info["orientation"],
        "aspect_ratio": video_info["aspect_ratio"],
        "resolution_class": video_info["resolution_class"],
        "fps_float": video_info["fps_float"],
        "is_hdr": video_info["is_hdr"],
        "creation_time": video_info.get("creation_time"),
    }


# Max parallel ffmpeg processes (avoid saturating CPU/disk)
MAX_PREPROCESS_WORKERS = 4

# Max parallel LLM API calls
MAX_LLM_WORKERS = 5


def preprocess_all_clips(
    clip_files: list[Path],
    editorial_paths: EditorialProjectPaths,
    cfg: PreprocessConfig,
) -> list[dict]:
    """Preprocess all clips in parallel. Returns list of clip metadata."""
    total = len(clip_files)

    # Submit all clips to thread pool
    futures = {}
    with ThreadPoolExecutor(max_workers=MAX_PREPROCESS_WORKERS) as pool:
        for i, clip_file in enumerate(clip_files):
            fut = pool.submit(
                _preprocess_single_clip, clip_file, editorial_paths, cfg, i + 1, total
            )
            futures[fut] = clip_file.stem

        # Collect results preserving input order
        results_by_id = {}
        failed_ids = []
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                results_by_id[clip_id] = fut.result()
            except Exception as e:
                print(f"  ERROR preprocessing {clip_id}: {e}")
                failed_ids.append(clip_id)

    if failed_ids:
        print(
            f"\n  WARNING: {len(failed_ids)}/{total} clips failed preprocessing: "
            f"{', '.join(failed_ids)}"
        )
        if len(failed_ids) > total // 2:
            raise RuntimeError(
                f"Too many preprocessing failures ({len(failed_ids)}/{total}). Aborting."
            )

    # Return in original file order
    return [results_by_id[f.stem] for f in clip_files if f.stem in results_by_id]


def build_master_manifest(
    clip_metadata: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
) -> dict:
    """Write and return the master manifest aggregating all clips."""
    total_duration = sum(c["duration_sec"] for c in clip_metadata)
    manifest = {
        "project": project_name,
        "clip_count": len(clip_metadata),
        "total_duration_sec": total_duration,
        "total_duration_fmt": format_duration(total_duration),
        "clips": clip_metadata,
    }
    editorial_paths.master_manifest.write_text(json.dumps(manifest, indent=2))
    return manifest


# ---------------------------------------------------------------------------
# Transcription (mlx-whisper local or Gemini cloud)
# ---------------------------------------------------------------------------

MAX_TRANSCRIBE_WORKERS_MLX = 2  # Whisper uses ~3GB RAM per instance
MAX_TRANSCRIBE_WORKERS_GEMINI = MAX_LLM_WORKERS  # API-bound, not RAM-bound


def _resolve_transcribe_provider(cfg: TranscribeConfig) -> str | None:
    """Resolve transcription provider from config. Returns 'mlx', 'gemini', or None."""
    if cfg.provider == "mlx":
        return "mlx"
    if cfg.provider == "gemini":
        return "gemini"
    # auto: try mlx first, then gemini
    try:
        import mlx_whisper  # noqa: F401

        return "mlx"
    except ImportError:
        pass
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return None


def _transcribe_single_clip(
    clip_id: str,
    editorial_paths: EditorialProjectPaths,
    cfg: TranscribeConfig,
    provider: str,
    index: int,
    total: int,
    speaker_context: str | None = None,
    tracer=None,
) -> tuple[str, dict | None]:
    """Transcribe a single clip. Returns (clip_id, transcript_dict)."""
    clip_paths = editorial_paths.clip_paths(clip_id)
    label = f"[{index}/{total}] {clip_id}"

    if clip_paths.has_transcript():
        from .versioning import resolve_transcript_path

        print(f"  {label}: transcript cached")
        transcript_path = resolve_transcript_path(clip_paths.root)
        if transcript_path:
            return clip_id, json.loads(transcript_path.read_text())

    if provider == "gemini":
        from .transcribe import transcribe_clip_gemini

        # Find proxy video
        proxy_files = list(clip_paths.proxy.glob("*_proxy.mp4"))
        if not proxy_files:
            print(f"  {label}: no proxy found, skipping")
            return clip_id, None

        print(f"  {label}: transcribing (gemini)...")
        transcript = transcribe_clip_gemini(
            proxy_files[0],
            clip_paths,
            cfg,
            speaker_context=speaker_context,
            tracer=tracer,
            editorial_paths=editorial_paths,
        )
    else:
        from .transcribe import transcribe_clip

        # Find audio WAV file
        wav_files = list(clip_paths.audio.glob("*.wav"))
        if not wav_files:
            print(f"  {label}: no audio found, skipping")
            return clip_id, None

        print(f"  {label}: transcribing (mlx)...")
        transcript = transcribe_clip(wav_files[0], clip_paths, cfg)

    if transcript:
        speech = "speech" if transcript.get("has_speech") else "no speech"
        print(f"  {label}: done ({speech})")
    return clip_id, transcript


def transcribe_all_clips(
    clip_metadata: list[dict],
    editorial_paths: EditorialProjectPaths,
    cfg: TranscribeConfig,
    provider: str = "mlx",
    speaker_context: str | None = None,
    tracer=None,
) -> dict[str, dict]:
    """Transcribe all clips in parallel. Returns {clip_id: transcript_dict}."""
    total = len(clip_metadata)
    results = {}

    max_workers = (
        MAX_TRANSCRIBE_WORKERS_GEMINI if provider == "gemini" else MAX_TRANSCRIBE_WORKERS_MLX
    )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, clip_info in enumerate(clip_metadata):
            fut = pool.submit(
                _transcribe_single_clip,
                clip_info["clip_id"],
                editorial_paths,
                cfg,
                provider,
                i + 1,
                total,
                speaker_context,
                tracer,
            )
            futures[fut] = clip_info["clip_id"]

        failed_ids = []
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                _, transcript = fut.result()
                if transcript:
                    results[clip_id] = transcript
            except Exception as e:
                print(f"  ERROR transcribing {clip_id}: {e}")
                failed_ids.append(clip_id)

    if failed_ids:
        print(
            f"\n  WARNING: {len(failed_ids)}/{total} clips failed transcription: "
            f"{', '.join(failed_ids)}"
        )

    return results


def _load_transcript_for_prompt(clip_paths) -> str | None:
    """Load and format a clip's transcript for LLM prompt injection."""
    from .versioning import resolve_transcript_path

    transcript_path = resolve_transcript_path(clip_paths.root)
    if not transcript_path:
        return None
    from .transcribe import format_transcript_for_prompt

    try:
        transcript = json.loads(transcript_path.read_text())
    except json.JSONDecodeError:
        return None
    if not transcript.get("has_speech"):
        return None
    return format_transcript_for_prompt(transcript)


def _load_all_transcripts_for_prompt(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    max_chars_per_clip: int = 2000,
) -> dict[str, str] | None:
    """Load formatted transcripts for all clips. Returns {clip_id: text} or None."""
    from .transcribe import format_transcript_for_prompt

    from .versioning import resolve_transcript_path

    transcripts = {}
    for review in clip_reviews:
        clip_id = review.get("clip_id", "")
        clip_paths = editorial_paths.clip_paths(clip_id)
        transcript_path = resolve_transcript_path(clip_paths.root)
        if transcript_path:
            try:
                t = json.loads(transcript_path.read_text())
            except json.JSONDecodeError:
                continue
            if t.get("has_speech"):
                transcripts[clip_id] = format_transcript_for_prompt(t, max_chars_per_clip)

    return transcripts if transcripts else None


# ---------------------------------------------------------------------------
# Re-exports for backward compatibility — callers can still do:
#   from .editorial_agent import run_phase1, run_phase2, run_monologue
# ---------------------------------------------------------------------------

from .editorial_phase1 import (  # noqa: F401
    run_phase1,
    run_phase1_gemini,
    run_phase1_claude,
)
from .editorial_phase2 import run_phase2  # noqa: F401
from .editorial_phase3 import run_monologue  # noqa: F401


# ---------------------------------------------------------------------------
# Phase 1 retry helper
# ---------------------------------------------------------------------------


def _retry_failed_phase1(
    failed: list[str],
    reviews: list[dict],
    editorial_paths,
    manifest: dict,
    provider: str,
    cfg,
    tracer=None,
    style_supplement: str | None = None,
    interactive: bool = True,
    user_context: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Prompt user to retry failed Phase 1 clips. Returns updated (reviews, failed)."""
    while failed:
        print(f"\n  {len(failed)} clip(s) failed: {', '.join(failed)}")
        if not interactive:
            break
        try:
            import questionary

            retry = questionary.confirm(
                "Retry failed clips?",
                default=True,
            ).ask()
        except (ImportError, EOFError):
            break
        if not retry:
            break

        print(f"\n  Retrying {len(failed)} clip(s)...\n")
        reviews, failed = run_phase1(
            editorial_paths,
            manifest,
            provider,
            cfg,
            tracer=tracer,
            style_supplement=style_supplement,
            only_clip_ids=failed,
            user_context=user_context,
        )
    return reviews, failed


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def run_editorial_pipeline(
    source_dir: Path,
    project_name: str,
    provider: str = "gemini",
    style: str = "vlog",
    cfg: Config | None = None,
    force: bool = False,
    interactive: bool = True,
    visual: bool = False,
    style_preset=None,
    included_clips: list[str] | None = None,
    max_cost: float | None = None,
) -> Path:
    """Full editorial pipeline: discover → preprocess → Phase 1 → Phase 2 → optional Phase 3."""
    import uuid

    from .tracing import ProjectTracer, otel_pipeline_span

    cfg = cfg or DEFAULT_CONFIG
    editorial_paths = cfg.editorial_project(project_name)
    editorial_paths.ensure_dirs()

    tracer = ProjectTracer(editorial_paths.root, max_cost_usd=max_cost)
    pipeline_run_id = str(uuid.uuid4())

    # Resolve style supplements from preset
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None
    has_phase3 = style_preset.has_phase3 if style_preset else False

    total_phases = 5 if has_phase3 else 4

    # Start pipeline-level OTel grouping (no-op if Phoenix not connected)
    _pipeline_ctx = otel_pipeline_span(project_name, pipeline_run_id)
    _pipeline_ctx.__enter__()

    # Discover clips
    print(f"[1/{total_phases}] Discovering clips in {source_dir}...")
    all_clip_files = discover_source_clips(source_dir)
    if included_clips:
        included_set = set(included_clips)
        clip_files = [c for c in all_clip_files if c.stem in included_set]
    else:
        clip_files = all_clip_files
    if not clip_files:
        raise RuntimeError(f"No video files found in {source_dir}")
    total_label = ", ".join(f.name for f in clip_files[:5])
    if len(clip_files) > 5:
        total_label += f", ... ({len(clip_files)} total)"
    print(f"  Found {len(clip_files)} clips: {total_label}")

    # Preprocess all clips
    print(f"[2/{total_phases}] Preprocessing {len(clip_files)} clips...")
    clip_metadata = preprocess_all_clips(clip_files, editorial_paths, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, editorial_paths, project_name)
    total_dur = format_duration(manifest["total_duration_sec"])
    print(f"  Total raw footage: {total_dur}")

    use_smart_briefing = interactive and bool(os.environ.get("GEMINI_API_KEY"))

    # Smart briefing BEFORE transcription (Gemini path) — uploads proxies and
    # populates the shared File API cache, plus gathers user context (speaker names,
    # highlights) that improves transcription and Phase 1 quality.
    user_context = None
    if use_smart_briefing:
        from .briefing import run_smart_briefing

        user_context = run_smart_briefing(
            editorial_paths,
            style,
            gemini_model=cfg.transcribe.gemini_model,
            tracer=tracer,
        )

    # Transcription (optional — mlx-whisper local or Gemini cloud)
    t_provider = _resolve_transcribe_provider(cfg.transcribe)
    if t_provider:
        # Load speaker context from briefing if available
        from .versioning import resolve_user_context_path

        speaker_context = None
        context_path = resolve_user_context_path(editorial_paths.root)
        if context_path:
            ctx = json.loads(context_path.read_text())
            speaker_context = ctx.get("people", "") or None

        print(f"  Transcribing audio ({t_provider})...")
        transcripts = transcribe_all_clips(
            clip_metadata,
            editorial_paths,
            cfg.transcribe,
            t_provider,
            speaker_context,
            tracer=tracer,
        )
        count = len(transcripts)
        print(f"  Transcribed {count}/{len(clip_metadata)} clips with speech")
    else:
        print("  Skipping transcription (no provider: install mlx-whisper or set GEMINI_API_KEY)")

    # Phase 1 — per-clip reviews (with retry loop)
    print(f"[3/{total_phases}] Phase 1: Reviewing clips with {provider}...")
    reviews, failed = run_phase1(
        editorial_paths,
        manifest,
        provider,
        cfg,
        force=force,
        tracer=tracer,
        style_supplement=p1_supplement,
        user_context=user_context,
    )
    reviews, failed = _retry_failed_phase1(
        failed,
        reviews,
        editorial_paths,
        manifest,
        provider,
        cfg,
        tracer=tracer,
        style_supplement=p1_supplement,
        interactive=interactive,
        user_context=user_context,
    )
    print(f"  Reviewed {len(reviews)} clips")
    tracer.print_summary("Phase 1")

    # Manual briefing AFTER Phase 1 (non-Gemini path) — needs Phase 1 reviews
    # to generate smart questions about detected people and highlights.
    if interactive and not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, editorial_paths.root)

    # Fallback: load existing user_context from disk if not gathered this run
    if user_context is None:
        from .versioning import resolve_user_context_path as _rucp_fallback

        _ctx_path = _rucp_fallback(editorial_paths.root)
        if _ctx_path:
            user_context = json.loads(_ctx_path.read_text())
            print(f"  Loaded existing user context: {_ctx_path.name}")

    # Phase 2 — editorial assembly
    print(f"[4/{total_phases}] Phase 2: Generating editorial storyboard...")
    output_path = run_phase2(
        clip_reviews=reviews,
        editorial_paths=editorial_paths,
        project_name=project_name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
        tracer=tracer,
        visual=visual,
        style_supplement=p2_supplement,
        review_config=cfg.review,
        interactive=interactive,
    )

    tracer.print_summary("Phase 2")

    # Phase 3 — visual monologue (if style preset has it)
    if has_phase3:
        print(f"[5/{total_phases}] Phase 3: Generating visual monologue...")
        run_monologue(
            editorial_paths=editorial_paths,
            provider=provider,
            gemini_cfg=cfg.gemini,
            claude_cfg=cfg.claude,
            style_preset=style_preset,
            user_context=user_context,
            tracer=tracer,
        )
        tracer.print_summary("Phase 3")

    tracer.print_summary("Pipeline Total")
    _pipeline_ctx.__exit__(None, None, None)
    return output_path
