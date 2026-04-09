"""Editorial Storyboard Agent — multi-clip analysis and creative assembly planning."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import (
    VIDEO_EXTENSIONS,
    ClaudeConfig,
    Config,
    EditorialProjectPaths,
    GeminiConfig,
    PreprocessConfig,
    ReviewConfig,
    TranscribeConfig,
    DEFAULT_CONFIG,
)
from .editorial_prompts import (
    build_clip_review_prompt,
    build_editorial_assembly_prompt,
    parse_clip_review,
)
from .versioning import (
    begin_version,
    commit_version,
    versioned_path,
    versioned_dir,
    update_latest_symlink,
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
from .file_cache import (
    load_file_api_cache,
    cache_file_uri,
    get_cached_uri,
)


GEMINI_UPLOAD_TIMEOUT_SEC = 300  # 5 minutes


def _wait_for_gemini_file(video_file, client, timeout_sec: int = GEMINI_UPLOAD_TIMEOUT_SEC):
    """Poll until Gemini file processing completes, with timeout."""
    start = time.monotonic()
    while video_file.state.name == "PROCESSING":
        if time.monotonic() - start > timeout_sec:
            raise TimeoutError(
                f"Gemini file processing timed out after {timeout_sec}s for {video_file.name}"
            )
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)
    return video_file


def _require_api_key(name: str) -> str:
    """Get a required API key from the environment, or raise with a helpful message."""
    key = os.environ.get(name)
    if not key:
        raise RuntimeError(f"{name} is not set. Add it to your .env file (see .env.example).")
    return key


def _get_gemini_client():
    """Create a Gemini client with the API key from the environment."""
    from google import genai

    return genai.Client(api_key=_require_api_key("GEMINI_API_KEY"))


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
# Phase 1 — Per-clip review
# ---------------------------------------------------------------------------


def _review_single_clip_gemini(
    clip_info: dict,
    editorial_paths: EditorialProjectPaths,
    cfg: GeminiConfig,
    force: bool,
    index: int,
    total: int,
    tracer=None,
    style_supplement: str | None = None,
    user_context: dict | None = None,
) -> dict | None:
    """Review a single clip via Gemini. Returns review dict or None on failure."""
    from google.genai import types

    clip_id = clip_info["clip_id"]
    clip_paths = editorial_paths.clip_paths(clip_id)
    label = f"[{index}/{total}] {clip_id}"

    # Check cache
    latest_review = clip_paths.review / "review_gemini_latest.json"
    legacy_review = clip_paths.review / "review_gemini.json"
    if not force and (latest_review.exists() or legacy_review.exists()):
        cached = latest_review if latest_review.exists() else legacy_review
        try:
            print(f"  {label}: review cached")
            return json.loads(cached.read_text())
        except json.JSONDecodeError:
            print(f"  {label}: corrupt cache, will re-review")

    client = _get_gemini_client()

    # Check file cache before uploading (may already be cached by briefing or transcription)
    file_cache = load_file_api_cache(editorial_paths)
    cached_uri = get_cached_uri(file_cache, clip_id)

    if cached_uri:
        file_uri = cached_uri
        print(f"  {label}: using cached proxy URI")
    else:
        print(f"  {label}: uploading proxy...")
        proxy_path = Path(clip_info["proxy_path"])
        video_file = client.files.upload(file=str(proxy_path))
        video_file = _wait_for_gemini_file(video_file, client)

        if video_file.state.name == "FAILED":
            print(f"  {label}: WARNING — Gemini processing failed, skipping")
            return None

        file_uri = video_file.uri
        cache_file_uri(editorial_paths, clip_id, file_uri)

    # Load transcript if available
    transcript_text = _load_transcript_for_prompt(clip_paths)

    prompt = build_clip_review_prompt(
        clip_id=clip_id,
        filename=clip_info["filename"],
        duration_sec=clip_info["duration_sec"],
        resolution=clip_info["resolution"],
        orientation=clip_info.get("orientation"),
        aspect_ratio=clip_info.get("aspect_ratio"),
        transcript_text=transcript_text,
        style_supplement=style_supplement,
        user_context=user_context,
        include_json_template=False,
    )

    print(f"  {label}: reviewing with {cfg.model}...")
    from .tracing import otel_phase_span, traced_gemini_generate

    from .models import ClipReview

    with otel_phase_span("phase1", stage="review", provider="gemini", clip_id=clip_id):
        response = traced_gemini_generate(
            client,
            model=cfg.model,
            contents=[
                types.Content(
                    parts=[
                        types.Part.from_uri(file_uri=file_uri, mime_type="video/mp4"),
                        types.Part.from_text(text=prompt),
                    ]
                )
            ],
            config=types.GenerateContentConfig(
                temperature=cfg.temperature,
                response_mime_type="application/json",
                response_schema=ClipReview,
            ),
            phase="phase1",
            clip_id=clip_id,
            tracer=tracer,
            num_video_files=1,
            prompt_chars=len(prompt),
        )

    review = ClipReview.model_validate_json(response.text).model_dump()

    # Validate review quality
    val_warnings, val_critical = validate_clip_review(review, clip_info)
    if val_warnings:
        print(f"  {label}: validation warnings: {'; '.join(val_warnings[:3])}")
    if tracer and tracer.traces:
        tracer.traces[-1].validation_warnings = val_warnings

    # Auto-retry once on critical validation failure
    if val_critical:
        feedback = "Your previous response had issues:\n" + "\n".join(
            f"- {w}" for w in val_warnings
        )
        retry_prompt = prompt + f"\n\n{feedback}\nPlease fix these issues."
        print(f"  {label}: critical validation issues, retrying with feedback...")
        with otel_phase_span(
            "phase1_retry",
            stage="review",
            provider="gemini",
            clip_id=clip_id,
            extra_tags=["retry:true"],
        ):
            retry_response = traced_gemini_generate(
                client,
                model=cfg.model,
                contents=[
                    types.Content(
                        parts=[
                            types.Part.from_uri(file_uri=file_uri, mime_type="video/mp4"),
                            types.Part.from_text(text=retry_prompt),
                        ]
                    )
                ],
                config=types.GenerateContentConfig(
                    temperature=cfg.temperature,
                    response_mime_type="application/json",
                    response_schema=ClipReview,
                ),
                phase="phase1",
                clip_id=clip_id,
                tracer=tracer,
                num_video_files=1,
                prompt_chars=len(retry_prompt),
            )
        review = ClipReview.model_validate_json(retry_response.text).model_dump()
        if tracer and tracer.traces:
            tracer.traces[-1].validation_retried = True

    # Build lineage inputs
    review_lineage = {}
    import re as _re2
    from .versioning import resolve_transcript_path as _rtp, resolve_user_context_path as _rucp

    _tp = _rtp(clip_paths.root)
    if _tp:
        _tm = _re2.search(r"_v(\d+)\.", _tp.name)
        _prov = "gemini" if "gemini" in _tp.name else "mlx"
        if _tm:
            review_lineage["transcript"] = f"transcript:{_prov}:{clip_id}:v{_tm.group(1)}"
    _ucp = _rucp(editorial_paths.root)
    if _ucp:
        _um = _re2.search(r"_v(\d+)\.", _ucp.name) if "_v" in _ucp.name else None
        if _um:
            review_lineage["user_context"] = f"user_context:user:v{_um.group(1)}"

    meta = begin_version(
        clip_paths.root,
        phase="review",
        provider="gemini",
        clip_id=clip_id,
        inputs=review_lineage,
        config_snapshot={"model": cfg.model, "temperature": cfg.temperature},
        target_dir=clip_paths.review,
    )
    vpath = versioned_path(clip_paths.review / "review_gemini.json", meta.version)
    vpath.write_text(json.dumps(review, indent=2, ensure_ascii=False))
    commit_version(clip_paths.root, meta, output_paths=[vpath], target_dir=clip_paths.review)
    print(f"  {label}: review complete (v{meta.version})")
    return review


def run_phase1_gemini(
    editorial_paths: EditorialProjectPaths,
    manifest: dict,
    cfg: GeminiConfig,
    force: bool = False,
    tracer=None,
    style_supplement: str | None = None,
    only_clip_ids: list[str] | None = None,
    user_context: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Phase 1 via Gemini: review clips in parallel.

    Returns (reviews, failed_clip_ids). Reviews are in original clip order.
    If only_clip_ids is set, only those clips are processed (others loaded from cache).
    """
    clips = manifest["clips"]
    total = len(clips)

    futures = {}
    with ThreadPoolExecutor(max_workers=MAX_LLM_WORKERS) as pool:
        for i, clip_info in enumerate(clips):
            if only_clip_ids and clip_info["clip_id"] not in only_clip_ids:
                continue
            fut = pool.submit(
                _review_single_clip_gemini,
                clip_info,
                editorial_paths,
                cfg,
                force if not only_clip_ids else True,  # force retry for targeted clips
                i + 1,
                total,
                tracer,
                style_supplement,
                user_context,
            )
            futures[fut] = clip_info["clip_id"]

        results_by_id = {}
        failed_ids = []
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                result = fut.result()
                if result:
                    results_by_id[clip_id] = result
                else:
                    failed_ids.append(clip_id)
            except Exception as e:
                print(f"  ERROR reviewing {clip_id}: {e}")
                failed_ids.append(clip_id)

    # When retrying specific clips, load cached results for the rest
    if only_clip_ids:
        for clip_info in clips:
            cid = clip_info["clip_id"]
            if cid in results_by_id or cid in failed_ids:
                continue
            cp = editorial_paths.clip_paths(cid)
            for name in ["review_gemini_latest.json", "review_gemini.json"]:
                cached = cp.review / name
                if cached.exists():
                    try:
                        results_by_id[cid] = json.loads(cached.read_text())
                    except json.JSONDecodeError:
                        print(f"  WARN: corrupt cache {cached.name} for {cid}, skipping")
                    break

    # Return in original clip order
    reviews = [results_by_id[c["clip_id"]] for c in clips if c["clip_id"] in results_by_id]
    return reviews, failed_ids


def run_phase1_claude(
    editorial_paths: EditorialProjectPaths,
    manifest: dict,
    cfg: ClaudeConfig,
    force: bool = False,
    style_supplement: str | None = None,
    only_clip_ids: list[str] | None = None,
    user_context: dict | None = None,
    tracer=None,
) -> tuple[list[dict], list[str]]:
    """Phase 1 via Claude: send each clip's frames, get structured JSON review.

    Returns (reviews, failed_clip_ids). Reviews are in original clip order.
    If only_clip_ids is set, only those clips are processed (others loaded from cache).
    """
    import base64
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. See .env.example")
    client = anthropic.Anthropic(api_key=api_key)

    reviews = []
    failed_ids = []
    for i, clip_info in enumerate(manifest["clips"]):
        clip_id = clip_info["clip_id"]
        clip_paths = editorial_paths.clip_paths(clip_id)

        # Skip clips not in retry set
        if only_clip_ids and clip_id not in only_clip_ids:
            # Load from cache
            for name in ["review_claude_latest.json", "review_claude.json"]:
                cached = clip_paths.review / name
                if cached.exists():
                    try:
                        reviews.append(json.loads(cached.read_text()))
                    except json.JSONDecodeError:
                        print(f"  WARN: corrupt cache {cached.name} for {clip_id}")
                    break
            continue

        # Check cache (skip when retrying specific clips)
        if not only_clip_ids:
            latest_review = clip_paths.review / "review_claude_latest.json"
            legacy_review = clip_paths.review / "review_claude.json"
            if not force and (latest_review.exists() or legacy_review.exists()):
                cached = latest_review if latest_review.exists() else legacy_review
                try:
                    print(f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: review cached")
                    reviews.append(json.loads(cached.read_text()))
                except json.JSONDecodeError:
                    print(
                        f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: corrupt cache, will re-review"
                    )
                continue

        try:
            print(f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: loading frames...")
            frames_manifest_path = clip_paths.frames / "manifest.json"
            if not frames_manifest_path.exists():
                print(f"    WARNING: No frames for {clip_id}, skipping")
                failed_ids.append(clip_id)
                continue
            frames_manifest = json.loads(frames_manifest_path.read_text())

            # Build image content — send all frames (or first batch if too many)
            all_frames = frames_manifest["frames"]
            frames_to_send = all_frames[: cfg.max_images_per_batch]

            content = []
            for frame in frames_to_send:
                img_path = clip_paths.frames / frame["file"]
                img_b64 = base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")
                content.append({"type": "text", "text": f"[{frame['timestamp_fmt']}]"})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    }
                )

            # Load transcript if available
            transcript_text = _load_transcript_for_prompt(clip_paths)

            prompt = build_clip_review_prompt(
                clip_id=clip_id,
                filename=clip_info["filename"],
                duration_sec=clip_info["duration_sec"],
                resolution=clip_info["resolution"],
                orientation=clip_info.get("orientation"),
                aspect_ratio=clip_info.get("aspect_ratio"),
                transcript_text=transcript_text,
                style_supplement=style_supplement,
                user_context=user_context,
            )
            content.append({"type": "text", "text": prompt})

            from .tracing import otel_phase_span, traced_claude_generate

            print(f"    Reviewing with {cfg.model} ({len(frames_to_send)} frames)...")
            with otel_phase_span("phase1", stage="review", provider="claude", clip_id=clip_id):
                response = traced_claude_generate(
                    client,
                    model=cfg.model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=cfg.max_tokens,
                    temperature=cfg.temperature,
                    phase="phase1",
                    clip_id=clip_id,
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )

            review = parse_clip_review(response.content[0].text)

            meta = begin_version(
                clip_paths.root,
                phase="review",
                provider="claude",
                clip_id=clip_id,
                inputs={},  # Claude reviews don't have easy lineage access here
                config_snapshot={"model": cfg.model, "temperature": cfg.temperature},
                target_dir=clip_paths.review,
            )
            vpath = versioned_path(clip_paths.review / "review_claude.json", meta.version)
            vpath.write_text(json.dumps(review, indent=2, ensure_ascii=False))
            commit_version(
                clip_paths.root, meta, output_paths=[vpath], target_dir=clip_paths.review
            )
            reviews.append(review)
        except Exception as e:
            print(f"  ERROR reviewing {clip_id}: {e}")
            failed_ids.append(clip_id)

    return reviews, failed_ids


# ---------------------------------------------------------------------------
# Response quality validation
# ---------------------------------------------------------------------------


def validate_clip_review(review: dict, clip_info: dict) -> tuple[list[str], bool]:
    """Validate a Phase 1 clip review for structural correctness.

    Returns (warnings, is_critical). is_critical means the review should be retried.
    """
    warnings = []
    dur = clip_info.get("duration_sec", 0)
    clip_id = clip_info.get("clip_id", "")

    # Check clip_id match
    review_cid = review.get("clip_id", "")
    if review_cid and clip_id and not clip_id.endswith(review_cid):
        warnings.append(f"clip_id mismatch: expected '{clip_id}', got '{review_cid}'")

    # Check usable segments
    for seg in review.get("usable_segments", []):
        in_s = seg.get("in_sec", 0)
        out_s = seg.get("out_sec", 0)
        if in_s >= out_s:
            warnings.append(f"Segment in_sec ({in_s}) >= out_sec ({out_s})")
        if dur > 0 and out_s > dur + 1.0:
            warnings.append(f"Segment out_sec ({out_s:.1f}) exceeds clip duration ({dur:.1f})")

    # Check for empty review on non-trivial clips
    has_segments = bool(review.get("usable_segments") or review.get("discard_segments"))
    if not has_segments and dur > 5.0:
        warnings.append("No usable or discard segments for a clip > 5s")

    # Critical if: no segments on a real clip, or majority of segments have bad timestamps
    bad_count = sum(
        1
        for seg in review.get("usable_segments", [])
        if seg.get("in_sec", 0) >= seg.get("out_sec", 0)
    )
    total_segs = len(review.get("usable_segments", []))
    is_critical = (not has_segments and dur > 5.0) or (
        total_segs > 0 and bad_count > total_segs / 2
    )

    return warnings, is_critical


def validate_storyboard(storyboard, clip_reviews: list[dict]) -> tuple[list[str], bool]:
    """Validate a Phase 2 storyboard for structural correctness.

    Returns (warnings, is_critical).
    """
    warnings = []
    known_ids = {r.get("clip_id", "") for r in clip_reviews}
    dur_map = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        dur_map[cid] = r.get("duration_sec", 0)

    unknown_count = 0
    for seg in storyboard.segments:
        if seg.clip_id not in known_ids:
            warnings.append(f"Seg {seg.index}: unknown clip_id '{seg.clip_id}'")
            unknown_count += 1
        if seg.in_sec >= seg.out_sec:
            warnings.append(f"Seg {seg.index}: in_sec ({seg.in_sec}) >= out_sec ({seg.out_sec})")
        max_dur = dur_map.get(seg.clip_id, 0)
        if max_dur > 0 and seg.out_sec > max_dur + 1.0:
            warnings.append(
                f"Seg {seg.index}: out_sec ({seg.out_sec:.1f}) > clip duration ({max_dur:.1f})"
            )

    if not storyboard.segments:
        warnings.append("Storyboard has no segments")

    # Check for duplicate indices
    indices = [s.index for s in storyboard.segments]
    if len(indices) != len(set(indices)):
        warnings.append("Duplicate segment indices detected")

    total = len(storyboard.segments)
    is_critical = total == 0 or (total > 0 and unknown_count > total * 0.3)

    return warnings, is_critical


# ---------------------------------------------------------------------------
# Phase 2 — Editorial assembly
# ---------------------------------------------------------------------------


# Phase 2 visual mode now uses concat_proxies() from preprocess.py instead of
# individual uploads. See run_phase2() below.


def _validate_constraints(
    storyboard,
    user_context: dict,
    client,
    model: str,
    tracer=None,
) -> str | None:
    """Run a cheap LLM validation call to check filmmaker constraint satisfaction.

    Returns the validation report text, or None if validation was skipped.
    Prints constraint violations to the console.
    """
    from .tracing import otel_phase_span, traced_gemini_generate

    constraints = []
    if user_context.get("highlights"):
        constraints.append(f"MUST INCLUDE: {user_context['highlights']}")
    if user_context.get("avoid"):
        constraints.append(f"MUST EXCLUDE: {user_context['avoid']}")
    if not constraints:
        return None

    # Build compact storyboard summary for the validator
    seg_lines = []
    for seg in storyboard.segments:
        seg_lines.append(
            f"  [{seg.index}] {seg.clip_id} {seg.in_sec:.1f}-{seg.out_sec:.1f}s "
            f"({seg.purpose}): {seg.description}"
        )
    seg_summary = "\n".join(seg_lines)

    prompt = (
        "You are reviewing a video storyboard against the filmmaker's constraints.\n\n"
        "CONSTRAINTS:\n"
        + "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(constraints))
        + "\n\nSTORYBOARD SEGMENTS:\n"
        + seg_summary
        + "\n\nFor each constraint, answer:\n"
        "- SATISFIED: YES or NO\n"
        "- EVIDENCE: which segment(s) satisfy it, or what is missing\n"
        "- If NO: suggest a specific fix (which clip/segment to add or remove)\n\n"
        "Be concise. One paragraph per constraint."
    )

    try:
        from google.genai import types

        print(f"  [Validate] Checking constraint satisfaction ({model})...")
        with otel_phase_span("phase2_validation", stage="validation", provider="gemini"):
            response = traced_gemini_generate(
                client,
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1),
                phase="phase2_validation",
                tracer=tracer,
                prompt_chars=len(prompt),
            )
        report = response.text.strip()

        # Parse for violations and print them
        has_violation = False
        for line in report.split("\n"):
            line_lower = line.strip().lower()
            if "satisfied: no" in line_lower or "no" in line_lower and "must" in line_lower:
                has_violation = True
        if has_violation:
            print("  [Validate] ⚠ Constraint violation(s) detected:")
            for line in report.split("\n"):
                if line.strip():
                    print(f"    {line.strip()}")
        else:
            print("  [Validate] ✓ All constraints satisfied")

        return report
    except Exception as e:
        print(f"  [Validate] Skipped — validation call failed: {e}")
        return None


def _run_phase2_sections(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Section-based Phase 2: Group → Storyline → Hook → Per-Section → Merge.

    Divide & Conquer pipeline that enforces chronological section order
    while allowing aesthetic freedom within each section.
    """
    from google.genai import types

    from .briefing import format_brief_for_prompt
    from .editorial_prompts import (
        build_hook_prompt,
        build_narrative_planner_prompt,
        build_scene_planner_prompt,
        build_section_storyboard_prompt,
        extract_cast_from_reviews,
    )
    from .models import (
        HookStoryboard,
        ScenePlan,
        SectionPlan,
        SectionStoryboard,
    )
    from .render import render_html_preview, render_markdown
    from .section_grouping import (
        build_section_groups_from_scene_plan,
        format_sections_for_display,
        group_clips_by_date,
        merge_section_storyboards,
    )
    from .tracing import LLMSpinner, otel_phase_span, traced_gemini_generate
    from .versioning import (
        begin_version,
        commit_version,
        current_version,
        update_latest_symlink,
        versioned_dir,
        versioned_path,
    )

    client = _get_gemini_client()
    gemini_cfg = gemini_cfg or GeminiConfig()

    # Format brief WITHOUT constraints for section calls (constraints distributed by storyline)
    section_context_text = (
        format_brief_for_prompt(user_context, phase="phase2", skip_constraints=True)
        if user_context
        else None
    )
    # Full brief WITH constraints for the storyline planner
    full_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    # Extract highlights/avoid for structured constraint distribution
    _highlights = ""
    _avoid = ""
    if user_context:
        if hasattr(user_context, "highlights"):
            _highlights = user_context.highlights or ""
            _avoid = user_context.avoid or ""
        elif isinstance(user_context, dict):
            _highlights = user_context.get("highlights", "")
            _avoid = user_context.get("avoid", "")

    # ── Date grouping (deterministic — the only hard boundary) ─────────────
    print("  [Dates] Grouping clips by date...")
    manifest_file = editorial_paths.master_manifest
    if not manifest_file.exists():
        raise RuntimeError(f"No manifest found: {manifest_file}")
    manifest_data = json.loads(manifest_file.read_text())

    date_groups = group_clips_by_date(manifest_data, clip_reviews)
    total_clips = sum(len(cid) for g in date_groups for s in g.sections for cid in [s.clip_ids])
    print(f"  [Dates] {len(date_groups)} days, {total_clips} clips")

    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # ── Pre-processing ────────────────────────────────────────────────────
    cast = extract_cast_from_reviews(clip_reviews)
    reviews_by_id = {r.get("clip_id", ""): r for r in clip_reviews}
    all_transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)

    # ── Scene Planner LLM call (groups clips into scenes within each date) ──
    print("  [Scene Planner] Identifying scenes within each date...")
    scene_prompt = build_scene_planner_prompt(
        date_groups=date_groups,
        clip_reviews=clip_reviews,
    )

    with LLMSpinner("Scene planning", provider=provider):
        with otel_phase_span(
            "phase2_scene_planner", stage="storyboard", provider="gemini", call="scene"
        ):
            response_scene = traced_gemini_generate(
                client,
                model=gemini_cfg.phase2,
                contents=scene_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=ScenePlan,
                ),
                phase="phase2_scene_planner",
                tracer=tracer,
                prompt_chars=len(scene_prompt),
            )
    scene_plan = ScenePlan.model_validate_json(response_scene.text)
    section_groups = build_section_groups_from_scene_plan(date_groups, scene_plan)

    total_sections = sum(len(g.sections) for g in section_groups)
    print(f"  [Scene Planner] {total_sections} scenes identified")
    print(format_sections_for_display(section_groups, clip_reviews))

    # Save scene plan artifact
    scene_plan_path = editorial_paths.storyboard / "scene_plan_latest.json"
    scene_plan_path.write_text(scene_plan.model_dump_json(indent=2))

    # ── HITL 1: Review scene grouping ─────────────────────────────────────
    if interactive:
        try:
            import questionary

            if not questionary.confirm("Proceed with these scenes?", default=True).ask():
                print(
                    "  Scene grouping not approved."
                    " Edit sections and re-run: vx sections <project> --regroup"
                )
                # Save what we have so user can edit, then exit
                sections_json = [g.model_dump() for g in section_groups]
                sections_path = editorial_paths.storyboard / "sections_latest.json"
                sections_path.write_text(json.dumps(sections_json, indent=2))
                return sections_path
        except (ImportError, EOFError):
            pass  # Non-interactive environment

    # Save confirmed sections
    sections_json = [g.model_dump() for g in section_groups]
    sections_path = editorial_paths.storyboard / "sections_latest.json"
    sections_path.write_text(json.dumps(sections_json, indent=2))

    # ── Narrative Planner LLM call (arc + constraint distribution) ────────
    print(f"  [Narrative] Planning story arc across {total_sections} scenes...")
    narrative_prompt = build_narrative_planner_prompt(
        section_groups=section_groups,
        clip_reviews=clip_reviews,
        user_context_text=full_context_text,
        style=style,
        cast=cast,
        highlights=_highlights,
        avoid=_avoid,
    )

    with LLMSpinner("Narrative planning", provider=provider):
        with otel_phase_span(
            "phase2_narrative_planner", stage="storyboard", provider="gemini", call="narrative"
        ):
            response_narr = traced_gemini_generate(
                client,
                model=gemini_cfg.phase2,
                contents=narrative_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=SectionPlan,
                ),
                phase="phase2_narrative_planner",
                tracer=tracer,
                prompt_chars=len(narrative_prompt),
            )
    section_plan = SectionPlan.model_validate_json(response_narr.text)
    print(f'  [Narrative] "{section_plan.title}" — hook from {section_plan.hook_section_id}')

    # Display narrative plan
    for sn in section_plan.section_narratives:
        goal_str = f" — {sn.section_goal[:60]}" if sn.section_goal else ""
        constraints_str = f" [must: {', '.join(sn.must_include)}]" if sn.must_include else ""
        print(f"    {sn.section_id}: {sn.arc_phase}, {sn.energy}{goal_str}{constraints_str}")

    # ── HITL 2: Review narrative plan ─────────────────────────────────────
    if interactive:
        try:
            import questionary

            if not questionary.confirm("Proceed with this narrative plan?", default=True).ask():
                print("  Narrative plan not approved. Adjust briefing and re-run.")
                return editorial_paths.storyboard / "sections_latest.json"
        except (ImportError, EOFError):
            pass

    # Save narrative plan artifact
    storyline_path = editorial_paths.storyboard / "storyline_latest.json"
    storyline_path.write_text(section_plan.model_dump_json(indent=2))

    # ── Opening Hook LLM call ─────────────────────────────────────────────
    print("  [Hook] Creating opening hook from best clips...")
    high_value_clips = [
        r
        for r in clip_reviews
        if any(km.get("editorial_value") == "high" for km in r.get("key_moments", []))
    ]
    if not high_value_clips:
        high_value_clips = clip_reviews[:5]  # fallback: first 5 clips

    hook_prompt = build_hook_prompt(
        high_value_clips=high_value_clips,
        section_plan=section_plan,
        cast=cast,
        style=style,
    )

    with LLMSpinner("Opening hook", provider=provider):
        with otel_phase_span("phase2_hook", stage="storyboard", provider="gemini", call="hook"):
            response_hook = traced_gemini_generate(
                client,
                model=gemini_cfg.phase2b,
                contents=hook_prompt,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2b_temperature,
                    response_mime_type="application/json",
                    response_schema=HookStoryboard,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    max_output_tokens=65536,
                ),
                phase="phase2_hook",
                tracer=tracer,
                prompt_chars=len(hook_prompt),
            )
    hook_sb = HookStoryboard.model_validate_json(response_hook.text)
    hook_dur = sum(s.duration_sec for s in hook_sb.segments)
    print(f"  [Hook] {len(hook_sb.segments)} segments, {hook_dur:.1f}s")

    # ── Per-section storyboarding (sequential) ────────────────────────────
    narrative_by_id = {sn.section_id: sn for sn in section_plan.section_narratives}
    cumulative_narratives: list[tuple[str, str]] = []
    section_storyboards: list[SectionStoryboard] = []

    flat_sections = [(group, section) for group in section_groups for section in group.sections]

    for idx, (group, section) in enumerate(flat_sections, 1):
        narrative = narrative_by_id.get(section.section_id)
        section_reviews = [reviews_by_id[cid] for cid in section.clip_ids if cid in reviews_by_id]

        # Prepare section transcripts
        section_transcripts = None
        if all_transcripts:
            section_transcripts = {
                cid: all_transcripts[cid] for cid in section.clip_ids if cid in all_transcripts
            }

        prompt = build_section_storyboard_prompt(
            section=section,
            section_clip_reviews=section_reviews,
            section_narrative=narrative,
            transcripts=section_transcripts,
            cumulative_narratives=cumulative_narratives if cumulative_narratives else None,
            cast=cast,
            style=style,
            user_context_text=section_context_text,  # constraints-free, goals come from narrative
            style_supplement=style_supplement,
        )

        label = section.label or section.section_id
        print(f"  [Section {idx}/{len(flat_sections)}] {label}...")

        with LLMSpinner(f"Section: {label}", provider=provider):
            with otel_phase_span(
                f"phase2_section_{section.section_id}",
                stage="storyboard",
                provider="gemini",
                call=f"section_{idx}",
            ):
                response_sec = traced_gemini_generate(
                    client,
                    model=gemini_cfg.phase2b,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2b_temperature,
                        response_mime_type="application/json",
                        response_schema=SectionStoryboard,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase=f"phase2_section_{section.section_id}",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )

        ssb = SectionStoryboard.model_validate_json(response_sec.text)
        section_storyboards.append(ssb)
        cumulative_narratives.append((section.section_id, ssb.narrative_summary))

        seg_dur = sum(s.duration_sec for s in ssb.segments)
        print(f"    ✓ {len(ssb.segments)} segments, {seg_dur:.1f}s")

    # ── Programmatic merge ────────────────────────────────────────────────
    print("  [Merge] Combining sections into final storyboard...")
    storyboard = merge_section_storyboards(
        hook=hook_sb,
        section_storyboards=section_storyboards,
        section_plan=section_plan,
        section_groups=section_groups,
    )

    total_dur = sum(s.duration_sec for s in storyboard.segments)
    print(f"  [Merge] {len(storyboard.segments)} segments, {total_dur:.1f}s total")

    # ── Resolve clip IDs and validate ─────────────────────────────────────
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    _resolve_clip_id_refs(storyboard, known_clip_ids)

    # Auto-clamp timestamps
    fix_log = []
    for seg in storyboard.segments:
        review = reviews_by_id.get(seg.clip_id)
        if not review:
            continue
        usable = review.get("usable_segments", [])
        best = None
        best_overlap = -1
        for us in usable:
            us_in = us.get("in_sec", 0)
            us_out = us.get("out_sec", 0)
            overlap = min(seg.out_sec, us_out) - max(seg.in_sec, us_in)
            if overlap > best_overlap:
                best_overlap = overlap
                best = us
        if best:
            if seg.in_sec < best.get("in_sec", 0):
                fix_log.append(
                    f"Seg {seg.index}: clamped in_sec {seg.in_sec:.1f} → {best['in_sec']:.1f}"
                )
                seg.in_sec = best["in_sec"]
            if seg.out_sec > best.get("out_sec", 0):
                fix_log.append(
                    f"Seg {seg.index}: clamped out_sec {seg.out_sec:.1f} → {best['out_sec']:.1f}"
                )
                seg.out_sec = best["out_sec"]

    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if fix_log:
        print(f"  [Fix] Auto-clamped {len(fix_log)} timestamps:")
        for f in fix_log[:5]:
            print(f"    {f}")
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")

    # ── Editorial Director review ─────────────────────────────────────────
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review
        from .review_display import print_turn

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            turn_callback=print_turn,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # ── Version and save outputs ──────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    from .versioning import resolve_user_context_path as _rucp_sec

    _uc_sec = _rucp_sec(editorial_paths.root)
    if _uc_sec and "_v" in _uc_sec.name:
        import re as _re_sec

        _um_sec = _re_sec.search(r"_v(\d+)\.", _uc_sec.name)
        if _um_sec:
            review_inputs["user_context"] = f"user_context:user:v{_um_sec.group(1)}"
    cfg_snap = {
        "model_scene": gemini_cfg.phase2,
        "model_narrative": gemini_cfg.phase2,
        "model_hook": gemini_cfg.phase2b,
        "model_section": gemini_cfg.phase2b,
        "pipeline": "sections",
        "temperature": gemini_cfg.phase2_temperature,
    }

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # Rendered: HTML preview
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    # Save fix log
    if fix_log:
        fix_path = editorial_paths.storyboard / f"fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs (section pipeline):")
    print(f"    Sections:  {sections_path}")
    print(f"    Storyline: {storyline_path}")
    print(f"    JSON:      {json_path}")
    print(f"    MD:        {md_path}")
    print(f"    Preview:   {preview_path}")
    return json_path


def _run_phase2_split(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Multi-call Phase 2: Reasoning → Structuring → Assembly → Validation.

    Call 2A:   Freeform editorial reasoning (no schema, high temp)
    Call 2A.5: Faithful structuring into StoryPlan JSON (cheap model)
    Call 2B:   Precise timestamp assembly within bounded windows (low temp)
    """
    from .models import EditorialStoryboard, StoryPlan
    from .render import render_markdown, render_html_preview
    from .briefing import format_brief_for_prompt
    from .editorial_prompts import (
        extract_cast_from_reviews,
        condense_clip_for_planning,
        trim_transcript_to_usable,
        build_phase2a_reasoning_prompt,
        build_phase2a_structuring_prompt,
        build_phase2b_assembly_prompt,
    )
    from .tracing import otel_phase_span, traced_gemini_generate, LLMSpinner

    total_duration = sum(
        sum(seg.get("duration_sec", 0) for seg in r.get("usable_segments", []))
        for r in clip_reviews
    )
    if total_duration == 0:
        total_duration = sum(r.get("duration_sec", 0) for r in clip_reviews if "duration_sec" in r)

    # Sort clip reviews chronologically
    filming_timeline = None
    manifest_file = editorial_paths.master_manifest
    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text())
        creation_times = {
            c["clip_id"]: c.get("creation_time") for c in manifest_data.get("clips", [])
        }
        clip_reviews.sort(
            key=lambda r: (creation_times.get(r.get("clip_id"), "") or "", r.get("clip_id", ""))
        )
        timeline_lines = []
        for i, r in enumerate(clip_reviews, 1):
            cid = r.get("clip_id", "unknown")
            ct = creation_times.get(cid)
            timeline_lines.append(f"  {i}. {cid} — filmed {ct}" if ct else f"  {i}. {cid}")
        filming_timeline = "\n".join(timeline_lines)

    # ── Pre-processing (deterministic) ──────────────────────────────────────
    print("  [2A] Pre-processing: deduplicating cast, condensing clips...")
    cast = extract_cast_from_reviews(clip_reviews)

    # Enable editorial hints when creative brief has explicit narrative beats
    _has_key_beats = False
    if user_context:
        from .models import CreativeBrief

        if isinstance(user_context, CreativeBrief):
            _has_key_beats = bool(user_context.narrative and user_context.narrative.key_beats)
        elif isinstance(user_context, dict):
            _narrative = user_context.get("narrative")
            if isinstance(_narrative, dict):
                _has_key_beats = bool(_narrative.get("key_beats"))
    condensed = [
        condense_clip_for_planning(r, include_editorial_hints=_has_key_beats) for r in clip_reviews
    ]

    all_transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)
    trimmed_transcripts = None
    if all_transcripts:
        trimmed_transcripts = {}
        for r in clip_reviews:
            cid = r.get("clip_id", "")
            if cid in all_transcripts:
                trimmed_transcripts[cid] = trim_transcript_to_usable(
                    all_transcripts[cid],
                    r.get("usable_segments", []),
                )

    user_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    # ── Call 2A: Freeform editorial reasoning ───────────────────────────────
    reasoning_prompt = build_phase2a_reasoning_prompt(
        clip_reviews=clip_reviews,
        style=style,
        total_duration_sec=total_duration,
        cast=cast,
        condensed_clips=condensed,
        transcripts=trimmed_transcripts,
        filming_timeline=filming_timeline,
        user_context_text=user_context_text,
        user_context=user_context,
        style_supplement=style_supplement,
    )

    clip_ids = [c["clip_id"] for c in condensed]

    if provider == "gemini":
        from google.genai import types

        client = _get_gemini_client()

        # Visual mode: attach proxy videos to Call 2A
        # Use all proxy clips from disk (same set as briefing/quick scan) so that
        # concat bundle composition matches and Gemini File API URIs get cache hits.
        video_parts = []
        if visual:
            from .preprocess import concat_proxies

            all_clip_ids = sorted(
                d.name
                for d in editorial_paths.clips_dir.iterdir()
                if d.is_dir() and (d / "proxy").exists()
            )
            bundles = concat_proxies(editorial_paths, all_clip_ids)
            if bundles:
                file_cache = load_file_api_cache(editorial_paths)
                cached_count = 0
                for i, bundle in enumerate(bundles):
                    cache_key = f"_concat_bundle_{i}"
                    cached_uri = get_cached_uri(file_cache, cache_key)
                    if cached_uri:
                        video_parts.append(
                            types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4")
                        )
                        cached_count += 1
                        continue
                    print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
                    video_file = client.files.upload(file=str(bundle["path"]))
                    video_file = _wait_for_gemini_file(video_file, client)
                    if video_file.state.name == "FAILED":
                        print(f"  WARNING: bundle {i + 1} upload failed")
                        continue
                    cache_file_uri(editorial_paths, cache_key, video_file.uri)
                    video_parts.append(
                        types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                    )
                if cached_count:
                    print(
                        f"  Concat cached: {cached_count}/{len(bundles)} bundle(s)"
                        " reused from Gemini"
                    )

        mode_label = "visual" if video_parts else "text-only"
        print(f"  [2A] Generating editorial plan ({provider}, {mode_label}, freeform)...")

        if video_parts:
            contents_2a = [
                types.Content(parts=[*video_parts, types.Part.from_text(text=reasoning_prompt)])
            ]
        else:
            contents_2a = reasoning_prompt

        with LLMSpinner("Editorial reasoning (Call 2A)", provider=provider):
            with otel_phase_span(
                "phase2a_reasoning", stage="storyboard", provider="gemini", call="2A"
            ):
                response_2a = traced_gemini_generate(
                    client,
                    model=gemini_cfg.phase2,
                    contents=contents_2a,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2_temperature,
                        # No response_schema — freeform text output
                    ),
                    phase="phase2a_reasoning",
                    tracer=tracer,
                    prompt_chars=len(reasoning_prompt),
                    num_video_files=len(video_parts),
                )
        editorial_plan_text = response_2a.text
        print(f"  [2A] Editorial plan: {len(editorial_plan_text)} chars")

        # ── Call 2A.5: Structuring (cheap model) ───────────────────────────
        print(f"  [2A.5] Structuring plan into StoryPlan JSON ({gemini_cfg.structuring_model})...")
        structuring_prompt = build_phase2a_structuring_prompt(
            editorial_plan_text=editorial_plan_text,
            clip_ids=clip_ids,
        )

        with LLMSpinner("Plan structuring (Call 2A.5)", provider=provider):
            with otel_phase_span(
                "phase2a_structuring", stage="storyboard", provider="gemini", call="2A.5"
            ):
                response_2a5 = traced_gemini_generate(
                    client,
                    model=gemini_cfg.structuring_model,
                    contents=structuring_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=StoryPlan,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase="phase2a_structuring",
                    tracer=tracer,
                    prompt_chars=len(structuring_prompt),
                )
        story_plan = StoryPlan.model_validate_json(response_2a5.text)

        # ── Checkpoint: validate plan ──────────────────────────────────────
        plan_issues = []
        known_ids = {r["clip_id"] for r in clip_reviews}
        for ps in story_plan.planned_segments:
            if ps.clip_id not in known_ids:
                plan_issues.append(f"Unknown clip_id in plan: {ps.clip_id}")
            else:
                review = next((r for r in clip_reviews if r["clip_id"] == ps.clip_id), None)
                if review:
                    n_segs = len(review.get("usable_segments", []))
                    if ps.usable_segment_index >= n_segs:
                        plan_issues.append(
                            f"Segment index {ps.usable_segment_index} out of range "
                            f"for {ps.clip_id} (has {n_segs} usable segments)"
                        )
        if plan_issues:
            for issue in plan_issues:
                print(f"  PLAN WARN: {issue}")

        # ── Call 2B: Precise assembly ──────────────────────────────────────
        selected_ids = {ps.clip_id for ps in story_plan.planned_segments}
        selected_reviews = [r for r in clip_reviews if r["clip_id"] in selected_ids]
        selected_transcripts = (
            {k: v for k, v in all_transcripts.items() if k in selected_ids}
            if all_transcripts
            else None
        )

        assembly_prompt = build_phase2b_assembly_prompt(
            story_plan=story_plan,
            clip_reviews=selected_reviews,
            transcripts=selected_transcripts,
            style=style,
        )

        print(
            f"  [2B] Assembling storyboard ({len(selected_reviews)} clips, "
            f"{len(story_plan.planned_segments)} segments)..."
        )
        with LLMSpinner("Precise assembly (Call 2B)", provider=provider):
            with otel_phase_span(
                "phase2b_assembly", stage="storyboard", provider="gemini", call="2B"
            ):
                response_2b = traced_gemini_generate(
                    client,
                    model=gemini_cfg.phase2b,
                    contents=assembly_prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.phase2b_temperature,
                        response_mime_type="application/json",
                        response_schema=EditorialStoryboard,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=65536,
                    ),
                    phase="phase2b_assembly",
                    tracer=tracer,
                    prompt_chars=len(assembly_prompt),
                )
        storyboard = EditorialStoryboard.model_validate_json(response_2b.text)

    else:
        raise ValueError(
            f"Split pipeline not yet implemented for provider: {provider}. "
            "Use provider='gemini' or set use_split_pipeline=False."
        )

    # ── Validate & fix ─────────────────────────────────────────────────────
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    _resolve_clip_id_refs(storyboard, known_clip_ids)

    # Enhanced validation: clamp timestamps to usable segment bounds
    reviews_by_id = {r["clip_id"]: r for r in clip_reviews}
    fix_log = []
    for seg in storyboard.segments:
        review = reviews_by_id.get(seg.clip_id)
        if not review:
            continue
        usable = review.get("usable_segments", [])
        # Find matching usable segment by best overlap
        best = None
        best_overlap = -1
        for us in usable:
            us_in = us.get("in_sec", 0)
            us_out = us.get("out_sec", 0)
            overlap = min(seg.out_sec, us_out) - max(seg.in_sec, us_in)
            if overlap > best_overlap:
                best_overlap = overlap
                best = us
        if best:
            if seg.in_sec < best.get("in_sec", 0):
                fix_log.append(
                    f"Seg {seg.index}: clamped in_sec {seg.in_sec:.1f} → {best['in_sec']:.1f}"
                )
                seg.in_sec = best["in_sec"]
            if seg.out_sec > best.get("out_sec", 0):
                fix_log.append(
                    f"Seg {seg.index}: clamped out_sec {seg.out_sec:.1f} → {best['out_sec']:.1f}"
                )
                seg.out_sec = best["out_sec"]

    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if fix_log:
        print(f"  [Fix] Auto-clamped {len(fix_log)} timestamps:")
        for f in fix_log[:5]:
            print(f"    {f}")
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")
    if tracer and tracer.traces:
        tracer.traces[-1].validation_warnings = val_warnings + fix_log

    # ── Constraint validation call (cheap LLM check) ──────────────────────
    constraint_report = None
    if user_context and (user_context.get("highlights") or user_context.get("avoid")):
        constraint_report = _validate_constraints(
            storyboard=storyboard,
            user_context=user_context,
            client=client,
            model=gemini_cfg.structuring_model,
            tracer=tracer,
        )

    # ── Editorial Director review (enabled by default) ────────────────────
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # ── Version and save outputs ───────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    from .versioning import resolve_user_context_path as _rucp2

    _uc2 = _rucp2(editorial_paths.root)
    if _uc2 and "_v" in _uc2.name:
        import re as _re3

        _um2 = _re3.search(r"_v(\d+)\.", _uc2.name)
        if _um2:
            review_inputs["user_context"] = f"user_context:user:v{_um2.group(1)}"
    cfg_snap = {
        "model_2a": gemini_cfg.phase2,
        "model_2a5": gemini_cfg.structuring_model,
        "model_2b": gemini_cfg.phase2b,
        "temperature": gemini_cfg.phase2_temperature,
    }

    from .versioning import current_version

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # Save Call 2A freeform plan (human-readable debug artifact)
    plan_txt_path = editorial_paths.storyboard / f"editorial_plan_{provider}_v{v}.txt"
    plan_txt_path.write_text(editorial_plan_text)

    # Save StoryPlan intermediate
    plan_json_path = editorial_paths.storyboard / f"storyplan_{provider}_v{v}.json"
    plan_json_path.write_text(story_plan.model_dump_json(indent=2))

    # Save fix log if any
    if fix_log:
        fix_path = editorial_paths.storyboard / f"fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    # Save constraint validation report
    if constraint_report:
        report_path = editorial_paths.storyboard / f"constraint_check_{provider}_v{v}.txt"
        report_path.write_text(constraint_report)

    # Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # Rendered: HTML preview
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path, plan_txt_path, plan_json_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs (split pipeline):")
    print(f"    Plan:      {plan_txt_path}")
    print(f"    StoryPlan: {plan_json_path}")
    print(f"    JSON:      {json_path}")
    print(f"    MD:        {md_path}")
    print(f"    Preview:   {preview_path}")
    return json_path


def run_phase2(
    clip_reviews: list[dict],
    editorial_paths: EditorialProjectPaths,
    project_name: str,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style: str = "vlog",
    user_context: dict | None = None,
    tracer=None,
    visual: bool = False,
    style_supplement: str | None = None,
    review_config: "ReviewConfig | None" = None,
    interactive: bool = False,
) -> Path:
    """Phase 2: produce structured EditorialStoryboard + render markdown and HTML preview.

    If gemini_cfg.use_split_pipeline is True, delegates to the multi-call pipeline
    (Call 2A reasoning → Call 2A.5 structuring → Call 2B assembly).
    """
    # Check for Timeline Mode (scene-by-scene chronological assembly)
    _timeline = gemini_cfg and gemini_cfg.use_timeline_mode and provider == "gemini"
    if not _timeline and user_context is not None:
        # Auto-detect from Creative Brief narrative structure
        _brief = user_context
        if hasattr(_brief, "narrative") and _brief.narrative:
            if getattr(_brief.narrative, "structure", "") == "chronological":
                _timeline = True
    if _timeline:
        return _run_phase2_sections(
            clip_reviews=clip_reviews,
            editorial_paths=editorial_paths,
            project_name=project_name,
            provider=provider,
            gemini_cfg=gemini_cfg,
            claude_cfg=claude_cfg,
            style=style,
            user_context=user_context,
            tracer=tracer,
            visual=visual,
            style_supplement=style_supplement,
            review_config=review_config,
            interactive=interactive,
        )

    # Check for split pipeline mode
    if gemini_cfg and gemini_cfg.use_split_pipeline and provider == "gemini":
        return _run_phase2_split(
            clip_reviews=clip_reviews,
            editorial_paths=editorial_paths,
            project_name=project_name,
            provider=provider,
            gemini_cfg=gemini_cfg,
            claude_cfg=claude_cfg,
            style=style,
            user_context=user_context,
            tracer=tracer,
            visual=visual,
            style_supplement=style_supplement,
            review_config=review_config,
            interactive=interactive,
        )

    from .models import EditorialStoryboard
    from .render import render_markdown, render_html_preview
    from .briefing import format_brief_for_prompt

    total_duration = sum(
        sum(seg.get("duration_sec", 0) for seg in r.get("usable_segments", []))
        for r in clip_reviews
    )
    if total_duration == 0:
        total_duration = sum(r.get("duration_sec", 0) for r in clip_reviews if "duration_sec" in r)

    # Sort clip reviews in chronological filming order
    filming_timeline = None
    manifest_file = editorial_paths.master_manifest
    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text())
        creation_times = {
            c["clip_id"]: c.get("creation_time") for c in manifest_data.get("clips", [])
        }
        clip_reviews.sort(
            key=lambda r: (creation_times.get(r.get("clip_id"), "") or "", r.get("clip_id", ""))
        )
        timeline_lines = []
        for i, r in enumerate(clip_reviews, 1):
            cid = r.get("clip_id", "unknown")
            ct = creation_times.get(cid)
            timeline_lines.append(f"  {i}. {cid} — filmed {ct}" if ct else f"  {i}. {cid}")
        filming_timeline = "\n".join(timeline_lines)

    # Load transcripts for all clips
    transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)

    # Resolve visual mode: concatenate proxies into bundles for Gemini.
    # Uses concat_proxies() to avoid the 10-video-per-prompt limit.
    # Use all proxy clips from disk (same set as briefing/quick scan) so that
    # concat bundle composition matches and Gemini File API URIs get cache hits.
    visual_timeline = None
    video_parts = []
    if visual and provider == "gemini":
        from google.genai import types
        from .preprocess import concat_proxies

        all_clip_ids = sorted(
            d.name
            for d in editorial_paths.clips_dir.iterdir()
            if d.is_dir() and (d / "proxy").exists()
        )
        bundles = concat_proxies(editorial_paths, all_clip_ids)

        if bundles:
            client = _get_gemini_client()
            file_cache = load_file_api_cache(editorial_paths)
            cached_count = 0

            for i, bundle in enumerate(bundles):
                cache_key = f"_concat_bundle_{i}"
                cached_uri = get_cached_uri(file_cache, cache_key)
                if cached_uri:
                    video_parts.append(
                        types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4")
                    )
                    cached_count += 1
                    continue

                print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
                video_file = client.files.upload(file=str(bundle["path"]))
                video_file = _wait_for_gemini_file(video_file, client)
                if video_file.state.name == "FAILED":
                    print(f"  WARNING: bundle {i + 1} upload failed")
                    continue
                cache_file_uri(editorial_paths, cache_key, video_file.uri)
                video_parts.append(
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                )

            if cached_count:
                print(
                    f"  Concat cached: {cached_count}/{len(bundles)} bundle(s) reused from Gemini"
                )
            visual_timeline = bundles

    user_context_text = (
        format_brief_for_prompt(user_context, phase="phase2") if user_context else None
    )

    prompt = build_editorial_assembly_prompt(
        project_name=project_name,
        clip_reviews=clip_reviews,
        style=style,
        clip_count=len(clip_reviews),
        total_duration_sec=total_duration,
        transcripts=transcripts,
        visual_timeline=visual_timeline,
        style_supplement=style_supplement,
        filming_timeline=filming_timeline,
        user_context_text=user_context_text,
    )

    p2_model = gemini_cfg.phase2 if gemini_cfg else None
    mode_label = "visual" if visual else "text-only"
    print(f"  Generating editorial storyboard ({provider}, {mode_label})...")

    if provider == "gemini":
        if not visual_timeline:
            from google.genai import types

            client = _get_gemini_client()

        from .tracing import otel_phase_span, traced_gemini_generate

        # Build contents: text-only or multipart with concat video bundles
        num_videos = len(video_parts)
        if visual_timeline and video_parts:
            contents = [types.Content(parts=[*video_parts, types.Part.from_text(text=prompt)])]
        else:
            contents = prompt

        with otel_phase_span("phase2", stage="storyboard", provider="gemini"):
            response = traced_gemini_generate(
                client,
                model=p2_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=gemini_cfg.phase2_temperature,
                    response_mime_type="application/json",
                    response_schema=EditorialStoryboard,
                    max_output_tokens=65536,
                ),
                phase="phase2",
                tracer=tracer,
                prompt_chars=len(prompt),
                num_video_files=num_videos,
            )
        storyboard = EditorialStoryboard.model_validate_json(response.text)

    elif provider == "claude":
        import anthropic

        from .tracing import otel_phase_span, traced_claude_generate

        client = anthropic.Anthropic(api_key=_require_api_key("ANTHROPIC_API_KEY"))
        with otel_phase_span("phase2", stage="storyboard", provider="claude"):
            response = traced_claude_generate(
                client,
                model=claude_cfg.model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                        + "\n\nRespond ONLY with valid JSON matching the EditorialStoryboard schema.",
                    }
                ],
                max_tokens=claude_cfg.max_tokens * 2,
                temperature=claude_cfg.phase2_temperature,
                phase="phase2",
                tracer=tracer,
                prompt_chars=len(prompt),
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        storyboard = EditorialStoryboard.model_validate_json(text)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Resolve abbreviated clip IDs (e.g., LLM returns "C0073" but clip_id is "20260330114125_C0073")
    known_clip_ids = {r["clip_id"] for r in clip_reviews}
    _resolve_clip_id_refs(storyboard, known_clip_ids)

    # Validate storyboard quality
    val_warnings, val_critical = validate_storyboard(storyboard, clip_reviews)
    if val_warnings:
        for w in val_warnings[:5]:
            print(f"  WARN: {w}")
        if len(val_warnings) > 5:
            print(f"  ... and {len(val_warnings) - 5} more warnings")
    if tracer and tracer.traces:
        tracer.traces[-1].validation_warnings = val_warnings
    if val_critical:
        print("  Critical storyboard issues detected — consider re-running with --force")

    # Constraint validation (cheap LLM check — works for both single and split pipelines)
    if provider == "gemini" and user_context and gemini_cfg:
        if user_context.get("highlights") or user_context.get("avoid"):
            if not visual_timeline:
                from google.genai import types  # noqa: F811

                client = _get_gemini_client()
            _validate_constraints(
                storyboard=storyboard,
                user_context=user_context,
                client=client,
                model=gemini_cfg.structuring_model,
                tracer=tracer,
            )

    # Editorial Director review (enabled by default)
    if review_config and review_config.enabled:
        from .editorial_director import run_editorial_review

        print(
            f"  [Director] Starting editorial review ({review_config.model}, "
            f"up to {review_config.max_turns} turns, "
            f"{review_config.wall_clock_timeout_sec:.0f}s timeout)..."
        )
        storyboard, _review_log = run_editorial_review(
            storyboard=storyboard,
            clip_reviews=clip_reviews,
            user_context=user_context,
            clips_dir=editorial_paths.clips_dir,
            review_config=review_config,
            tracer=tracer,
            interactive=interactive,
            style_guidelines=style_supplement,
        )
        print(
            f"  [Director] Review complete: {_review_log.convergence_reason} "
            f"({_review_log.total_turns} turns, {_review_log.total_fixes} fixes, "
            f"${_review_log.total_cost_usd:.3f}, {_review_log.total_duration_sec:.1f}s)"
        )

    # Version and save outputs
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # Build lineage: record which review versions and user context were used
    review_inputs = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        review_inputs[f"review:{cid}"] = cid
    # Add user_context lineage
    from .versioning import resolve_user_context_path as _rucp2

    _uc2 = _rucp2(editorial_paths.root)
    if _uc2 and "_v" in _uc2.name:
        import re as _re3

        _um2 = _re3.search(r"_v(\d+)\.", _uc2.name)
        if _um2:
            review_inputs["user_context"] = f"user_context:user:v{_um2.group(1)}"
    cfg_snap = {}
    if gemini_cfg:
        cfg_snap = {"model": gemini_cfg.phase2, "temperature": gemini_cfg.temperature}
    elif claude_cfg:
        cfg_snap = {"model": claude_cfg.model, "temperature": claude_cfg.temperature}

    # Determine parent_id from review version for lineage-prefixed versioning
    from .versioning import current_version

    rv_version = current_version(editorial_paths.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(editorial_paths.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="storyboard",
        provider=provider,
        inputs=review_inputs,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    # 1. Primary: structured JSON
    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(storyboard.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    # 2. Rendered: markdown
    md_path = versioned_path(editorial_paths.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(storyboard))
    update_latest_symlink(md_path)

    # 3. Rendered: HTML preview (in versioned exports dir)
    export_dir = versioned_dir(editorial_paths.exports, v)
    html = render_html_preview(
        storyboard,
        clips_dir=editorial_paths.clips_dir,
        output_dir=export_dir,
    )
    preview_path = export_dir / "preview.html"
    preview_path.write_text(html)
    update_latest_symlink(export_dir)

    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, md_path, preview_path],
        target_dir=editorial_paths.storyboard,
    )

    print(f"  v{v} outputs:")
    print(f"    JSON:    {json_path}")
    print(f"    MD:      {md_path}")
    print(f"    Preview: {preview_path}")
    return json_path


# ---------------------------------------------------------------------------
# Phase 3 — Visual Monologue (text overlay generation)
# ---------------------------------------------------------------------------


def _run_monologue_split(
    editorial_paths: EditorialProjectPaths,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    style_preset=None,
    user_context: dict | None = None,
    tracer=None,
    persona_hint: str | None = None,
    storyboard_path: Path | None = None,
) -> Path:
    """Multi-call Phase 3: Analysis → Creative → Validation.

    Call 1: Segment eligibility analysis + arc planning (which segments get overlays)
    Call 2: Creative text generation within bounded windows
    Call 3: Deterministic validation + assembly into MonologuePlan
    """
    from .models import (
        EditorialStoryboard,
        MonologuePlan,
        MonologueOverlay,
        OverlayPlan,
        OverlayDrafts,
    )
    from .editorial_prompts import (
        build_monologue_call1_prompt,
        build_monologue_call2_prompt,
        validate_monologue_overlays,
    )
    from .briefing import format_brief_for_prompt
    from .tracing import otel_phase_span, traced_gemini_generate, LLMSpinner
    from google.genai import types

    if not style_preset or not style_preset.has_phase3:
        raise ValueError("Phase 3 requires a style preset with has_phase3=True")

    # Load storyboard
    if storyboard_path:
        storyboard_json = storyboard_path
    else:
        storyboard_json = _find_latest_storyboard(editorial_paths, provider)
    if not storyboard_json:
        raise FileNotFoundError(
            f"No storyboard found in {editorial_paths.storyboard}. Run Phase 2 first."
        )
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json.read_text())

    transcripts = _load_all_transcripts_for_monologue(editorial_paths, storyboard)

    user_context_text = None
    if user_context:
        user_context_text = format_brief_for_prompt(user_context, phase="phase2")
    else:
        from .versioning import resolve_user_context_path

        context_path = resolve_user_context_path(editorial_paths.root)
        if context_path:
            ctx = json.loads(context_path.read_text())
            user_context_text = format_brief_for_prompt(ctx, phase="phase2")

    client = _get_gemini_client()

    # ── Call 1: Segment analysis & arc planning ────────────────────────────
    call1_prompt = build_monologue_call1_prompt(
        storyboard=storyboard,
        transcripts=transcripts,
        user_context_text=user_context_text,
    )

    seg_count = len(storyboard.segments)
    print(f"  [M1] Analyzing {seg_count} segments for overlay eligibility...")

    with LLMSpinner("Segment analysis (Call M1)", provider=provider):
        with otel_phase_span("phase3_analysis", stage="monologue", provider="gemini", call="M1"):
            response_1 = traced_gemini_generate(
                client,
                model=gemini_cfg.model,
                contents=call1_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=OverlayPlan,
                ),
                phase="phase3_analysis",
                tracer=tracer,
                prompt_chars=len(call1_prompt),
            )
    overlay_plan = OverlayPlan.model_validate_json(response_1.text)
    eligible_count = len(overlay_plan.eligible_segments)
    print(
        f"  [M1] {eligible_count}/{seg_count} segments eligible for overlays "
        f"(persona: {overlay_plan.persona_recommendation})"
    )

    if eligible_count == 0:
        raise ValueError("No segments eligible for overlays — all segments contain speech?")

    # ── Call 2: Creative text generation ───────────────────────────────────
    call2_prompt = build_monologue_call2_prompt(
        overlay_plan=overlay_plan,
        storyboard=storyboard,
        persona_hint=persona_hint,
    )

    print(
        f"  [M2] Generating overlay text ({persona_hint or overlay_plan.persona_recommendation})..."
    )

    with LLMSpinner("Creative text (Call M2)", provider=provider):
        with otel_phase_span("phase3_creative", stage="monologue", provider="gemini", call="M2"):
            response_2 = traced_gemini_generate(
                client,
                model=gemini_cfg.model,
                contents=call2_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.8,  # creative writing needs variety
                    response_mime_type="application/json",
                    response_schema=OverlayDrafts,
                ),
                phase="phase3_creative",
                tracer=tracer,
                prompt_chars=len(call2_prompt),
            )
    drafts = OverlayDrafts.model_validate_json(response_2.text)
    print(f"  [M2] Generated {len(drafts.overlays)} overlay drafts")

    # ── Call 3: Deterministic validation (no LLM needed) ──────────────────
    fixed_overlays, fix_log = validate_monologue_overlays(
        overlays=drafts.overlays,
        eligible_segments=overlay_plan.eligible_segments,
        storyboard_segments=storyboard.segments,
    )

    if fix_log:
        print(f"  [M3] Validation fixes ({len(fix_log)}):")
        for f in fix_log[:5]:
            print(f"    {f}")
        if len(fix_log) > 5:
            print(f"    ... and {len(fix_log) - 5} more")

    # Assemble into MonologuePlan
    persona = persona_hint or overlay_plan.persona_recommendation
    monologue_overlays = []
    for i, ov in enumerate(fixed_overlays):
        monologue_overlays.append(
            MonologueOverlay(
                index=i,
                segment_index=ov.segment_index,
                text=ov.text,
                appear_at=ov.appear_at,
                duration_sec=ov.duration_sec,
                note=f"arc: {ov.arc_phase}" if ov.arc_phase else "",
            )
        )

    monologue = MonologuePlan(
        persona=persona,
        persona_description=overlay_plan.persona_rationale,
        tone_mechanics=["lowercase_whisper", "ellipses", "micro_pacing"],
        arc_structure=sorted({es.arc_phase for es in overlay_plan.eligible_segments}),
        overlays=monologue_overlays,
        total_text_time_sec=sum(ov.duration_sec for ov in monologue_overlays),
        pacing_notes=[],
        music_sync_notes=[],
    )

    # ── Version and save ──────────────────────────────────────────────────
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    storyboard_input = {}
    storyboard_latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if storyboard_latest.exists():
        import re as _re

        resolved = storyboard_latest.resolve()
        vm = _re.search(r"_v(\d+)\.json$", resolved.name)
        if vm:
            storyboard_input["storyboard"] = f"storyboard:{provider}:v{vm.group(1)}"

    cfg_snap = {"model": gemini_cfg.model, "temperature": gemini_cfg.temperature}
    sb_parent_id = f"sb.{vm.group(1)}" if storyboard_latest.exists() and vm else None

    art_meta = begin_version(
        editorial_paths.root,
        phase="monologue",
        provider=provider,
        inputs=storyboard_input,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=sb_parent_id,
    )
    v = art_meta.version
    base = f"monologue_{provider}"

    # Save intermediate artifacts
    plan_path = editorial_paths.storyboard / f"overlay_plan_{provider}_v{v}.json"
    plan_path.write_text(overlay_plan.model_dump_json(indent=2))

    if fix_log:
        fix_path = editorial_paths.storyboard / f"monologue_fixlog_{provider}_v{v}.txt"
        fix_path.write_text("\n".join(fix_log))

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(monologue.model_dump_json(indent=2))
    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path, plan_path],
        target_dir=editorial_paths.storyboard,
    )

    overlay_count = len(monologue.overlays)
    text_time = monologue.total_text_time_sec
    print(
        f"  v{v} monologue (split pipeline): {overlay_count} overlays, {text_time:.1f}s text time"
    )
    print(f"    Persona: {monologue.persona}")
    print(f"    Plan:    {plan_path}")
    print(f"    JSON:    {json_path}")
    return json_path


def run_monologue(
    editorial_paths: EditorialProjectPaths,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style_preset=None,
    user_context: dict | None = None,
    tracer=None,
    persona_hint: str | None = None,
    storyboard_path: Path | None = None,
) -> Path:
    """Phase 3: generate Visual Monologue text overlay plan from the editorial storyboard.

    If gemini_cfg.use_split_pipeline is True, delegates to the multi-call pipeline
    (Call M1 analysis → Call M2 creative → deterministic validation).

    Args:
        storyboard_path: Explicit storyboard JSON path for version switching.
                         If None, uses the latest storyboard.
    """
    # Check for split pipeline mode
    if gemini_cfg and gemini_cfg.use_split_pipeline and provider == "gemini":
        return _run_monologue_split(
            editorial_paths=editorial_paths,
            provider=provider,
            gemini_cfg=gemini_cfg,
            style_preset=style_preset,
            user_context=user_context,
            tracer=tracer,
            persona_hint=persona_hint,
            storyboard_path=storyboard_path,
        )

    from .models import EditorialStoryboard, MonologuePlan
    from .editorial_prompts import build_monologue_prompt
    from .briefing import format_brief_for_prompt

    if not style_preset or not style_preset.has_phase3:
        raise ValueError("Phase 3 requires a style preset with has_phase3=True")

    # Load storyboard — explicit path or latest
    if storyboard_path:
        storyboard_json = storyboard_path
    else:
        storyboard_json = _find_latest_storyboard(editorial_paths, provider)
    if not storyboard_json:
        raise FileNotFoundError(
            f"No storyboard found in {editorial_paths.storyboard}. Run Phase 2 first."
        )
    storyboard = EditorialStoryboard.model_validate_json(storyboard_json.read_text())

    # Load transcripts
    transcripts = _load_all_transcripts_for_monologue(editorial_paths, storyboard)

    # Build user context text
    user_context_text = None
    if user_context:
        user_context_text = format_brief_for_prompt(user_context, phase="phase2")
    else:
        from .versioning import resolve_user_context_path

        context_path = resolve_user_context_path(editorial_paths.root)
        if context_path:
            ctx = json.loads(context_path.read_text())
            user_context_text = format_brief_for_prompt(ctx, phase="phase2")

    prompt = build_monologue_prompt(
        storyboard=storyboard,
        phase3_prompt_template=style_preset.phase3_prompt,
        transcripts=transcripts,
        user_context_text=user_context_text,
    )

    # Optionally inject persona hint
    if persona_hint:
        prompt += (
            f"\n\nIMPORTANT: The filmmaker prefers the **{persona_hint}** persona. "
            "Use this persona for the monologue."
        )

    # Log what we're sending
    seg_count = len(storyboard.segments)
    transcript_count = len(transcripts) if transcripts else 0
    prompt_kb = len(prompt) / 1024
    print(
        f"  Storyboard: {seg_count} segments, {transcript_count} transcripts, prompt ~{prompt_kb:.0f}KB"
    )

    from .tracing import LLMSpinner

    if provider == "gemini":
        from google.genai import types
        from .tracing import otel_phase_span, traced_gemini_generate

        client = _get_gemini_client()

        with LLMSpinner("Generating visual monologue", provider="gemini"):
            with otel_phase_span("monologue", stage="monologue", provider="gemini"):
                response = traced_gemini_generate(
                    client,
                    model=gemini_cfg.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=gemini_cfg.temperature,
                        response_mime_type="application/json",
                        response_schema=MonologuePlan,
                    ),
                    phase="monologue",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )
        monologue = MonologuePlan.model_validate_json(response.text)

    elif provider == "claude":
        import anthropic

        from .tracing import otel_phase_span, traced_claude_generate

        client = anthropic.Anthropic(api_key=_require_api_key("ANTHROPIC_API_KEY"))
        with LLMSpinner("Generating visual monologue", provider="claude"):
            with otel_phase_span("monologue", stage="monologue", provider="claude"):
                response = traced_claude_generate(
                    client,
                    model=claude_cfg.model,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt
                            + "\n\nRespond ONLY with valid JSON matching the MonologuePlan schema.",
                        }
                    ],
                    max_tokens=claude_cfg.max_tokens * 2,
                    temperature=claude_cfg.temperature,
                    phase="monologue",
                    tracer=tracer,
                    prompt_chars=len(prompt),
                )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        monologue = MonologuePlan.model_validate_json(text)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Version and save
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)

    # Build lineage: which storyboard was this monologue derived from
    storyboard_input = {}
    storyboard_latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if storyboard_latest.exists():
        import re as _re

        resolved = storyboard_latest.resolve()
        vm = _re.search(r"_v(\d+)\.json$", resolved.name)
        if vm:
            storyboard_input["storyboard"] = f"storyboard:{provider}:v{vm.group(1)}"

    cfg_snap = {}
    if gemini_cfg:
        cfg_snap = {"model": gemini_cfg.model, "temperature": gemini_cfg.temperature}
    elif claude_cfg:
        cfg_snap = {"model": claude_cfg.model, "temperature": claude_cfg.temperature}

    # Determine storyboard parent_id for lineage-prefixed versioning
    sb_parent_id = None
    if vm:
        sb_version = int(vm.group(1))
        sb_parent_id = f"sb.{sb_version}"

    art_meta = begin_version(
        editorial_paths.root,
        phase="monologue",
        provider=provider,
        inputs=storyboard_input,
        config_snapshot=cfg_snap,
        target_dir=editorial_paths.storyboard,
        parent_id=sb_parent_id,
    )
    v = art_meta.version
    base = f"monologue_{provider}"

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(monologue.model_dump_json(indent=2))
    commit_version(
        editorial_paths.root,
        art_meta,
        output_paths=[json_path],
        target_dir=editorial_paths.storyboard,
    )

    overlay_count = len(monologue.overlays)
    text_time = monologue.total_text_time_sec
    print(f"  v{v} monologue: {overlay_count} overlays, {text_time:.1f}s text time")
    print(f"    Persona: {monologue.persona}")
    print(f"    JSON: {json_path}")
    return json_path


def _find_latest_storyboard(editorial_paths: EditorialProjectPaths, provider: str) -> Path | None:
    """Find the latest storyboard JSON for the given provider.

    Always returns the resolved versioned path (not the _latest symlink).
    """
    from .versioning import resolve_versioned_path

    latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if latest.exists():
        return resolve_versioned_path(latest)
    # Fallback: try any provider
    for p in ("gemini", "claude"):
        f = editorial_paths.storyboard / f"editorial_{p}_latest.json"
        if f.exists():
            return resolve_versioned_path(f)
    return None


def _load_all_transcripts_for_monologue(
    editorial_paths: EditorialProjectPaths,
    storyboard,
) -> dict[str, str] | None:
    """Load transcripts for clips used in the storyboard."""
    clip_ids = {seg.clip_id for seg in storyboard.segments}
    transcripts = {}
    for clip_id in clip_ids:
        clip_paths = editorial_paths.clip_paths(clip_id)
        text = _load_transcript_for_prompt(clip_paths)
        if text:
            transcripts[clip_id] = text
    return transcripts if transcripts else None


def _resolve_clip_id_refs(storyboard, known_ids: set[str]):
    """Fix abbreviated clip IDs in the storyboard by matching against known IDs.

    E.g., LLM returns "C0073" but actual clip_id is "20260330114125_C0073".
    """
    # Build a suffix lookup: "C0073" -> "20260330114125_C0073"
    suffix_map = {}
    for kid in known_ids:
        # Try common abbreviation patterns
        parts = kid.split("_")
        for i in range(len(parts)):
            suffix = "_".join(parts[i:])
            if suffix not in suffix_map:
                suffix_map[suffix] = kid

    def resolve(clip_id: str) -> str:
        if clip_id in known_ids:
            return clip_id
        if clip_id in suffix_map:
            return suffix_map[clip_id]
        # Try case-insensitive
        for k, v in suffix_map.items():
            if k.lower() == clip_id.lower():
                return v
        return clip_id  # give up, return as-is

    for seg in storyboard.segments:
        seg.clip_id = resolve(seg.clip_id)
    for d in storyboard.discarded:
        d.clip_id = resolve(d.clip_id)
    for c in storyboard.cast:
        c.appears_in = [resolve(cid) for cid in c.appears_in]


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
        if provider == "gemini":
            reviews, failed = run_phase1_gemini(
                editorial_paths,
                manifest,
                cfg.gemini,
                tracer=tracer,
                style_supplement=style_supplement,
                only_clip_ids=failed,
                user_context=user_context,
            )
        else:
            reviews, failed = run_phase1_claude(
                editorial_paths,
                manifest,
                cfg.claude,
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
    if provider == "gemini":
        reviews, failed = run_phase1_gemini(
            editorial_paths,
            manifest,
            cfg.gemini,
            force=force,
            tracer=tracer,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    elif provider == "claude":
        reviews, failed = run_phase1_claude(
            editorial_paths,
            manifest,
            cfg.claude,
            force=force,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")
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
