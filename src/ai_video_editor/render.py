"""Render EditorialStoryboard to markdown and HTML — pure templates, no LLM."""

import html as html_mod
import json
import os
import subprocess
from pathlib import Path


def _esc(val) -> str:
    """Escape a value for safe HTML interpolation (XSS prevention)."""
    return html_mod.escape(str(val), quote=True)


from .models import EditorialStoryboard
from .storyboard_format import format_duration


PURPOSE_COLORS = {
    "hook": "#e74c3c",
    "establish": "#2c3e50",
    "context": "#2980b9",
    "action": "#e67e22",
    "reaction": "#f39c12",
    "b_roll": "#7f8c8d",
    "cutaway": "#95a5a6",
    "climax": "#c0392b",
    "payoff": "#27ae60",
    "reflection": "#16a085",
    "outro": "#8e44ad",
    "stakes": "#d35400",
    "build_up": "#f1c40f",
    "tension": "#e74c3c",
    "intro": "#3498db",
}


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(sb: EditorialStoryboard) -> str:
    lines = []
    lines.append(f"# {sb.title}")
    lines.append(
        f"**Estimated final cut**: {format_duration(sb.estimated_duration_sec)} | **Style**: {sb.style}"
    )
    lines.append(
        f"**Segments**: {len(sb.segments)} | **Total segment time**: {format_duration(sb.total_segments_duration)}"
    )
    lines.append("")
    lines.append("## Story Concept")
    lines.append(sb.story_concept)
    lines.append("")
    if sb.cast:
        lines.append("## Cast")
        lines.append("| Person | Description | Role | Appears In |")
        lines.append("|--------|-------------|------|------------|")
        for c in sb.cast:
            lines.append(f"| {c.name} | {c.description} | {c.role} | {', '.join(c.appears_in)} |")
        lines.append("")
    if sb.story_arc:
        lines.append("## Story Arc")
        for arc in sb.story_arc:
            lines.append(f"### {arc.title}")
            lines.append(arc.description)
            lines.append("")
    lines.append("## Edit Decision List (EDL)")
    lines.append("| # | Clip | In | Out | Dur | Purpose | Description | Transition |")
    lines.append("|---|------|----|-----|-----|---------|-------------|------------|")
    for s in sb.segments:
        lines.append(
            f"| {s.index} | {s.clip_id} | {format_duration(s.in_sec)} | {format_duration(s.out_sec)} "
            f"| {s.duration_sec:.1f}s | {s.purpose} | {s.description} | {s.transition} |"
        )
    lines.append("")
    if sb.discarded:
        lines.append("## Discarded Clips")
        for d in sb.discarded:
            lines.append(f"- **{d.clip_id}**: {d.reason}")
        lines.append("")
    if sb.pacing_notes:
        lines.append("## Pacing Notes")
        for n in sb.pacing_notes:
            lines.append(f"- {n}")
        lines.append("")
    if sb.music_plan:
        lines.append("## Music & Audio Plan")
        lines.append("| Section | Strategy | Notes |")
        lines.append("|---------|----------|-------|")
        for m in sb.music_plan:
            lines.append(f"| {m.section} | {m.strategy} | {m.notes} |")
        lines.append("")
    if sb.technical_notes:
        lines.append("## Technical Notes")
        for n in sb.technical_notes:
            lines.append(f"- {n}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML preview — interactive with video playback and range editing
# ---------------------------------------------------------------------------


def _extract_thumbnail(source_path: Path, timestamp_sec: float, output_path: Path) -> bool:
    if output_path.exists():
        return True
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp_sec),
            "-i",
            str(source_path),
            "-frames:v",
            "1",
            "-q:v",
            "5",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _resolve_clip_source(clip_id: str, clips_dir: Path) -> Path | None:
    source_dir = clips_dir / clip_id / "source"
    if source_dir.exists():
        files = [f for f in source_dir.iterdir() if f.is_file()]
        if files:
            return files[0]
    # Proxy fallback when source drive is offline
    return _resolve_clip_proxy(clip_id, clips_dir)


def _resolve_clip_proxy(clip_id: str, clips_dir: Path) -> Path | None:
    proxy_dir = clips_dir / clip_id / "proxy"
    if proxy_dir.exists():
        files = [f for f in proxy_dir.iterdir() if f.suffix == ".mp4"]
        if files:
            return files[0]
    return None


def _get_clip_duration(clip_id: str, clips_dir: Path) -> float:
    source = _resolve_clip_source(clip_id, clips_dir)
    if not source:
        return 0
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(source)],
            capture_output=True,
            text=True,
            check=True,
        )
        import json as _json

        return float(_json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return 0


def _render_monologue_panel(monologue) -> str:
    """Render the Visual Monologue panel as HTML."""
    persona_labels = {
        "conversational_confidant": "Conversational Confidant",
        "detached_observer": "Detached Observer",
        "stream_of_consciousness": "Stream of Consciousness",
    }
    synergy_colors = {"harmony": "#3498db", "dissonance": "#e67e22"}

    persona_label = persona_labels.get(monologue.persona, monologue.persona)
    mechanics = ", ".join(monologue.tone_mechanics) if monologue.tone_mechanics else "none"
    arc = " → ".join(monologue.arc_structure) if monologue.arc_structure else "none"

    overlay_cards = []
    for ov in monologue.overlays:
        color = synergy_colors.get(ov.synergy, "#95a5a6")
        end_t = ov.appear_at + ov.duration_sec
        overlay_cards.append(
            f'<div style="border-left:3px solid {color};padding:6px 12px;margin:6px 0;'
            f'background:#1a1a2e;border-radius:4px">'
            f'<div style="display:flex;justify-content:space-between;font-size:0.85em;'
            f'color:#aaa">'
            f"<span>Segment #{ov.segment_index}</span>"
            f"<span>{ov.appear_at:.1f}s – {end_t:.1f}s ({ov.duration_sec:.1f}s)</span>"
            f'<span style="color:{color}">{ov.synergy}</span>'
            f"</div>"
            f'<div style="font-size:1.1em;margin-top:4px;font-style:italic;color:#e0e0e0">'
            f'"{ov.text}"</div>'
            + (
                f'<div style="font-size:0.8em;color:#888;margin-top:2px">{ov.note}</div>'
                if ov.note
                else ""
            )
            + "</div>"
        )

    pacing = (
        "".join(f"<li>{n}</li>" for n in monologue.pacing_notes) if monologue.pacing_notes else ""
    )
    music = (
        "".join(f"<li>{n}</li>" for n in monologue.music_sync_notes)
        if monologue.music_sync_notes
        else ""
    )

    return f"""
<h2>Visual Monologue</h2>
<div style="background:#16213e;padding:16px;border-radius:8px;margin-bottom:16px">
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px">
    <div><strong>Persona:</strong> {persona_label}</div>
    <div><strong>Text time:</strong> {monologue.total_text_time_sec:.1f}s</div>
    <div><strong>Overlays:</strong> {len(monologue.overlays)}</div>
  </div>
  <div style="margin-bottom:8px"><strong>Voice:</strong> <em>{monologue.persona_description}</em></div>
  <div style="margin-bottom:8px"><strong>Tone:</strong> {mechanics}</div>
  <div><strong>Arc:</strong> {arc}</div>
</div>
<details open>
  <summary style="cursor:pointer;font-weight:bold;margin-bottom:8px">
    Text Overlays ({len(monologue.overlays)})
  </summary>
  {"".join(overlay_cards)}
</details>
{"<h3>Pacing Notes</h3><ul>" + pacing + "</ul>" if pacing else ""}
{"<h3>Music Sync Notes</h3><ul>" + music + "</ul>" if music else ""}
"""


def render_html_preview(
    sb: EditorialStoryboard,
    clips_dir: Path | None = None,
    output_dir: Path | None = None,
    warnings: list[str] | None = None,
    rough_cut_path: Path | None = None,
    monologue=None,
) -> str:
    total_dur = sb.total_segments_duration
    thumbs_dir = output_dir / "thumbnails" if output_dir else None
    if thumbs_dir:
        thumbs_dir.mkdir(parents=True, exist_ok=True)

    # Build clip info: proxy paths, durations, and transcripts
    clip_info = {}
    clip_transcripts = {}
    if clips_dir:
        seen_clips = set(s.clip_id for s in sb.segments)
        for cid in seen_clips:
            proxy = _resolve_clip_proxy(cid, clips_dir)
            dur = _get_clip_duration(cid, clips_dir)
            if proxy:
                # Compute relative path from output_dir to proxy
                if output_dir:
                    rel = os.path.relpath(proxy, output_dir)
                else:
                    rel = str(proxy)
                clip_info[cid] = {"proxy": rel, "duration": dur}

            # Load transcript if available
            transcript_path = clips_dir / cid / "audio" / "transcript.json"
            if transcript_path.exists():
                import json as _json2

                t = _json2.loads(transcript_path.read_text())
                clip_transcripts[cid] = t.get("segments", [])

    # Extract thumbnails
    thumb_files = {}
    if clips_dir and thumbs_dir:
        for seg in sb.segments:
            source = _resolve_clip_source(seg.clip_id, clips_dir)
            if not source:
                continue
            thumb_path = thumbs_dir / f"thumb_{seg.index:03d}.jpg"
            mid = seg.in_sec + seg.duration_sec / 2
            _extract_thumbnail(source, mid, thumb_path)
            if thumb_path.exists():
                thumb_files[seg.index] = thumb_path.name

    # Serialize storyboard data for JavaScript
    sb_json = sb.model_dump_json()

    # Build EDL rows
    edl_rows = ""
    timeline_blocks = ""
    for seg in sb.segments:
        color = PURPOSE_COLORS.get(seg.purpose, "#95a5a6")
        width_pct = max((seg.duration_sec / total_dur) * 100, 1.5) if total_dur > 0 else 5
        thumb = thumb_files.get(seg.index, "")
        thumb_html = (
            f'<img src="thumbnails/{thumb}" />' if thumb else '<div class="no-thumb"></div>'
        )

        edl_rows += f"""
        <tr class="edl-row" data-seg-index="{seg.index}" onclick="openSegment({seg.index})">
          <td class="idx">{seg.index}</td>
          <td class="thumb-cell">{thumb_html}</td>
          <td><strong>{_esc(seg.clip_id)}</strong></td>
          <td class="tc">{format_duration(seg.in_sec)}</td>
          <td class="tc">{format_duration(seg.out_sec)}</td>
          <td class="tc">{seg.duration_sec:.1f}s</td>
          <td><span class="tag" style="background:{color}">{_esc(seg.purpose)}</span></td>
          <td class="desc">{_esc(seg.description)}</td>
          <td class="trans">{_esc(seg.transition)}</td>
        </tr>"""

        timeline_blocks += f"""
        <div class="tl-block" style="width:{width_pct}%;background:{color}"
             data-seg-index="{seg.index}"
             onclick="openSegment({seg.index})"
             title="#{seg.index} {_esc(seg.clip_id)} ({seg.duration_sec:.1f}s) — {_esc(seg.purpose)}">
          <span>{seg.index}</span>
        </div>"""

    # Cast, music, etc.
    cast_rows = "".join(
        f"<tr><td><strong>{_esc(p.name)}</strong></td><td>{_esc(p.description)}</td><td>{_esc(p.role)}</td><td>{_esc(', '.join(p.appears_in))}</td></tr>"
        for p in sb.cast
    )
    music_rows = "".join(
        f"<tr><td>{_esc(m.section)}</td><td>{_esc(m.strategy)}</td><td>{_esc(m.notes)}</td></tr>"
        for m in sb.music_plan
    )
    tech_notes = "".join(f"<li>{_esc(n)}</li>" for n in sb.technical_notes)
    pacing = "".join(f"<li>{_esc(n)}</li>" for n in sb.pacing_notes)
    used = set(s.purpose for s in sb.segments)
    legend = "".join(
        f'<span><span class="dot" style="background:{PURPOSE_COLORS.get(p, "#999")}"></span>{p.replace("_", " ")}</span>'
        for p in PURPOSE_COLORS
        if p in used
    )

    video_html = ""
    if rough_cut_path and rough_cut_path.exists():
        video_html = f'<video controls preload="metadata"><source src="{_esc(rough_cut_path.name)}" type="video/mp4" /></video>'
    else:
        video_html = '<p class="muted">Run <code>vx cut</code> to generate the rough cut video.</p>'

    warn_html = ""
    if warnings:
        warn_items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
        warn_html = (
            f'<h2 style="color:#e74c3c">Warnings</h2><ul style="color:#e74c3c">{warn_items}</ul>'
        )

    arc_html = "".join(
        f'<div class="arc-section"><div class="arc-title">{_esc(a.title)}</div><div class="arc-body">{_esc(a.description[:250])}</div></div>'
        for a in sb.story_arc
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(sb.title)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'SF Pro Display', 'Helvetica Neue', sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 32px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 28px; font-weight: 600; color: #fff; }}
  .meta {{ color: #666; font-size: 13px; margin: 4px 0 8px; }}
  .concept {{ color: #aaa; font-size: 14px; line-height: 1.6; margin-bottom: 32px; max-width: 700px; }}
  h2 {{ font-size: 12px; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: 1.5px; margin: 36px 0 12px; }}
  .timeline {{ display: flex; height: 44px; border-radius: 6px; overflow: hidden; gap: 1px; }}
  .tl-block {{ display: flex; align-items: center; justify-content: center; color: #fff; font-size: 11px; font-weight: 600; cursor: pointer; min-width: 18px; opacity: 0.85; transition: opacity .15s, transform .15s; }}
  .tl-block:hover {{ opacity: 1; transform: scaleY(1.15); }}
  .tl-block.active {{ opacity: 1; outline: 2px solid #fff; outline-offset: -2px; transform: scaleY(1.15); }}
  .legend {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0 0; }}
  .legend span {{ font-size: 11px; display: flex; align-items: center; gap: 4px; color: #777; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .arc-container {{ display: flex; gap: 12px; overflow-x: auto; padding-bottom: 8px; }}
  .arc-section {{ flex: 0 0 220px; background: #1a1a1a; border-radius: 8px; padding: 14px; border: 1px solid #2a2a2a; }}
  .arc-title {{ font-weight: 600; font-size: 14px; color: #fff; }}
  .arc-body {{ font-size: 12px; color: #999; line-height: 1.5; margin-top: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #444; padding: 8px 10px; border-bottom: 1px solid #1a1a1a; }}
  td {{ padding: 10px; border-bottom: 1px solid #141414; vertical-align: middle; font-size: 13px; }}
  .edl-row {{ cursor: pointer; transition: background .15s; }}
  .edl-row:hover {{ background: #1a1a1a; }}
  .edl-row.active {{ background: #1a2a1a; }}
  .idx {{ color: #444; width: 28px; font-variant-numeric: tabular-nums; }}
  .tc {{ font-family: 'SF Mono', Menlo, monospace; font-size: 12px; color: #888; white-space: nowrap; }}
  .desc {{ color: #999; max-width: 280px; font-size: 12px; line-height: 1.4; }}
  .trans {{ color: #555; font-size: 12px; }}
  .thumb-cell img {{ width: 96px; height: 54px; object-fit: cover; border-radius: 4px; display: block; }}
  .no-thumb {{ width: 96px; height: 54px; background: #1a1a1a; border-radius: 4px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: 700; color: #fff; text-transform: uppercase; letter-spacing: 0.5px; }}
  video {{ width: 100%; max-width: 640px; border-radius: 8px; margin-top: 8px; background: #000; }}
  .muted {{ color: #444; font-size: 13px; font-style: italic; }}
  ul {{ padding-left: 20px; color: #999; font-size: 13px; line-height: 1.6; }}

  /* Modal overlay */
  .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; }}
  .modal-overlay.open {{ display: flex; }}
  .modal {{ background: #141414; border-radius: 12px; width: 90vw; max-width: 900px; max-height: 90vh; overflow-y: auto; border: 1px solid #2a2a2a; }}
  .modal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid #222; }}
  .modal-header h3 {{ font-size: 16px; color: #fff; font-weight: 600; }}
  .modal-close {{ background: none; border: none; color: #666; font-size: 24px; cursor: pointer; padding: 4px 8px; }}
  .modal-close:hover {{ color: #fff; }}
  .modal-body {{ padding: 20px; }}

  /* Segment editor */
  .seg-video-container {{ position: relative; background: #000; border-radius: 8px; overflow: hidden; }}
  .seg-video {{ width: 100%; display: block; }}
  .seg-meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }}
  .seg-meta-item {{ background: #1a1a1a; border-radius: 6px; padding: 10px 14px; }}
  .seg-meta-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #555; margin-bottom: 4px; }}
  .seg-meta-value {{ font-size: 14px; color: #fff; font-weight: 500; }}

  /* Range scrubber */
  .range-container {{ margin-top: 20px; padding: 0 4px; }}
  .range-label {{ font-size: 11px; color: #666; margin-bottom: 8px; display: flex; justify-content: space-between; }}
  .range-track {{ position: relative; height: 32px; background: #1a1a1a; border-radius: 4px; cursor: pointer; user-select: none; }}
  .range-fill {{ position: absolute; top: 0; height: 100%; background: rgba(255,255,255,0.15); border-radius: 4px; pointer-events: none; }}
  .range-selected {{ position: absolute; top: 0; height: 100%; background: rgba(46,204,113,0.3); border: 1px solid rgba(46,204,113,0.6); border-radius: 4px; pointer-events: none; }}
  .range-playhead {{ position: absolute; top: -2px; width: 2px; height: 36px; background: #fff; pointer-events: none; z-index: 2; }}
  .range-handle {{ position: absolute; top: -4px; width: 12px; height: 40px; background: #2ecc71; border-radius: 3px; cursor: ew-resize; z-index: 3; display: flex; align-items: center; justify-content: center; }}
  .range-handle::after {{ content: ''; width: 2px; height: 16px; background: rgba(0,0,0,0.3); border-radius: 1px; }}
  .range-handle.out {{ background: #e74c3c; }}
  .range-time {{ position: absolute; top: 36px; font-size: 10px; font-family: 'SF Mono', Menlo, monospace; color: #888; transform: translateX(-50%); white-space: nowrap; }}

  /* Action buttons */
  .seg-actions {{ display: flex; gap: 8px; margin-top: 20px; justify-content: space-between; align-items: center; }}
  .btn {{ padding: 8px 16px; border-radius: 6px; border: none; font-size: 13px; font-weight: 600; cursor: pointer; transition: background .15s; }}
  .btn-primary {{ background: #2ecc71; color: #000; }}
  .btn-primary:hover {{ background: #27ae60; }}
  .btn-secondary {{ background: #333; color: #ccc; }}
  .btn-secondary:hover {{ background: #444; }}
  .btn-danger {{ background: transparent; color: #e74c3c; border: 1px solid #e74c3c; }}
  .btn-danger:hover {{ background: rgba(231,76,60,0.1); }}
  .seg-description {{ margin-top: 12px; padding: 10px 14px; background: #1a1a1a; border-radius: 6px; color: #999; font-size: 13px; line-height: 1.5; }}
  .seg-transcript {{ margin-top: 12px; padding: 12px 14px; background: #1a1a1a; border-radius: 6px; max-height: 200px; overflow-y: auto; }}
  .seg-transcript-list {{ display: flex; flex-direction: column; gap: 4px; }}
  .t-line {{ padding: 4px 8px; border-radius: 4px; font-size: 12px; line-height: 1.5; cursor: pointer; transition: background .15s; }}
  .t-line:hover {{ background: #2a2a2a; }}
  .t-line.t-active {{ background: #2d4a2d; }}
  .t-time {{ color: #555; font-family: 'SF Mono', Menlo, monospace; font-size: 11px; margin-right: 6px; }}
  .t-speaker {{ color: #4fc3f7; font-weight: 600; }}
  .t-music {{ color: #ce93d8; font-style: italic; }}
  .t-sfx {{ color: #ffb74d; font-style: italic; }}

  /* Toast notification */
  .toast {{ position: fixed; bottom: 24px; right: 24px; background: #2ecc71; color: #000; padding: 12px 20px; border-radius: 8px; font-weight: 600; font-size: 13px; z-index: 2000; transform: translateY(100px); opacity: 0; transition: all .3s ease; }}
  .toast.show {{ transform: translateY(0); opacity: 1; }}

  /* Export bar */
  .export-bar {{ position: sticky; bottom: 0; background: #0a0a0a; border-top: 1px solid #222; padding: 12px 0; margin-top: 32px; display: flex; justify-content: space-between; align-items: center; }}
  .changes-count {{ font-size: 13px; color: #888; }}
  .changes-count strong {{ color: #2ecc71; }}
</style>
</head>
<body>

<h1>{_esc(sb.title)}</h1>
<div class="meta">{len(sb.segments)} segments &middot; ~{format_duration(total_dur)} &middot; {len(sb.cast)} cast &middot; {_esc(sb.style)}</div>
<p class="concept">{_esc(sb.story_concept)}</p>

<h2>Timeline <span style="font-weight:400;color:#555;text-transform:none;letter-spacing:0">(click a segment to preview &amp; adjust)</span></h2>
<div class="timeline" id="timeline">{timeline_blocks}</div>
<div class="legend">{legend}</div>

{"<h2>Story Arc</h2><div class='arc-container'>" + arc_html + "</div>" if arc_html else ""}

<h2>Rough Cut</h2>
{video_html}

{"<h2>Cast</h2><table><tr><th>Name</th><th>Description</th><th>Role</th><th>Appears In</th></tr>" + cast_rows + "</table>" if cast_rows else ""}

<h2>Edit Decision List</h2>
<table>
<thead><tr><th>#</th><th>Preview</th><th>Clip</th><th>In</th><th>Out</th><th>Dur</th><th>Purpose</th><th>Description</th><th>Transition</th></tr></thead>
<tbody id="edl-body">{edl_rows}</tbody>
</table>

{"<h2>Pacing Notes</h2><ul>" + pacing + "</ul>" if pacing else ""}
{"<h2>Music Plan</h2><table><tr><th>Section</th><th>Strategy</th><th>Notes</th></tr>" + music_rows + "</table>" if music_rows else ""}
{"<h2>Technical Notes</h2><ul>" + tech_notes + "</ul>" if tech_notes else ""}
{_render_monologue_panel(monologue) if monologue else ""}
{warn_html}

<!-- Export bar -->
<div class="export-bar" id="export-bar" style="display:none">
  <div class="changes-count"><strong id="changes-count">0</strong> segment(s) adjusted</div>
  <div>
    <button class="btn btn-secondary" onclick="resetAllChanges()">Reset All</button>
    <button class="btn btn-primary" onclick="exportJSON()">Export Adjusted JSON</button>
  </div>
</div>

<!-- Segment detail modal -->
<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modal-title">Segment</h3>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body">
      <div class="seg-video-container">
        <video class="seg-video" id="seg-video" preload="metadata"></video>
      </div>

      <div class="range-container">
        <div class="range-label">
          <span>Full clip</span>
          <span id="range-duration">0:00</span>
        </div>
        <div class="range-track" id="range-track">
          <div class="range-selected" id="range-selected"></div>
          <div class="range-playhead" id="range-playhead"></div>
          <div class="range-handle" id="handle-in" data-type="in"></div>
          <div class="range-handle out" id="handle-out" data-type="out"></div>
        </div>
        <div style="position:relative;height:20px;margin-top:4px">
          <span class="range-time" id="time-in" style="left:0">0:00</span>
          <span class="range-time" id="time-out" style="right:0">0:00</span>
        </div>
      </div>

      <div class="seg-meta">
        <div class="seg-meta-item">
          <div class="seg-meta-label">In Point</div>
          <div class="seg-meta-value" id="meta-in">0:00</div>
        </div>
        <div class="seg-meta-item">
          <div class="seg-meta-label">Out Point</div>
          <div class="seg-meta-value" id="meta-out">0:00</div>
        </div>
        <div class="seg-meta-item">
          <div class="seg-meta-label">Duration</div>
          <div class="seg-meta-value" id="meta-dur">0s</div>
        </div>
        <div class="seg-meta-item">
          <div class="seg-meta-label">Purpose</div>
          <div class="seg-meta-value" id="meta-purpose">—</div>
        </div>
      </div>

      <div class="seg-description" id="seg-description"></div>

      <div class="seg-transcript" id="seg-transcript-panel" style="display:none">
        <div class="seg-meta-label" style="margin-bottom:8px">Transcript</div>
        <div class="seg-transcript-list" id="seg-transcript-list"></div>
      </div>

      <div class="seg-actions">
        <div>
          <button class="btn btn-secondary" onclick="previewRange()">Preview Selection</button>
          <button class="btn btn-secondary" onclick="playFullClip()">Play Full Clip</button>
        </div>
        <div>
          <button class="btn btn-danger" onclick="resetSegment()">Reset</button>
          <button class="btn btn-primary" onclick="applyChanges()">Apply Changes</button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// --- Data ---
const storyboard = {sb_json};
const clipInfo = {json.dumps(clip_info)};
const clipTranscripts = {json.dumps(clip_transcripts)};
const purposeColors = {json.dumps(PURPOSE_COLORS)};
let changes = {{}};  // segIndex -> {{in_sec, out_sec}}
let currentSegIndex = null;
let dragging = null;

// --- Helpers ---
function fmt(sec) {{
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const ms = Math.floor((sec % 1) * 10);
  return m + ':' + String(s).padStart(2,'0') + '.' + ms;
}}

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}}

function getSeg(index) {{
  return storyboard.segments.find(s => s.index === index);
}}

function getEffective(seg) {{
  const c = changes[seg.index];
  return c ? {{ in_sec: c.in_sec, out_sec: c.out_sec }} : {{ in_sec: seg.in_sec, out_sec: seg.out_sec }};
}}

// --- Modal ---
function openSegment(index) {{
  const seg = getSeg(index);
  if (!seg) return;
  currentSegIndex = index;
  const ci = clipInfo[seg.clip_id];
  if (!ci) {{ showToast('No proxy video for ' + seg.clip_id); return; }}

  const modal = document.getElementById('modal');
  const video = document.getElementById('seg-video');

  document.getElementById('modal-title').textContent = '#' + seg.index + ' — ' + seg.clip_id + ' (' + seg.purpose + ')';
  document.getElementById('seg-description').textContent = seg.description + (seg.audio_note ? '\\n\\nAudio: ' + seg.audio_note : '');

  video.src = ci.proxy;
  video.load();

  const eff = getEffective(seg);
  video.addEventListener('loadedmetadata', function handler() {{
    const dur = ci.duration || video.duration;
    document.getElementById('range-duration').textContent = fmt(dur);
    updateRangeUI(eff.in_sec, eff.out_sec, dur);
    video.currentTime = eff.in_sec;
    video.removeEventListener('loadedmetadata', handler);
  }});

  // Playhead tracking + transcript highlight
  video.ontimeupdate = () => {{
    const dur = ci.duration || video.duration;
    if (dur > 0) {{
      const pct = (video.currentTime / dur) * 100;
      document.getElementById('range-playhead').style.left = pct + '%';
    }}
    updateTranscriptHighlight(video.currentTime);
  }};

  // Highlight active
  document.querySelectorAll('.tl-block.active, .edl-row.active').forEach(el => el.classList.remove('active'));
  document.querySelector(`.tl-block[data-seg-index="${{index}}"]`)?.classList.add('active');
  document.querySelector(`.edl-row[data-seg-index="${{index}}"]`)?.classList.add('active');

  updateMeta(eff.in_sec, eff.out_sec, seg.purpose);
  renderSegTranscript(seg.clip_id, eff.in_sec, eff.out_sec);
  modal.classList.add('open');
}}

function closeModal() {{
  document.getElementById('modal').classList.remove('open');
  const video = document.getElementById('seg-video');
  video.pause();
  video.src = '';
  document.querySelectorAll('.tl-block.active, .edl-row.active').forEach(el => el.classList.remove('active'));
  currentSegIndex = null;
}}

// --- Range UI ---
function updateRangeUI(inSec, outSec, totalDur) {{
  const track = document.getElementById('range-track');
  const selected = document.getElementById('range-selected');
  const handleIn = document.getElementById('handle-in');
  const handleOut = document.getElementById('handle-out');
  const timeIn = document.getElementById('time-in');
  const timeOut = document.getElementById('time-out');

  if (totalDur <= 0) return;
  const inPct = (inSec / totalDur) * 100;
  const outPct = (outSec / totalDur) * 100;

  selected.style.left = inPct + '%';
  selected.style.width = (outPct - inPct) + '%';
  handleIn.style.left = 'calc(' + inPct + '% - 6px)';
  handleOut.style.left = 'calc(' + outPct + '% - 6px)';
  timeIn.style.left = inPct + '%';
  timeIn.textContent = fmt(inSec);
  timeOut.style.left = outPct + '%';
  timeOut.textContent = fmt(outSec);
}}

function updateMeta(inSec, outSec, purpose) {{
  document.getElementById('meta-in').textContent = fmt(inSec);
  document.getElementById('meta-out').textContent = fmt(outSec);
  document.getElementById('meta-dur').textContent = (outSec - inSec).toFixed(1) + 's';
  document.getElementById('meta-purpose').textContent = purpose;
}}

// --- Handle dragging ---
document.addEventListener('mousedown', (e) => {{
  if (e.target.id === 'handle-in' || e.target.id === 'handle-out') {{
    dragging = e.target.dataset.type;
    e.preventDefault();
  }}
}});

document.addEventListener('mousemove', (e) => {{
  if (!dragging || currentSegIndex === null) return;
  const seg = getSeg(currentSegIndex);
  const ci = clipInfo[seg.clip_id];
  const totalDur = ci.duration || document.getElementById('seg-video').duration;
  const track = document.getElementById('range-track');
  const rect = track.getBoundingClientRect();
  let pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  let sec = pct * totalDur;

  const eff = getEffective(seg);
  let inSec = eff.in_sec, outSec = eff.out_sec;

  if (dragging === 'in') {{
    inSec = Math.min(sec, outSec - 0.5);
    inSec = Math.max(0, inSec);
  }} else {{
    outSec = Math.max(sec, inSec + 0.5);
    outSec = Math.min(totalDur, outSec);
  }}

  // Temp update without saving
  updateRangeUI(inSec, outSec, totalDur);
  updateMeta(inSec, outSec, seg.purpose);
  // Store temporarily
  if (!changes[seg.index]) changes[seg.index] = {{ ...eff }};
  changes[seg.index].in_sec = Math.round(inSec * 10) / 10;
  changes[seg.index].out_sec = Math.round(outSec * 10) / 10;
}});

document.addEventListener('mouseup', () => {{
  if (dragging && currentSegIndex !== null) {{
    const seg = getSeg(currentSegIndex);
    const eff = getEffective(seg);
    document.getElementById('seg-video').currentTime = eff.in_sec;
  }}
  dragging = null;
}});

// Click on track to seek
document.getElementById('range-track').addEventListener('click', (e) => {{
  if (dragging) return;
  if (e.target.classList.contains('range-handle')) return;
  const seg = getSeg(currentSegIndex);
  if (!seg) return;
  const ci = clipInfo[seg.clip_id];
  const totalDur = ci.duration || document.getElementById('seg-video').duration;
  const rect = e.currentTarget.getBoundingClientRect();
  const pct = (e.clientX - rect.left) / rect.width;
  const sec = pct * totalDur;
  document.getElementById('seg-video').currentTime = sec;
}});

// --- Actions ---
function previewRange() {{
  const seg = getSeg(currentSegIndex);
  if (!seg) return;
  const eff = getEffective(seg);
  const video = document.getElementById('seg-video');
  video.currentTime = eff.in_sec;
  video.play();
  // Stop at out point
  const stopAt = eff.out_sec;
  const handler = () => {{
    if (video.currentTime >= stopAt) {{
      video.pause();
      video.removeEventListener('timeupdate', handler);
    }}
  }};
  video.addEventListener('timeupdate', handler);
}}

function playFullClip() {{
  const video = document.getElementById('seg-video');
  video.currentTime = 0;
  video.play();
}}

function applyChanges() {{
  if (currentSegIndex === null) return;
  const seg = getSeg(currentSegIndex);
  const eff = getEffective(seg);
  // Mark change
  changes[seg.index] = {{ in_sec: eff.in_sec, out_sec: eff.out_sec }};
  updateExportBar();
  showToast('Segment #' + seg.index + ' adjusted: ' + fmt(eff.in_sec) + ' → ' + fmt(eff.out_sec));
  closeModal();
}}

function resetSegment() {{
  if (currentSegIndex === null) return;
  const seg = getSeg(currentSegIndex);
  delete changes[seg.index];
  const ci = clipInfo[seg.clip_id];
  const totalDur = ci.duration || 60;
  updateRangeUI(seg.in_sec, seg.out_sec, totalDur);
  updateMeta(seg.in_sec, seg.out_sec, seg.purpose);
  document.getElementById('seg-video').currentTime = seg.in_sec;
  updateExportBar();
  showToast('Segment #' + seg.index + ' reset to original');
}}

function resetAllChanges() {{
  changes = {{}};
  updateExportBar();
  showToast('All changes reset');
}}

function updateExportBar() {{
  const count = Object.keys(changes).length;
  const bar = document.getElementById('export-bar');
  document.getElementById('changes-count').textContent = count;
  bar.style.display = count > 0 ? 'flex' : 'none';
}}

function exportJSON() {{
  // Deep clone storyboard and apply changes
  const modified = JSON.parse(JSON.stringify(storyboard));
  for (const seg of modified.segments) {{
    if (changes[seg.index]) {{
      seg.in_sec = changes[seg.index].in_sec;
      seg.out_sec = changes[seg.index].out_sec;
    }}
  }}
  // Download as JSON
  const blob = new Blob([JSON.stringify(modified, null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'editorial_adjusted.json';
  a.click();
  URL.revokeObjectURL(url);
  showToast('Exported adjusted JSON');
}}

// --- Transcript in segment modal ---
function renderSegTranscript(clipId, inSec, outSec) {{
  const panel = document.getElementById('seg-transcript-panel');
  const list = document.getElementById('seg-transcript-list');
  list.innerHTML = '';
  const segs = clipTranscripts[clipId];
  if (!segs || segs.length === 0) {{ panel.style.display = 'none'; return; }}

  // Filter transcript segments that overlap with this edit segment
  const relevant = segs.filter(s => s.end > inSec && s.start < outSec);
  if (relevant.length === 0) {{ panel.style.display = 'none'; return; }}

  relevant.forEach((s, i) => {{
    const div = document.createElement('div');
    div.className = 't-line';
    div.dataset.tStart = s.start;
    div.dataset.tEnd = s.end;
    const m = Math.floor(s.start / 60);
    const sec = Math.floor(s.start % 60);
    const ts = '<span class="t-time">' + m + ':' + String(sec).padStart(2,'0') + '</span>';

    if (s.type === 'music') {{
      div.innerHTML = ts + '<span class="t-music">\\u266a ' + (s.text || 'Music') + ' \\u266a</span>';
    }} else if (s.type === 'sound_effect') {{
      div.innerHTML = ts + '<span class="t-sfx">[' + (s.text || 'sound') + ']</span>';
    }} else if (s.type === 'silence') {{
      return;
    }} else {{
      const spk = s.speaker ? '<span class="t-speaker">' + s.speaker + ':</span> ' : '';
      div.innerHTML = ts + spk + s.text;
    }}

    div.onclick = () => {{
      document.getElementById('seg-video').currentTime = s.start;
      document.getElementById('seg-video').play();
    }};
    list.appendChild(div);
  }});

  panel.style.display = 'block';
}}

// Highlight active transcript line during playback
function updateTranscriptHighlight(currentTime) {{
  document.querySelectorAll('.t-line').forEach(el => {{
    const s = parseFloat(el.dataset.tStart);
    const e = parseFloat(el.dataset.tEnd);
    const active = currentTime >= s && currentTime < e;
    el.classList.toggle('t-active', active);
    if (active) el.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
  }});
}}

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') closeModal();
  if (e.key === ' ' && currentSegIndex !== null) {{
    e.preventDefault();
    const v = document.getElementById('seg-video');
    v.paused ? v.play() : v.pause();
  }}
}});
</script>

</body>
</html>"""
