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
    TranscribeConfig,
    DEFAULT_CONFIG,
)
from .editorial_prompts import (
    build_clip_review_prompt,
    build_editorial_assembly_prompt,
    parse_clip_review,
)
from .versioning import (
    next_version,
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
    manifest = json.loads(manifest_path.read_text())
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
        "resolution": f"{video_info['width']}x{video_info['height']}",
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
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                results_by_id[clip_id] = fut.result()
            except Exception as e:
                print(f"  ERROR preprocessing {clip_id}: {e}")

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
        print(f"  {label}: transcript cached")
        transcript_path = clip_paths.audio / "transcript.json"
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

        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                _, transcript = fut.result()
                if transcript:
                    results[clip_id] = transcript
            except Exception as e:
                print(f"  ERROR transcribing {clip_id}: {e}")

    return results


def _load_transcript_for_prompt(clip_paths) -> str | None:
    """Load and format a clip's transcript for LLM prompt injection."""
    transcript_path = clip_paths.audio / "transcript.json"
    if not transcript_path.exists():
        return None
    from .transcribe import format_transcript_for_prompt

    transcript = json.loads(transcript_path.read_text())
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

    transcripts = {}
    for review in clip_reviews:
        clip_id = review.get("clip_id", "")
        clip_paths = editorial_paths.clip_paths(clip_id)
        transcript_path = clip_paths.audio / "transcript.json"
        if transcript_path.exists():
            t = json.loads(transcript_path.read_text())
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
    from google import genai
    from google.genai import types

    clip_id = clip_info["clip_id"]
    clip_paths = editorial_paths.clip_paths(clip_id)
    label = f"[{index}/{total}] {clip_id}"

    # Check cache
    latest_review = clip_paths.review / "review_gemini_latest.json"
    legacy_review = clip_paths.review / "review_gemini.json"
    if not force and (latest_review.exists() or legacy_review.exists()):
        cached = latest_review if latest_review.exists() else legacy_review
        print(f"  {label}: review cached")
        return json.loads(cached.read_text())

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

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

        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            video_file = client.files.get(name=video_file.name)

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
        transcript_text=transcript_text,
        style_supplement=style_supplement,
        user_context=user_context,
    )

    print(f"  {label}: reviewing with {cfg.model}...")
    from .tracing import traced_gemini_generate

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
        config=types.GenerateContentConfig(temperature=cfg.temperature),
        phase="phase1",
        clip_id=clip_id,
        tracer=tracer,
        num_video_files=1,
        prompt_chars=len(prompt),
    )

    review = parse_clip_review(response.text)

    v = next_version(clip_paths.root, "review_gemini")
    vpath = versioned_path(clip_paths.review / "review_gemini.json", v)
    vpath.write_text(json.dumps(review, indent=2, ensure_ascii=False))
    update_latest_symlink(vpath)
    print(f"  {label}: review complete (v{v})")
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
                    results_by_id[cid] = json.loads(cached.read_text())
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
                    reviews.append(json.loads(cached.read_text()))
                    break
            continue

        # Check cache (skip when retrying specific clips)
        if not only_clip_ids:
            latest_review = clip_paths.review / "review_claude_latest.json"
            legacy_review = clip_paths.review / "review_claude.json"
            if not force and (latest_review.exists() or legacy_review.exists()):
                cached = latest_review if latest_review.exists() else legacy_review
                print(f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: review cached")
                reviews.append(json.loads(cached.read_text()))
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
                transcript_text=transcript_text,
                style_supplement=style_supplement,
                user_context=user_context,
            )
            content.append({"type": "text", "text": prompt})

            print(f"    Reviewing with {cfg.model} ({len(frames_to_send)} frames)...")
            response = client.messages.create(
                model=cfg.model,
                max_tokens=cfg.max_tokens,
                temperature=cfg.temperature,
                messages=[{"role": "user", "content": content}],
            )

            review = parse_clip_review(response.content[0].text)

            v = next_version(clip_paths.root, "review_claude")
            vpath = versioned_path(clip_paths.review / "review_claude.json", v)
            vpath.write_text(json.dumps(review, indent=2, ensure_ascii=False))
            update_latest_symlink(vpath)
            reviews.append(review)
        except Exception as e:
            print(f"  ERROR reviewing {clip_id}: {e}")
            failed_ids.append(clip_id)

    return reviews, failed_ids


# ---------------------------------------------------------------------------
# Phase 2 — Editorial assembly
# ---------------------------------------------------------------------------


# Phase 2 visual mode now uses concat_proxies() from preprocess.py instead of
# individual uploads. See run_phase2() below.


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
) -> Path:
    """Phase 2: produce structured EditorialStoryboard + render markdown and HTML preview."""
    from .models import EditorialStoryboard
    from .render import render_markdown, render_html_preview
    from .briefing import format_context_for_prompt

    total_duration = sum(
        sum(seg.get("duration_sec", 0) for seg in r.get("usable_segments", []))
        for r in clip_reviews
    )
    if total_duration == 0:
        total_duration = sum(r.get("duration_sec", 0) for r in clip_reviews if "duration_sec" in r)

    # Load transcripts for all clips
    transcripts = _load_all_transcripts_for_prompt(clip_reviews, editorial_paths)

    # Resolve visual mode: concatenate proxies into bundles for Gemini.
    # Uses concat_proxies() to avoid the 10-video-per-prompt limit.
    visual_timeline = None
    video_parts = []
    if visual and provider == "gemini":
        from google import genai
        from google.genai import types
        from .preprocess import concat_proxies

        clip_ids = list(dict.fromkeys(r.get("clip_id", "") for r in clip_reviews))
        bundles = concat_proxies(editorial_paths, clip_ids)

        if bundles:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            file_cache = load_file_api_cache(editorial_paths)

            for i, bundle in enumerate(bundles):
                cache_key = f"_concat_bundle_{i}"
                cached_uri = get_cached_uri(file_cache, cache_key)
                if cached_uri:
                    video_parts.append(
                        types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4")
                    )
                    continue

                print(f"  Uploading concat bundle {i + 1}/{len(bundles)}...")
                video_file = client.files.upload(file=str(bundle["path"]))
                while video_file.state.name == "PROCESSING":
                    time.sleep(3)
                    video_file = client.files.get(name=video_file.name)
                if video_file.state.name == "FAILED":
                    print(f"  WARNING: bundle {i + 1} upload failed")
                    continue
                cache_file_uri(editorial_paths, cache_key, video_file.uri)
                video_parts.append(
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                )

            visual_timeline = bundles

    prompt = build_editorial_assembly_prompt(
        project_name=project_name,
        clip_reviews=clip_reviews,
        style=style,
        clip_count=len(clip_reviews),
        total_duration_sec=total_duration,
        transcripts=transcripts,
        visual_timeline=visual_timeline,
        style_supplement=style_supplement,
    )

    # Inject user context if provided
    if user_context:
        context_text = format_context_for_prompt(user_context)
        prompt = prompt + "\n\n" + context_text

    mode_label = "visual" if visual else "text-only"
    print(f"  Generating editorial storyboard ({provider}, {mode_label})...")

    if provider == "gemini":
        if not visual_timeline:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

        from .tracing import traced_gemini_generate

        # Build contents: text-only or multipart with concat video bundles
        num_videos = len(video_parts)
        if visual_timeline and video_parts:
            contents = [types.Content(parts=[*video_parts, types.Part.from_text(text=prompt)])]
        else:
            contents = prompt

        response = traced_gemini_generate(
            client,
            model=gemini_cfg.model,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=gemini_cfg.temperature,
                response_mime_type="application/json",
                response_schema=EditorialStoryboard,
            ),
            phase="phase2",
            tracer=tracer,
            prompt_chars=len(prompt),
            num_video_files=num_videos,
        )
        storyboard = EditorialStoryboard.model_validate_json(response.text)

    elif provider == "claude":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=claude_cfg.model,
            max_tokens=claude_cfg.max_tokens * 2,
            temperature=claude_cfg.temperature,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                    + "\n\nRespond ONLY with valid JSON matching the EditorialStoryboard schema.",
                }
            ],
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

    # Version and save outputs
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)
    v = next_version(editorial_paths.root, "analyze")
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

    print(f"  v{v} outputs:")
    print(f"    JSON:    {json_path}")
    print(f"    MD:      {md_path}")
    print(f"    Preview: {preview_path}")
    return json_path


# ---------------------------------------------------------------------------
# Phase 3 — Visual Monologue (text overlay generation)
# ---------------------------------------------------------------------------


def run_monologue(
    editorial_paths: EditorialProjectPaths,
    provider: str,
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    style_preset=None,
    user_context: dict | None = None,
    tracer=None,
    persona_hint: str | None = None,
) -> Path:
    """Phase 3: generate Visual Monologue text overlay plan from the editorial storyboard."""
    from .models import EditorialStoryboard, MonologuePlan
    from .editorial_prompts import build_monologue_prompt
    from .briefing import format_context_for_prompt

    if not style_preset or not style_preset.has_phase3:
        raise ValueError("Phase 3 requires a style preset with has_phase3=True")

    # Load the latest storyboard
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
        user_context_text = format_context_for_prompt(user_context)
    else:
        context_path = editorial_paths.root / "user_context.json"
        if context_path.exists():
            ctx = json.loads(context_path.read_text())
            user_context_text = format_context_for_prompt(ctx)

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
        from google import genai
        from google.genai import types
        from .tracing import traced_gemini_generate

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

        with LLMSpinner("Generating visual monologue", provider="gemini"):
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

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        with LLMSpinner("Generating visual monologue", provider="claude"):
            response = client.messages.create(
                model=claude_cfg.model,
                max_tokens=claude_cfg.max_tokens * 2,
                temperature=claude_cfg.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                        + "\n\nRespond ONLY with valid JSON matching the MonologuePlan schema.",
                    }
                ],
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        monologue = MonologuePlan.model_validate_json(text)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Version and save
    editorial_paths.storyboard.mkdir(parents=True, exist_ok=True)
    v = next_version(editorial_paths.root, "monologue")
    base = f"monologue_{provider}"

    json_path = versioned_path(editorial_paths.storyboard / f"{base}.json", v)
    json_path.write_text(monologue.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    overlay_count = len(monologue.overlays)
    text_time = monologue.total_text_time_sec
    print(f"  v{v} monologue: {overlay_count} overlays, {text_time:.1f}s text time")
    print(f"    Persona: {monologue.persona}")
    print(f"    JSON: {json_path}")
    return json_path


def _find_latest_storyboard(editorial_paths: EditorialProjectPaths, provider: str) -> Path | None:
    """Find the latest storyboard JSON for the given provider."""
    latest = editorial_paths.storyboard / f"editorial_{provider}_latest.json"
    if latest.exists():
        return latest
    # Fallback: try any provider
    for p in ("gemini", "claude"):
        f = editorial_paths.storyboard / f"editorial_{p}_latest.json"
        if f.exists():
            return f
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
) -> Path:
    """Full editorial pipeline: discover → preprocess → Phase 1 → Phase 2 → optional Phase 3."""
    from .tracing import ProjectTracer

    cfg = cfg or DEFAULT_CONFIG
    editorial_paths = cfg.editorial_project(project_name)
    editorial_paths.ensure_dirs()

    tracer = ProjectTracer(editorial_paths.root)

    # Resolve style supplements from preset
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None
    has_phase3 = style_preset.has_phase3 if style_preset else False

    total_phases = 5 if has_phase3 else 4

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
        speaker_context = None
        context_path = editorial_paths.root / "user_context.json"
        if context_path.exists():
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

    # Manual briefing AFTER Phase 1 (non-Gemini path) — needs Phase 1 reviews
    # to generate smart questions about detected people and highlights.
    if interactive and not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, editorial_paths.root)

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
    )

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

    tracer.print_summary("Pipeline Total")
    return output_path
