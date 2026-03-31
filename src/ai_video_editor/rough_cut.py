"""Rough cut executor — LLM-structured EDL → ffmpeg assembly → HTML preview."""

import json
import os
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import EditorialProjectPaths, GeminiConfig, ClaudeConfig
from .preprocess import get_video_duration
from .storyboard_format import format_duration
from .versioning import next_version, versioned_dir, update_latest_symlink


# ---------------------------------------------------------------------------
# Data model — the structured output the LLM produces
# ---------------------------------------------------------------------------

EDL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Title of the final video"},
        "estimated_duration_sec": {"type": "number"},
        "cast": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "role": {"type": "string", "enum": ["main_subject", "companion", "bystander"]},
                    "appears_in": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "role", "appears_in"],
            },
        },
        "story_concept": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "clip_id": {"type": "string"},
                    "in_sec": {"type": "number", "description": "In-point in seconds from clip start"},
                    "out_sec": {"type": "number", "description": "Out-point in seconds from clip start"},
                    "purpose": {
                        "type": "string",
                        "enum": ["hook", "establish", "context", "action", "reaction",
                                 "b_roll", "cutaway", "climax", "payoff", "reflection",
                                 "outro", "stakes", "build_up", "tension"],
                    },
                    "description": {"type": "string"},
                    "transition": {
                        "type": "string",
                        "enum": ["cut", "dissolve", "fade_in", "fade_out",
                                 "fade_to_black", "j_cut", "l_cut", "wipe"],
                    },
                    "audio_note": {"type": "string", "description": "How audio should be handled for this segment"},
                    "text_overlay": {"type": "string", "description": "Text to overlay, or empty"},
                },
                "required": ["index", "clip_id", "in_sec", "out_sec", "purpose",
                              "description", "transition"],
            },
        },
        "discarded": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["clip_id", "reason"],
            },
        },
        "music_plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "strategy": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["section", "strategy"],
            },
        },
        "technical_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "estimated_duration_sec", "segments", "story_concept"],
}

STRUCTURED_EDL_PROMPT = """\
You are a professional video editor. Below is an editorial storyboard you created for a video project.
Now convert this editorial plan into a precise, machine-readable JSON edit decision list.

IMPORTANT:
- All timestamps must be in SECONDS (float), not timecodes
- in_sec and out_sec are relative to the START of each clip (not the overall timeline)
- Be precise with in/out points — they will be used to cut video with ffmpeg
- Include every segment from the EDL, in order
- Preserve the creative intent of the storyboard

Editorial Storyboard:
---
{editorial_md}
---

Produce the structured JSON now.
"""


# ---------------------------------------------------------------------------
# Phase 3 — LLM structured output
# ---------------------------------------------------------------------------

def generate_structured_edl_gemini(
    editorial_md: str,
    cfg: GeminiConfig,
    editorial_paths: EditorialProjectPaths | None = None,
) -> dict:
    """Ask Gemini to produce structured EDL JSON, optionally with video context.

    If editorial_paths is provided, uploads clip proxies (up to 10) alongside
    the editorial text so Gemini can verify timestamps against actual footage.
    """
    from google import genai
    from google.genai import types
    import time

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt_text = STRUCTURED_EDL_PROMPT.format(editorial_md=editorial_md)

    content_parts = []

    # Upload proxy videos for visual grounding (up to 10, Gemini limit)
    if editorial_paths:
        clip_ids = editorial_paths.discover_clips()
        proxies_to_upload = []
        for clip_id in clip_ids[:10]:
            cp = editorial_paths.clip_paths(clip_id)
            proxy_files = list(cp.proxy.glob("*.mp4")) if cp.proxy.exists() else []
            if proxy_files:
                proxies_to_upload.append((clip_id, proxy_files[0]))

        if proxies_to_upload:
            print(f"    Uploading {len(proxies_to_upload)} proxy videos for visual grounding...")
            for clip_id, proxy_path in proxies_to_upload:
                video_file = client.files.upload(file=str(proxy_path))
                while video_file.state.name == "PROCESSING":
                    time.sleep(2)
                    video_file = client.files.get(name=video_file.name)
                if video_file.state.name != "FAILED":
                    content_parts.append(
                        types.Part.from_text(text=f"[Proxy video for clip: {clip_id}]")
                    )
                    content_parts.append(
                        types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4")
                    )

    content_parts.append(types.Part.from_text(text=prompt_text))

    response = client.models.generate_content(
        model=cfg.model,
        contents=[types.Content(parts=content_parts)],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=EDL_SCHEMA,
        ),
    )
    return json.loads(response.text)


def generate_structured_edl_claude(
    editorial_md: str,
    cfg: ClaudeConfig,
) -> dict:
    """Ask Claude to produce structured EDL JSON from the editorial storyboard."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = STRUCTURED_EDL_PROMPT.format(editorial_md=editorial_md)

    response = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens * 2,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract JSON from response
    text = response.content[0].text.strip()
    # Handle potential code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_edl(edl_data: dict, editorial_paths: EditorialProjectPaths) -> list[str]:
    """Validate EDL segments against actual clip durations. Returns list of warnings.
    Also clamps out-of-bounds timestamps in-place."""
    warnings = []
    clip_durations: dict[str, float] = {}

    for seg in edl_data["segments"]:
        clip_id = seg["clip_id"]

        # Get clip duration (cached)
        if clip_id not in clip_durations:
            source = _resolve_clip_source(clip_id, editorial_paths)
            if source:
                clip_durations[clip_id] = get_video_duration(source)
            else:
                warnings.append(f"#{seg['index']}: source not found for {clip_id}")
                continue

        clip_dur = clip_durations[clip_id]
        in_sec = seg["in_sec"]
        out_sec = seg["out_sec"]
        dur = out_sec - in_sec

        # Clamp out_sec to clip end
        if out_sec > clip_dur:
            warnings.append(
                f"#{seg['index']} {clip_id}: out_sec {out_sec:.1f}s exceeds clip duration "
                f"{clip_dur:.1f}s — clamped to {clip_dur:.1f}s"
            )
            seg["out_sec"] = clip_dur

        # Clamp in_sec
        if in_sec >= clip_dur:
            warnings.append(
                f"#{seg['index']} {clip_id}: in_sec {in_sec:.1f}s exceeds clip duration "
                f"{clip_dur:.1f}s — segment will be skipped"
            )
            seg["_skip"] = True
            continue

        # Check for invalid range
        if in_sec >= seg["out_sec"]:
            warnings.append(
                f"#{seg['index']} {clip_id}: in_sec ({in_sec:.1f}) >= out_sec ({seg['out_sec']:.1f}) — skipped"
            )
            seg["_skip"] = True
            continue

        # Warn on suspiciously short segments
        final_dur = seg["out_sec"] - in_sec
        if final_dur < 0.5:
            warnings.append(
                f"#{seg['index']} {clip_id}: very short segment ({final_dur:.2f}s)"
            )

    return warnings


# ---------------------------------------------------------------------------
# ffmpeg assembly
# ---------------------------------------------------------------------------

def _resolve_clip_source(clip_id: str, editorial_paths: EditorialProjectPaths) -> Path | None:
    clip_paths = editorial_paths.clip_paths(clip_id)
    source_dir = clip_paths.source
    if source_dir.exists():
        files = [f for f in source_dir.iterdir() if f.is_file()]
        if files:
            return files[0]
    return None


def _extract_segment(source_path: Path, in_sec: float, out_sec: float, output_path: Path) -> bool:
    duration = out_sec - in_sec
    if duration <= 0:
        return False
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(in_sec),
            "-i", str(source_path),
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path),
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _extract_thumbnail(source_path: Path, timestamp_sec: float, output_path: Path) -> bool:
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(timestamp_sec),
            "-i", str(source_path),
            "-frames:v", "1", "-q:v", "5",
            str(output_path),
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def assemble_rough_cut(
    edl_data: dict, editorial_paths: EditorialProjectPaths, version_dir: Path | None = None
) -> tuple[Path, list[str]]:
    """Assemble a rough cut video from the structured EDL. Returns (path, warnings)."""
    out_dir = version_dir or editorial_paths.exports
    out_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = out_dir / "segments"
    segments_dir.mkdir(exist_ok=True)

    segments = edl_data["segments"]
    segment_files = []
    warnings = []

    for seg in segments:
        if seg.get("_skip"):
            continue

        source = _resolve_clip_source(seg["clip_id"], editorial_paths)
        if not source:
            warnings.append(f"#{seg['index']}: source not found for {seg['clip_id']}")
            continue

        seg_path = segments_dir / f"seg_{seg['index']:03d}_{seg['clip_id']}.mp4"
        expected_dur = seg["out_sec"] - seg["in_sec"]
        print(f"  [{seg['index']}/{len(segments)}] {seg['clip_id']} "
              f"{seg['in_sec']:.1f}s-{seg['out_sec']:.1f}s ({expected_dur:.1f}s) — {seg['purpose']}")

        if seg_path.exists() and seg_path.stat().st_size > 0:
            segment_files.append(seg_path)
            continue

        ok = _extract_segment(source, seg["in_sec"], seg["out_sec"], seg_path)
        if ok and seg_path.exists():
            # Post-extraction duration check
            actual_dur = get_video_duration(seg_path)
            if abs(actual_dur - expected_dur) > 1.0:
                warnings.append(
                    f"#{seg['index']} {seg['clip_id']}: expected {expected_dur:.1f}s, "
                    f"got {actual_dur:.1f}s"
                )
            segment_files.append(seg_path)
        else:
            warnings.append(f"#{seg['index']}: ffmpeg extraction failed")

    if not segment_files:
        raise RuntimeError("No segments extracted — cannot assemble rough cut")

    # Concatenate
    rough_cut_path = out_dir / "rough_cut.mp4"
    concat_list = segments_dir / "concat_list.txt"
    concat_list.write_text(
        "\n".join(f"file '{seg.resolve()}'" for seg in segment_files) + "\n"
    )

    print(f"\n  Concatenating {len(segment_files)} segments...")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(rough_cut_path),
        ],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    size_mb = rough_cut_path.stat().st_size / 1024 / 1024
    print(f"  Rough cut: {rough_cut_path} ({size_mb:.1f} MB)")
    return rough_cut_path, warnings


# ---------------------------------------------------------------------------
# HTML timeline preview
# ---------------------------------------------------------------------------

PURPOSE_COLORS = {
    "hook": "#e74c3c", "establish": "#2c3e50", "context": "#2980b9",
    "action": "#e67e22", "reaction": "#f39c12", "b_roll": "#7f8c8d",
    "cutaway": "#95a5a6", "climax": "#c0392b", "payoff": "#27ae60",
    "reflection": "#16a085", "outro": "#8e44ad", "stakes": "#d35400",
    "build_up": "#f1c40f", "tension": "#e74c3c",
}


def generate_timeline_preview(
    edl_data: dict,
    editorial_paths: EditorialProjectPaths,
    version_dir: Path | None = None,
    warnings: list[str] | None = None,
) -> Path:
    """Generate an HTML timeline preview with thumbnails."""
    exports_dir = version_dir or editorial_paths.exports
    exports_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = exports_dir / "thumbnails"
    thumbs_dir.mkdir(exist_ok=True)

    segments = edl_data["segments"]
    total_dur = sum(s["out_sec"] - s["in_sec"] for s in segments)
    title = edl_data.get("title", "Editorial Preview")
    concept = edl_data.get("story_concept", "")

    # Extract thumbnails
    for seg in segments:
        source = _resolve_clip_source(seg["clip_id"], editorial_paths)
        if not source:
            continue
        thumb_path = thumbs_dir / f"thumb_{seg['index']:03d}.jpg"
        if not thumb_path.exists():
            mid = seg["in_sec"] + (seg["out_sec"] - seg["in_sec"]) / 2
            _extract_thumbnail(source, mid, thumb_path)

    # Build rows
    edl_rows = ""
    timeline_blocks = ""
    for seg in segments:
        dur = seg["out_sec"] - seg["in_sec"]
        color = PURPOSE_COLORS.get(seg["purpose"], "#95a5a6")
        width_pct = max((dur / total_dur) * 100, 1.5)
        thumb_file = f"thumb_{seg['index']:03d}.jpg"
        thumb_exists = (thumbs_dir / thumb_file).exists()
        thumb_html = f'<img src="thumbnails/{thumb_file}" />' if thumb_exists else '<div class="no-thumb"></div>'
        overlay = seg.get("text_overlay", "")
        audio = seg.get("audio_note", "")

        edl_rows += f"""
        <tr>
          <td class="idx">{seg['index']}</td>
          <td class="thumb-cell">{thumb_html}</td>
          <td><strong>{seg['clip_id']}</strong></td>
          <td class="tc">{format_duration(seg['in_sec'])}</td>
          <td class="tc">{format_duration(seg['out_sec'])}</td>
          <td class="tc">{dur:.1f}s</td>
          <td><span class="tag" style="background:{color}">{seg['purpose']}</span></td>
          <td class="desc">{seg['description']}</td>
          <td class="trans">{seg['transition']}</td>
          <td class="desc">{audio}</td>
        </tr>"""

        timeline_blocks += f"""
        <div class="tl-block" style="width:{width_pct}%;background:{color}"
             title="#{seg['index']} {seg['clip_id']} ({dur:.1f}s) — {seg['purpose']}">
          <span>{seg['index']}</span>
        </div>"""

    # Cast table
    cast_rows = ""
    for p in edl_data.get("cast", []):
        cast_rows += f"<tr><td><strong>{p['name']}</strong></td><td>{p['description']}</td><td>{p['role']}</td><td>{', '.join(p['appears_in'])}</td></tr>"

    # Music plan
    music_rows = ""
    for m in edl_data.get("music_plan", []):
        music_rows += f"<tr><td>{m['section']}</td><td>{m['strategy']}</td><td>{m.get('notes','')}</td></tr>"

    # Technical notes
    tech_notes = "".join(f"<li>{n}</li>" for n in edl_data.get("technical_notes", []))

    # Legend (only purposes used)
    used_purposes = set(s["purpose"] for s in segments)
    legend = "".join(
        f'<span><span class="dot" style="background:{PURPOSE_COLORS.get(p,"#999")}"></span>{p.replace("_"," ")}</span>'
        for p in PURPOSE_COLORS if p in used_purposes
    )

    has_video = (exports_dir / "rough_cut.mp4").exists()
    video_html = '<video controls preload="metadata"><source src="rough_cut.mp4" type="video/mp4" /></video>' if has_video else '<p class="muted">Run <code>vx cut</code> to generate the rough cut video.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 32px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 28px; font-weight: 600; color: #fff; }}
  .meta {{ color: #666; font-size: 13px; margin: 4px 0 8px; }}
  .concept {{ color: #aaa; font-size: 14px; line-height: 1.6; margin-bottom: 32px; max-width: 700px; }}
  h2 {{ font-size: 12px; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: 1.5px; margin: 36px 0 12px; }}
  .timeline {{ display: flex; height: 44px; border-radius: 6px; overflow: hidden; gap: 1px; }}
  .tl-block {{ display: flex; align-items: center; justify-content: center; color: #fff; font-size: 11px; font-weight: 600; cursor: default; min-width: 18px; opacity: 0.85; }}
  .tl-block:hover {{ opacity: 1; }}
  .legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0 0; }}
  .legend span {{ font-size: 11px; display: flex; align-items: center; gap: 4px; color: #777; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #444; padding: 8px 10px; border-bottom: 1px solid #1a1a1a; }}
  td {{ padding: 10px; border-bottom: 1px solid #141414; vertical-align: middle; font-size: 13px; }}
  tr:hover {{ background: #111; }}
  .idx {{ color: #444; width: 28px; font-variant-numeric: tabular-nums; }}
  .tc {{ font-family: 'SF Mono', Menlo, monospace; font-size: 12px; color: #888; white-space: nowrap; }}
  .desc {{ color: #999; max-width: 280px; font-size: 12px; line-height: 1.4; }}
  .trans {{ color: #555; font-size: 12px; }}
  .thumb-cell img {{ width: 96px; height: 54px; object-fit: cover; border-radius: 4px; display: block; }}
  .no-thumb {{ width: 96px; height: 54px; background: #1a1a1a; border-radius: 4px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: 700; color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }}
  video {{ width: 100%; max-width: 640px; border-radius: 8px; margin-top: 8px; background: #000; }}
  .muted {{ color: #444; font-size: 13px; font-style: italic; }}
  .cast-table td {{ font-size: 13px; }}
  ul {{ padding-left: 20px; color: #999; font-size: 13px; line-height: 1.6; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{len(segments)} segments &middot; ~{format_duration(total_dur)} &middot; {len(edl_data.get('cast',[]))} cast</div>
<p class="concept">{concept}</p>

<h2>Timeline</h2>
<div class="timeline">{timeline_blocks}</div>
<div class="legend">{legend}</div>

<h2>Rough Cut</h2>
{video_html}

{"<h2>Cast</h2><table><tr><th>Name</th><th>Description</th><th>Role</th><th>Appears In</th></tr>" + cast_rows + "</table>" if cast_rows else ""}

<h2>Edit Decision List</h2>
<table>
<thead><tr><th>#</th><th>Preview</th><th>Clip</th><th>In</th><th>Out</th><th>Dur</th><th>Purpose</th><th>Description</th><th>Transition</th><th>Audio</th></tr></thead>
<tbody>{edl_rows}</tbody>
</table>

{"<h2>Music Plan</h2><table><tr><th>Section</th><th>Strategy</th><th>Notes</th></tr>" + music_rows + "</table>" if music_rows else ""}

{"<h2>Technical Notes</h2><ul>" + tech_notes + "</ul>" if tech_notes else ""}

{"<h2 style='color:#e74c3c'>Warnings</h2><ul style='color:#e74c3c'>" + "".join(f"<li>{w}</li>" for w in (warnings or [])) + "</ul>" if warnings else ""}

</body>
</html>"""

    preview_path = exports_dir / "preview.html"
    preview_path.write_text(html)
    return preview_path


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_rough_cut(
    editorial_md_path: Path,
    editorial_paths: EditorialProjectPaths,
    provider: str = "gemini",
    gemini_cfg: GeminiConfig | None = None,
    claude_cfg: ClaudeConfig | None = None,
    assemble: bool = True,
) -> dict:
    """Full rough cut pipeline: editorial MD → LLM structured output → ffmpeg assembly → HTML preview."""
    editorial_md = editorial_md_path.read_text()

    # Version this cut run
    v = next_version(editorial_paths.root, "cut")
    vdir = versioned_dir(editorial_paths.exports, v)
    print(f"  Cut version: v{v}")

    # Phase 3: Get structured EDL from LLM
    print("  Generating structured EDL via LLM...")
    if provider == "gemini":
        edl_data = generate_structured_edl_gemini(
            editorial_md, gemini_cfg, editorial_paths=editorial_paths
        )
    elif provider == "claude":
        edl_data = generate_structured_edl_claude(editorial_md, claude_cfg)
    else:
        raise ValueError(f"Unknown provider: {provider}")

    # Validate EDL against actual clips
    print("  Validating EDL...")
    validation_warnings = validate_edl(edl_data, editorial_paths)
    if validation_warnings:
        for w in validation_warnings:
            print(f"    WARNING: {w}")
    else:
        print("    All segments valid")

    # Save structured EDL
    edl_path = vdir / "edl.json"
    edl_path.write_text(json.dumps(edl_data, indent=2, ensure_ascii=False))
    print(f"  Structured EDL: {edl_path} ({len(edl_data['segments'])} segments)")

    result = {"edl_data": edl_data, "edl_path": edl_path, "version": v, "warnings": validation_warnings}
    all_warnings = list(validation_warnings)

    if assemble:
        print("\n  Assembling rough cut...")
        rough_cut_path, assembly_warnings = assemble_rough_cut(edl_data, editorial_paths, vdir)
        all_warnings.extend(assembly_warnings)
        result["rough_cut"] = rough_cut_path

    print("\n  Generating timeline preview...")
    preview_path = generate_timeline_preview(
        edl_data, editorial_paths, version_dir=vdir, warnings=all_warnings
    )
    result["preview"] = preview_path

    # Symlink latest
    update_latest_symlink(vdir)

    return result
