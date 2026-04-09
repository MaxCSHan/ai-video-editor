"""Phase 1 — Per-clip review (Gemini and Claude providers).

Extracted from editorial_agent.py to reduce god-module complexity.
Each clip is reviewed independently, producing a ClipReview JSON.

Gemini: native video upload + structured output schema, parallel (5 workers).
Claude: base64 frame images + JSON template in prompt, sequential.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import (
    ClaudeConfig,
    Config,
    EditorialProjectPaths,
    GeminiConfig,
)
from .domain.exceptions import FileUploadError
from .domain.validation import validate_clip_review
from .editorial_prompts import build_clip_review_prompt, parse_clip_review
from .file_cache import (
    cache_file_uri,
    get_cached_uri,
    load_file_api_cache,
)
from .infra.gemini_client import GeminiClient
from .versioning import (
    begin_version,
    commit_version,
    versioned_path,
)

# Max parallel LLM API calls
MAX_LLM_WORKERS = 5


def _load_transcript_for_prompt(clip_paths):
    """Import helper from editorial_agent to avoid circular dependency."""
    from .editorial_agent import _load_transcript_for_prompt as _helper

    return _helper(clip_paths)


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

    client = GeminiClient.from_env()

    # Check file cache before uploading (may already be cached by briefing or transcription)
    file_cache = load_file_api_cache(editorial_paths)
    cached_uri = get_cached_uri(file_cache, clip_id)

    if cached_uri:
        file_uri = cached_uri
        print(f"  {label}: using cached proxy URI")
    else:
        print(f"  {label}: uploading proxy...")
        proxy_path = Path(clip_info["proxy_path"])
        try:
            video_file = client.upload_and_wait(proxy_path, label=clip_id)
        except FileUploadError:
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
            client.raw,
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
                client.raw,
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
                        f"  [{i + 1}/{manifest['clip_count']}] {clip_id}: corrupt cache,"
                        " will re-review"
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


def run_phase1(
    editorial_paths: EditorialProjectPaths,
    manifest: dict,
    provider: str,
    cfg: Config,
    force: bool = False,
    tracer=None,
    style_supplement: str | None = None,
    only_clip_ids: list[str] | None = None,
    user_context: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Unified Phase 1 entry point — dispatches to provider-specific implementation.

    Returns (reviews, failed_clip_ids). Reviews are in original clip order.
    """
    if provider == "gemini":
        return run_phase1_gemini(
            editorial_paths,
            manifest,
            cfg.gemini,
            force=force,
            tracer=tracer,
            style_supplement=style_supplement,
            only_clip_ids=only_clip_ids,
            user_context=user_context,
        )
    elif provider == "claude":
        return run_phase1_claude(
            editorial_paths,
            manifest,
            cfg.claude,
            force=force,
            tracer=tracer,
            style_supplement=style_supplement,
            only_clip_ids=only_clip_ids,
            user_context=user_context,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")
