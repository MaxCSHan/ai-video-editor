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
    current_version,
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
            proxy_files[0], clip_paths, cfg, speaker_context=speaker_context, tracer=tracer
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


# ---------------------------------------------------------------------------
# Gemini File API URI cache (for reuse between Phase 1 and Phase 2)
# ---------------------------------------------------------------------------

FILE_API_CACHE_MAX_AGE_SEC = 90 * 60  # 90 minutes (Gemini keeps files for 2 hours)


def _load_file_api_cache(editorial_paths: EditorialProjectPaths) -> dict:
    """Load cached Gemini File API URIs."""
    cache_path = editorial_paths.root / "file_api_cache.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def _save_file_api_cache(editorial_paths: EditorialProjectPaths, cache: dict):
    """Save Gemini File API URI cache."""
    cache_path = editorial_paths.root / "file_api_cache.json"
    cache_path.write_text(json.dumps(cache, indent=2))


def _cache_file_uri(editorial_paths: EditorialProjectPaths, clip_id: str, uri: str):
    """Cache a single file URI after upload."""
    cache = _load_file_api_cache(editorial_paths)
    cache[clip_id] = {"uri": uri, "cached_at": time.time()}
    _save_file_api_cache(editorial_paths, cache)


def _get_cached_uri(cache: dict, clip_id: str) -> str | None:
    """Get a cached URI if still fresh (< 90 min old)."""
    entry = cache.get(clip_id)
    if not entry:
        return None
    age = time.time() - entry.get("cached_at", 0)
    if age > FILE_API_CACHE_MAX_AGE_SEC:
        return None
    return entry.get("uri")


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

    print(f"  {label}: uploading proxy...")
    proxy_path = Path(clip_info["proxy_path"])
    video_file = client.files.upload(file=str(proxy_path))

    while video_file.state.name == "PROCESSING":
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        print(f"  {label}: WARNING — Gemini processing failed, skipping")
        return None

    # Cache the File API URI for potential reuse in Phase 2
    _cache_file_uri(editorial_paths, clip_id, video_file.uri)

    # Load transcript if available
    transcript_text = _load_transcript_for_prompt(clip_paths)

    prompt = build_clip_review_prompt(
        clip_id=clip_id,
        filename=clip_info["filename"],
        duration_sec=clip_info["duration_sec"],
        resolution=clip_info["resolution"],
        transcript_text=transcript_text,
    )

    print(f"  {label}: reviewing with {cfg.model}...")
    from .tracing import traced_gemini_generate

    response = traced_gemini_generate(
        client,
        model=cfg.model,
        contents=[
            types.Content(
                parts=[
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"),
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
) -> list[dict]:
    """Phase 1 via Gemini: review clips in parallel."""
    clips = manifest["clips"]
    total = len(clips)

    futures = {}
    with ThreadPoolExecutor(max_workers=MAX_LLM_WORKERS) as pool:
        for i, clip_info in enumerate(clips):
            fut = pool.submit(
                _review_single_clip_gemini,
                clip_info,
                editorial_paths,
                cfg,
                force,
                i + 1,
                total,
                tracer,
            )
            futures[fut] = clip_info["clip_id"]

        results_by_id = {}
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                result = fut.result()
                if result:
                    results_by_id[clip_id] = result
            except Exception as e:
                print(f"  ERROR reviewing {clip_id}: {e}")

    # Return in original clip order
    return [results_by_id[c["clip_id"]] for c in clips if c["clip_id"] in results_by_id]


def run_phase1_claude(
    editorial_paths: EditorialProjectPaths,
    manifest: dict,
    cfg: ClaudeConfig,
    force: bool = False,
) -> list[dict]:
    """Phase 1 via Claude: send each clip's frames, get structured JSON review."""
    import base64
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. See .env.example")
    client = anthropic.Anthropic(api_key=api_key)

    reviews = []
    for i, clip_info in enumerate(manifest["clips"]):
        clip_id = clip_info["clip_id"]
        clip_paths = editorial_paths.clip_paths(clip_id)

        # Check cache
        latest_review = clip_paths.review / "review_claude_latest.json"
        legacy_review = clip_paths.review / "review_claude.json"
        if not force and (latest_review.exists() or legacy_review.exists()):
            cached = latest_review if latest_review.exists() else legacy_review
            print(f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: review cached")
            reviews.append(json.loads(cached.read_text()))
            continue

        print(f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: loading frames...")
        frames_manifest_path = clip_paths.frames / "manifest.json"
        if not frames_manifest_path.exists():
            print(f"    WARNING: No frames for {clip_id}, skipping")
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
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
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

    return reviews


# ---------------------------------------------------------------------------
# Phase 2 — Editorial assembly
# ---------------------------------------------------------------------------


def _upload_proxies_for_phase2(client, clip_reviews, editorial_paths, types) -> list:
    """Upload or reuse cached proxy videos for visual Phase 2.

    Returns list of types.Part objects referencing the uploaded videos.
    """
    cache = _load_file_api_cache(editorial_paths)
    parts = []
    seen_clips = set()

    for review in clip_reviews:
        clip_id = review.get("clip_id", "")
        if clip_id in seen_clips:
            continue
        seen_clips.add(clip_id)

        # Try cached URI first
        cached_uri = _get_cached_uri(cache, clip_id)
        if cached_uri:
            parts.append(types.Part.from_uri(file_uri=cached_uri, mime_type="video/mp4"))
            continue

        # Upload proxy
        proxy_dir = editorial_paths.clips_dir / clip_id / "proxy"
        proxy_files = list(proxy_dir.glob("*_proxy.mp4")) if proxy_dir.exists() else []
        if not proxy_files:
            continue

        print(f"    Uploading proxy: {clip_id}...")
        video_file = client.files.upload(file=str(proxy_files[0]))

        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            video_file = client.files.get(name=video_file.name)

        if video_file.state.name == "FAILED":
            print(f"    WARNING: upload failed for {clip_id}, skipping")
            continue

        _cache_file_uri(editorial_paths, clip_id, video_file.uri)
        parts.append(types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"))

    print(f"    {len(parts)} proxy videos attached to Phase 2")
    return parts


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

    prompt = build_editorial_assembly_prompt(
        project_name=project_name,
        clip_reviews=clip_reviews,
        style=style,
        clip_count=len(clip_reviews),
        total_duration_sec=total_duration,
        transcripts=transcripts,
        visual=visual,
    )

    # Inject user context if provided
    if user_context:
        context_text = format_context_for_prompt(user_context)
        prompt = prompt + "\n\n" + context_text

    mode_label = "visual" if visual else "text-only"
    print(f"  Generating editorial storyboard ({provider}, {mode_label})...")

    if provider == "gemini":
        from google import genai
        from google.genai import types

        from .tracing import traced_gemini_generate

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

        # Build contents: text-only or multipart with proxy videos
        num_videos = 0
        if visual:
            video_parts = _upload_proxies_for_phase2(client, clip_reviews, editorial_paths, types)
            num_videos = len(video_parts)
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
) -> Path:
    """Full editorial pipeline: discover → preprocess → Phase 1 → Phase 2."""
    from .tracing import ProjectTracer

    cfg = cfg or DEFAULT_CONFIG
    editorial_paths = cfg.editorial_project(project_name)
    editorial_paths.ensure_dirs()

    tracer = ProjectTracer(editorial_paths.root)

    # Discover clips
    print(f"[1/4] Discovering clips in {source_dir}...")
    clip_files = discover_source_clips(source_dir)
    if not clip_files:
        raise RuntimeError(f"No video files found in {source_dir}")
    total_label = ", ".join(f.name for f in clip_files[:5])
    if len(clip_files) > 5:
        total_label += f", ... ({len(clip_files)} total)"
    print(f"  Found {len(clip_files)} clips: {total_label}")

    # Preprocess all clips
    print(f"[2/4] Preprocessing {len(clip_files)} clips...")
    clip_metadata = preprocess_all_clips(clip_files, editorial_paths, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, editorial_paths, project_name)
    total_dur = format_duration(manifest["total_duration_sec"])
    print(f"  Total raw footage: {total_dur}")

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

    # Phase 1 — per-clip reviews
    print(f"[3/4] Phase 1: Reviewing clips with {provider}...")
    if provider == "gemini":
        reviews = run_phase1_gemini(
            editorial_paths, manifest, cfg.gemini, force=force, tracer=tracer
        )
    elif provider == "claude":
        reviews = run_phase1_claude(editorial_paths, manifest, cfg.claude, force=force)
    else:
        raise ValueError(f"Unknown provider: {provider}")
    print(f"  Reviewed {len(reviews)} clips")

    # Briefing — optional interactive user context (smart briefing if Gemini available)
    user_context = None
    if interactive:
        if os.environ.get("GEMINI_API_KEY"):
            from .briefing import run_smart_briefing

            user_context = run_smart_briefing(
                editorial_paths,
                style,
                gemini_model=cfg.transcribe.gemini_model,
                tracer=tracer,
            )
        else:
            from .briefing import run_briefing

            user_context = run_briefing(reviews, style, editorial_paths.root)

    # Phase 2 — editorial assembly
    step = "4/4" if not interactive else "4/4"
    print(f"[{step}] Phase 2: Generating editorial storyboard...")
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
    )

    tracer.print_summary("Pipeline Total")
    return output_path
