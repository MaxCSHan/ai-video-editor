"""Interactive TUI mode — guided video production workflow."""

import json
import os
import subprocess
from pathlib import Path

import questionary
from questionary import Style

from .config import DEFAULT_CONFIG

VX_STYLE = Style(
    [
        ("qmark", "fg:#2ecc71 bold"),
        ("question", "fg:#ffffff bold"),
        ("answer", "fg:#2ecc71"),
        ("pointer", "fg:#2ecc71 bold"),
        ("highlighted", "fg:#2ecc71 bold"),
        ("selected", "fg:#2ecc71"),
        ("instruction", "fg:#666666"),
        ("text", "fg:#aaaaaa"),
    ]
)

BANNER = """
\033[1m  VX — AI Video Editor\033[0m
\033[2m  Turn raw footage into polished vlogs with AI\033[0m
"""

# ANSI codes
_RED = "\033[31m"
_GREEN = "\033[32m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

# Pipeline node definitions — semantic names (not Phase 1/2/3)
PIPELINE_NODES = ["scan", "brief", "speech", "review", "story", "mono"]
NODE_LABELS = {
    "scan": "Scan",
    "brief": "Brief",
    "speech": "Speech",
    "review": "Review",
    "story": "Story",
    "mono": "Mono",
}
NODE_FULL_NAMES = {
    "scan": "Quick Scan",
    "brief": "Briefing Context",
    "speech": "Transcription",
    "review": "Clip Reviews",
    "story": "Storyboard",
    "mono": "Monologue",
}
# Map TUI node names to internal phase names used by versioning
NODE_TO_PHASE = {
    "scan": "quick_scan",
    "brief": "user_context",
    "speech": "transcript",
    "review": "review",
    "story": "storyboard",
    "mono": "monologue",
}


# ---------------------------------------------------------------------------
# Pipeline state gathering
# ---------------------------------------------------------------------------


def _gather_pipeline_state(ep, meta) -> dict:
    """Collect version state for all pipeline nodes."""
    from .versioning import (
        list_artifacts,
        resolve_quick_scan_path,
        resolve_user_context_path,
        resolve_transcript_path,
    )
    import re

    provider = meta.get("provider", "gemini")
    state = {}

    # --- Scan ---
    scan_path = resolve_quick_scan_path(ep.root)
    scan_versions = []
    for f in sorted(ep.root.glob("quick_scan_v*.json")):
        if f.name.endswith(".meta.json") or f.is_symlink():
            continue
        m = re.search(r"_v(\d+)\.json$", f.name)
        if m:
            scan_versions.append(int(m.group(1)))
    state["scan"] = {
        "exists": scan_path is not None,
        "versions": scan_versions,
        "provider": "gemini",
        "date": _file_date(scan_path) if scan_path else "",
    }

    # --- User Context ---
    ctx_path = resolve_user_context_path(ep.root)
    ctx_versions = []
    for f in sorted(ep.root.glob("user_context_v*.json")):
        if f.name.endswith(".meta.json") or f.is_symlink():
            continue
        m = re.search(r"_v(\d+)\.json$", f.name)
        if m:
            ctx_versions.append(int(m.group(1)))
    # Fallback: bare file counts as v1
    if not ctx_versions and ctx_path and "user_context.json" == ctx_path.name:
        ctx_versions = [1]
    state["brief"] = {
        "exists": ctx_path is not None,
        "versions": ctx_versions,
        "date": _file_date(ctx_path) if ctx_path else "",
    }

    # --- Transcript (per-clip aggregate) ---
    clips = ep.discover_clips()
    t_count = 0
    t_provider = ""
    for cid in clips:
        cp = ep.clip_paths(cid)
        tp = resolve_transcript_path(cp.root)
        if tp:
            t_count += 1
            if not t_provider:
                try:
                    data = json.loads(tp.read_text())
                    t_provider = data.get("provider", "mlx") or "mlx"
                except Exception:
                    t_provider = "?"
    state["speech"] = {
        "exists": t_count > 0,
        "provider": t_provider,
        "clip_count": t_count,
        "total_clips": len(clips),
    }

    # --- P1 Reviews (per-clip aggregate) ---
    r_count = 0
    r_version = 0
    for cid in clips:
        cp = ep.clip_paths(cid)
        if cp.has_review(provider):
            r_count += 1
            # Get max version from latest review file
            for f in cp.review.glob(f"review_{provider}_v*.json"):
                m = re.search(r"_v(\d+)\.", f.name)
                if m:
                    r_version = max(r_version, int(m.group(1)))
    state["review"] = {
        "exists": r_count > 0,
        "provider": provider,
        "clip_count": r_count,
        "total_clips": len(clips),
        "version": r_version if r_version > 0 else 1,
    }

    # --- P2 Storyboard ---
    sb_artifacts = list_artifacts(ep.root, phase="storyboard")
    sb_versions = [a.version for a in sb_artifacts if a.status == "complete"]
    sb_detail = ""
    if sb_versions:
        latest_sb = ep.storyboard / f"editorial_{provider}_latest.json"
        if not latest_sb.exists():
            # Try any provider
            for p in ("gemini", "claude"):
                latest_sb = ep.storyboard / f"editorial_{p}_latest.json"
                if latest_sb.exists():
                    break
        if latest_sb.exists():
            try:
                from .models import EditorialStoryboard
                from .storyboard_format import format_duration

                sb = EditorialStoryboard.model_validate_json(latest_sb.read_text())
                sb_detail = f"{len(sb.segments)} seg, {format_duration(sb.total_segments_duration)}"
            except Exception:
                pass
    state["story"] = {
        "exists": len(sb_versions) > 0,
        "versions": sb_versions,
        "provider": provider,
        "detail": sb_detail,
        "date": _file_date(sb_artifacts[-1]) if sb_artifacts else "",
    }

    # --- P3 Monologue ---
    mono_artifacts = list_artifacts(ep.root, phase="monologue")
    mono_versions = [a.version for a in mono_artifacts if a.status == "complete"]
    mono_detail = ""
    if mono_versions:
        latest_mono = ep.storyboard / f"monologue_{provider}_latest.json"
        if latest_mono.exists():
            try:
                from .models import MonologuePlan

                mp = MonologuePlan.model_validate_json(latest_mono.read_text())
                mono_detail = f"{len(mp.overlays)} overlays"
            except Exception:
                pass
    state["mono"] = {
        "exists": len(mono_versions) > 0,
        "versions": mono_versions,
        "detail": mono_detail,
    }

    # --- Cuts ---
    cuts = []
    cuts_dir = ep.exports / "cuts"
    if cuts_dir.exists():
        for d in sorted(cuts_dir.iterdir()):
            if d.is_dir() and d.name.startswith("cut_"):
                comp_file = d / "composition.json"
                ref = ""
                if comp_file.exists():
                    try:
                        comp = json.loads(comp_file.read_text())
                        sb_ref = comp.get("storyboard", {}).get("artifact_id", "")
                        mono_ref = comp.get("monologue", {})
                        mono_str = ""
                        if mono_ref:
                            mono_str = f"+{mono_ref.get('artifact_id', '')}"
                        ref = f"{sb_ref}{mono_str}"
                    except Exception:
                        pass
                cuts.append({"cut_id": d.name, "ref": ref})
    state["cuts"] = cuts

    return state


def _file_date(path_or_artifact) -> str:
    """Extract a short date string from a file mtime or ArtifactMeta."""
    try:
        if hasattr(path_or_artifact, "created_at"):
            from datetime import datetime

            dt = datetime.fromisoformat(path_or_artifact.created_at)
            return dt.strftime("%b %d")
        if hasattr(path_or_artifact, "stat"):
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(path_or_artifact.stat().st_mtime, tz=timezone.utc)
            return dt.strftime("%b %d")
    except Exception:
        pass
    return ""


def _compact_version_chain(versions: list[int]) -> str:
    """Build 'v1 > v2 > v3' from a list of version numbers."""
    if not versions:
        return "--"
    return " > ".join(f"v{v}" for v in sorted(versions))


def _get_node_version_text(state: dict, node: str) -> str:
    """Get the version display text for a pipeline node tab."""
    s = state.get(node, {})
    if not s.get("exists"):
        return "--"

    if node in ("scan", "brief"):
        versions = s.get("versions", [])
        if versions:
            return f".{max(versions)}"
        return ".1"

    if node == "speech":
        prov = s.get("provider", "?")
        return f"{prov}.1"

    if node == "review":
        prov = s.get("provider", "?")
        return f"{prov}.{s.get('version', 1)}"

    if node in ("story", "mono"):
        versions = s.get("versions", [])
        if versions:
            return f".{max(versions)}"
        return ".1"

    return "--"


# ---------------------------------------------------------------------------
# Tab bar rendering
# ---------------------------------------------------------------------------


def _render_tab_bar(state: dict, active_node: str):
    """Render the pipeline tab bar with box-drawing characters."""
    tabs = []
    for node in PIPELINE_NODES:
        label = NODE_LABELS[node]
        version_text = _get_node_version_text(state, node)
        is_active = node == active_node
        exists = state.get(node, {}).get("exists", False)

        width = max(len(label), len(version_text)) + 2

        if is_active:
            top = f"╭{'─' * width}╮"
            mid1 = f"│{_GREEN}{label:^{width}}{_RESET}│"
            mid2 = f"│{_GREEN}{version_text:^{width}}{_RESET}│"
            bot = f"╰{'─' * width}╯"
        elif exists:
            top = f"┌{'─' * width}┐"
            mid1 = f"│{label:^{width}}│"
            mid2 = f"│{version_text:^{width}}│"
            bot = f"└{'─' * width}┘"
        else:
            top = f"┌{'─' * width}┐"
            mid1 = f"│{_DIM}{label:^{width}}{_RESET}│"
            mid2 = f"│{_DIM}{version_text:^{width}}{_RESET}│"
            bot = f"└{'─' * width}┘"

        tabs.append((top, mid1, mid2, bot))

    # Print rows
    for row in range(4):
        print(" " + " ".join(t[row] for t in tabs))

    # Cuts line
    cuts = state.get("cuts", [])
    if cuts:
        cut_parts = []
        for c in cuts:
            ref = c.get("ref", "")
            ref_str = f" ({ref})" if ref else ""
            cut_parts.append(f"{c['cut_id']}{ref_str}")
        print(f" {_DIM}Cuts: {', '.join(cut_parts)}{_RESET}")


def _render_node_detail(state: dict, active_node: str):
    """Render detail line for the active node."""
    s = state.get(active_node, {})
    full_name = NODE_FULL_NAMES.get(active_node, active_node)

    if not s.get("exists"):
        print(f"\n {_DIM}{full_name} — not started{_RESET}")
        return

    if active_node == "scan":
        date = s.get("date", "")
        versions = s.get("versions", [1])
        print(f"\n {_BOLD}{full_name}{_RESET} ({s.get('provider', 'gemini')}, {date})")
        if len(versions) > 1:
            print(f"   {_compact_version_chain(versions)}")

    elif active_node == "brief":
        date = s.get("date", "")
        versions = s.get("versions", [1])
        chain = _compact_version_chain(versions)
        print(f"\n {_BOLD}{full_name}{_RESET} ({chain}, {date})")

    elif active_node == "speech":
        prov = s.get("provider", "?")
        count = s.get("clip_count", 0)
        total = s.get("total_clips", 0)
        print(f"\n {_BOLD}{full_name}{_RESET} ({prov}, {count}/{total} clips)")

    elif active_node == "review":
        prov = s.get("provider", "?")
        count = s.get("clip_count", 0)
        total = s.get("total_clips", 0)
        v = s.get("version", 1)
        print(f"\n {_BOLD}{full_name}{_RESET} (rv.{v}, {prov}, {count}/{total} clips)")

    elif active_node == "story":
        versions = s.get("versions", [])
        chain = _compact_version_chain(versions)
        detail = s.get("detail", "")
        detail_str = f" — {detail}" if detail else ""
        print(f"\n {_BOLD}{full_name}{_RESET} ({chain}){detail_str}")

    elif active_node == "mono":
        versions = s.get("versions", [])
        chain = _compact_version_chain(versions)
        detail = s.get("detail", "")
        detail_str = f" — {detail}" if detail else ""
        print(f"\n {_BOLD}{full_name}{_RESET} ({chain}){detail_str}")


# ---------------------------------------------------------------------------
# Input confirmation for phase reruns
# ---------------------------------------------------------------------------


def _render_lineage_tree(ep, meta):
    """Render the full lineage tree showing pipeline flow with version branches.

    Groups artifacts by pipeline phase in DAG order, then shows versions
    within each phase with their upstream lineage references. Works with
    both new (parent_id) and legacy (inputs dict) artifacts.
    """
    from .versioning import list_artifacts, resolve_quick_scan_path, resolve_user_context_path

    # Gather all artifacts + non-artifact nodes
    artifacts = list_artifacts(ep.root, include_failed=False)

    # Phase ordering for the DAG
    phase_order = [
        "quick_scan",
        "user_context",
        "transcript",
        "review",
        "storyboard",
        "monologue",
    ]
    phase_labels = {
        "quick_scan": "Scan",
        "user_context": "Brief",
        "transcript": "Speech",
        "review": "Review",
        "storyboard": "Story",
        "monologue": "Mono",
    }

    # Group artifacts by phase
    by_phase = {}
    for art in artifacts:
        by_phase.setdefault(art.phase, []).append(art)

    # Also check for non-artifact nodes (bare files from before versioning)
    scan_path = resolve_quick_scan_path(ep.root)
    ctx_path = resolve_user_context_path(ep.root)

    print(f"\n  {_BOLD}Lineage Tree:{_RESET}")

    # Render each phase level
    has_content = False
    for phase in phase_order:
        label = phase_labels.get(phase, phase)
        arts = by_phase.get(phase, [])

        # For phases with no artifacts, check for bare files
        if not arts:
            if phase == "quick_scan" and scan_path:
                print(f"  {label} ─── {_DIM}{scan_path.name}{_RESET}")
                has_content = True
            elif phase == "user_context" and ctx_path:
                print(f"  {label} ─── {_DIM}{ctx_path.name}{_RESET}")
                has_content = True
            elif phase == "transcript":
                clips = ep.discover_clips()
                t_count = sum(1 for c in clips if ep.clip_paths(c).has_transcript())
                if t_count > 0:
                    print(f"  {label} ─── {_DIM}{t_count} clips transcribed{_RESET}")
                    has_content = True
            elif phase == "review":
                clips = ep.discover_clips()
                provider = meta.get("provider", "gemini")
                r_count = sum(1 for c in clips if ep.clip_paths(c).has_review(provider))
                if r_count > 0:
                    print(f"  {label} ─── {_DIM}{r_count} clips reviewed{_RESET}")
                    has_content = True
            continue

        has_content = True
        # Sort by version
        arts_sorted = sorted(arts, key=lambda a: a.version)

        if len(arts_sorted) == 1:
            art = arts_sorted[0]
            lineage = _format_lineage_ref(art)
            print(f"  {label} ─── {art.artifact_id}{lineage}")
        else:
            print(f"  {label}")
            for i, art in enumerate(arts_sorted):
                is_last = i == len(arts_sorted) - 1
                connector = "└─" if is_last else "├─"
                lineage = _format_lineage_ref(art)
                print(f"    {connector} {art.artifact_id}{lineage}")

    if not has_content:
        print(f"  {_DIM}No pipeline data yet. Run the pipeline to build lineage.{_RESET}")
        return

    # Show cuts
    cuts_dir = ep.exports / "cuts"
    if cuts_dir.exists():
        cut_dirs = sorted(d for d in cuts_dir.iterdir() if d.is_dir() and d.name.startswith("cut_"))
        if cut_dirs:
            print(f"\n  {_BOLD}Cuts:{_RESET}")
            for d in cut_dirs:
                comp = d / "composition.json"
                ref = ""
                if comp.exists():
                    data = json.loads(comp.read_text())
                    sb = data.get("storyboard", {}).get("artifact_id", "?")
                    mn = data.get("monologue", {})
                    mn_str = f" + {mn.get('artifact_id', '')}" if mn else ""
                    ref = f" ← {sb}{mn_str}"
                print(f"    {d.name}{ref}")


def _format_lineage_ref(art) -> str:
    """Format the upstream lineage reference for display."""
    # Try parent_id first (new format)
    if art.parent_id:
        return f"  {_DIM}← {art.parent_id}{_RESET}"
    # Fall back to inputs dict (legacy)
    if art.inputs:
        refs = []
        for key, val in art.inputs.items():
            if val and key not in ("monologue",):  # skip self-references
                refs.append(str(val))
        if refs:
            return f"  {_DIM}← {', '.join(refs[:3])}{_RESET}"
    return ""


def _confirm_phase_inputs(ep, meta, phase: str) -> dict | None:
    """Show inputs for a phase rerun and get confirmation. Returns lineage dict or None."""
    from .versioning import (
        resolve_user_context_path,
        current_version,
    )
    import re

    provider = meta.get("provider", "gemini")
    phase_labels = {
        "review": "Clip Reviews",
        "storyboard": "Storyboard",
        "monologue": "Monologue",
        "transcript": "Transcription",
    }
    inputs = []  # (label, value) for display
    lineage = {}

    # User context — used by P1, P2, P3, transcript
    ctx_path = resolve_user_context_path(ep.root)
    ctx_label = "--"
    if ctx_path:
        m = re.search(r"_v(\d+)\.", ctx_path.name)
        if m:
            ctx_label = f"ctx:v{m.group(1)} ({_file_date(ctx_path)})"
            lineage["user_context"] = f"user_context:user:v{m.group(1)}"
        else:
            ctx_label = f"{ctx_path.name} ({_file_date(ctx_path)})"

    clips = ep.discover_clips()

    if phase == "review":
        inputs.append(("Clips", f"{len(clips)} clips"))
        inputs.append(("Briefing", ctx_label))
        t_count = sum(1 for c in clips if ep.clip_paths(c).has_transcript())
        inputs.append(("Transcripts", f"{t_count} clips"))
        inputs.append(("Provider", provider))
        preset = meta.get("style_preset", "")
        if preset:
            inputs.append(("Preset", preset))
        next_v = current_version(ep.root, f"review_{provider}") + 1
        inputs.append(("Will create", f"P1:v{next_v} reviews"))

    elif phase == "storyboard":
        r_count = sum(1 for c in clips if ep.clip_paths(c).has_review(provider))
        inputs.append(("Reviews", f"{r_count} clips, {provider}"))
        inputs.append(("Briefing", ctx_label))
        t_count = sum(1 for c in clips if ep.clip_paths(c).has_transcript())
        inputs.append(("Transcripts", f"{t_count} clips"))
        inputs.append(("Style", meta.get("style", "vlog")))
        preset = meta.get("style_preset", "")
        if preset:
            inputs.append(("Preset", preset))
        next_v = current_version(ep.root, "analyze") + 1
        inputs.append(("Will create", f"storyboard v{next_v}"))

    elif phase == "monologue":
        sb_v = current_version(ep.root, "analyze")
        inputs.append(("Storyboard", f"v{sb_v}"))
        inputs.append(("Briefing", ctx_label))
        preset = meta.get("style_preset", "")
        if preset:
            inputs.append(("Preset", preset))
        next_v = current_version(ep.root, "monologue") + 1
        inputs.append(("Will create", f"monologue v{next_v}"))

    elif phase == "transcript":
        inputs.append(("Clips", f"{len(clips)} clips"))
        inputs.append(("Briefing", ctx_label))
        inputs.append(("Provider", provider))

    print(f"\n  Rerun {phase_labels.get(phase, phase)}\n")
    if inputs:
        max_label = max(len(lab) for lab, _ in inputs)
        for label, val in inputs:
            print(f"    {label + ':':>{max_label + 1}}  {val}")
    print()

    if not questionary.confirm("Proceed?", default=True, style=VX_STYLE).ask():
        return None
    return lineage


def _build_node_actions(active_node, state, ep, meta, offline) -> list:
    """Build questionary choices for the active node + global actions."""
    from questionary import Separator

    choices = []
    node_exists = state.get(active_node, {}).get("exists", False)
    has_storyboard = state.get("story", {}).get("exists", False)
    has_rough_cut = len(state.get("cuts", [])) > 0
    has_preview = any(ep.exports.glob("*/preview.html")) if ep.exports.exists() else False
    preset_key = meta.get("style_preset")
    has_phase3 = False
    if preset_key:
        try:
            from .style_presets import get_preset as _gp

            _sp = _gp(preset_key)
            has_phase3 = _sp.has_phase3 if _sp else False
        except Exception:
            pass

    # --- Node-specific actions ---
    if active_node == "scan":
        if not offline:
            choices.append(questionary.Choice("Re-scan footage", value="rescan"))
        if node_exists:
            choices.append(questionary.Choice("View scan results", value="view_scan"))

    elif active_node == "brief":
        choices.append(questionary.Choice("Edit briefing (AI-guided)", value="edit_brief_ai"))
        choices.append(questionary.Choice("Edit briefing (manual)", value="edit_brief_manual"))

    elif active_node == "speech":
        choices.append(questionary.Choice("Transcribe audio", value="transcribe"))

    elif active_node == "review":
        choices.append(questionary.Choice("Rerun clip reviews", value="rerun_p1"))

    elif active_node == "story":
        choices.append(questionary.Choice("Rerun storyboard", value="rerun_p2"))
        if node_exists:
            choices.append(
                questionary.Choice("Review storyboard (Director)", value="review_storyboard")
            )
            choices.append(questionary.Choice("Chat with Director", value="chat_director"))
            choices.append(questionary.Choice("Compare storyboard versions", value="compare"))
        if has_preview:
            choices.append(questionary.Choice("Open preview", value="open_preview"))

    elif active_node == "mono":
        if has_phase3:
            choices.append(questionary.Choice("Rerun monologue", value="rerun_p3"))
        elif not has_phase3:
            choices.append(
                questionary.Choice(
                    "Set style preset (monologue requires preset)", value="set_preset"
                )
            )

    # --- Version switching (when multiple versions exist) ---
    node_state = state.get(active_node, {})
    versions = node_state.get("versions", [])
    if len(versions) > 1 and active_node in ("story", "mono", "scan", "brief"):
        choices.append(questionary.Choice("Switch version...", value="switch_version"))

    # --- Navigation ---
    node_idx = PIPELINE_NODES.index(active_node)
    if node_idx < len(PIPELINE_NODES) - 1:
        next_node = PIPELINE_NODES[node_idx + 1]
        choices.append(
            questionary.Choice(
                f"→ Switch to {NODE_FULL_NAMES[next_node]}", value=f"nav_{next_node}"
            )
        )
    if node_idx > 0:
        prev_node = PIPELINE_NODES[node_idx - 1]
        choices.append(
            questionary.Choice(
                f"← Switch to {NODE_FULL_NAMES[prev_node]}", value=f"nav_{prev_node}"
            )
        )

    # --- Assembly (global) ---
    if has_storyboard:
        choices.append(Separator("── Assembly ──"))
        choices.append(questionary.Choice("Compose a cut", value="compose_cut"))
        if not offline:
            choices.append(questionary.Choice("Assemble rough cut", value="assemble"))
        else:
            choices.append(questionary.Choice("Assemble rough cut (proxy)", value="assemble_proxy"))
        choices.append(
            questionary.Choice("Export to DaVinci Resolve (.fcpxml)", value="export_xml")
        )

    # --- Project (global) ---
    choices.append(Separator("── Project ──"))
    if has_rough_cut:
        choices.append(questionary.Choice("Open rough cut video", value="open_cut"))
    if has_storyboard:
        choices.append(questionary.Choice("Regenerate preview", value="regen_preview"))
    choices.append(questionary.Choice("Run full pipeline", value="run_full"))
    if not offline:
        choices.append(questionary.Choice("Manage clips", value="manage_clips"))
    choices.append(questionary.Choice("View lineage tree", value="lineage_tree"))
    choices.append(questionary.Choice("Version history", value="version_history"))
    choices.append(questionary.Choice("Show status", value="show_status"))
    choices.append(questionary.Choice("Set style preset", value="set_preset"))
    choices.append(questionary.Choice("← Back", value="back"))

    return choices


def run_interactive():
    """Main interactive loop."""
    from dotenv import load_dotenv

    load_dotenv()

    print(BANNER)
    cfg = DEFAULT_CONFIG

    # Auto-connect to Phoenix tracing server if running
    from .tracing import connect_phoenix, get_phoenix_status

    if connect_phoenix():
        _, trace_url = get_phoenix_status()
        print(f"  \033[2mTracing: connected ({trace_url})\033[0m\n")

    while True:
        action = questionary.select(
            "What would you like to do?",
            choices=[
                "New project",
                "Open existing project",
                "Settings",
                questionary.Choice("Quit", value="quit"),
            ],
            style=VX_STYLE,
        ).ask()

        if action is None or action == "quit":
            print("\n  Bye!\n")
            break
        elif action == "New project":
            _new_project_flow(cfg)
        elif action == "Open existing project":
            _open_project_flow(cfg)
        elif action == "Settings":
            _settings_flow(cfg)


def _new_project_flow(cfg):
    """Guided flow: create project → preprocess → brief → analyze."""
    print()
    name = questionary.text(
        "Project name:",
        instruction="(e.g., family-trip-hsinchu, puma-run)",
        style=VX_STYLE,
    ).ask()
    if not name:
        return

    source = questionary.path(
        "Footage folder (directory containing your raw video clips):",
        only_directories=True,
        style=VX_STYLE,
    ).ask()
    if not source:
        return
    source_path = Path(source.strip().strip("'\"")).expanduser().resolve()

    if not source_path.is_dir():
        print(f"\n  Error: {source_path} is not a directory\n")
        return

    style = questionary.select(
        "Video style:",
        choices=[
            "vlog",
            "travel-vlog",
            "family-video",
            "event-recap",
            "cinematic",
            "short-form",
            questionary.Choice("Custom...", value="__custom__"),
        ],
        style=VX_STYLE,
    ).ask()
    if style == "__custom__":
        style = questionary.text(
            "Describe your video style:",
            instruction="(e.g., 'lo-fi daily journal', 'drone landscape showcase')",
            style=VX_STYLE,
        ).ask()
    if not style:
        return

    # Style preset selection (optional creative direction)
    from .style_presets import list_presets, get_preset

    presets = list_presets()
    preset_choices = [questionary.Choice("None (standard editing)", value=None)]
    for p in presets:
        preset_choices.append(questionary.Choice(f"{p.label} — {p.description}", value=p.key))

    preset_key = questionary.select(
        "Style preset (optional — adds AI creative direction):",
        choices=preset_choices,
        style=VX_STYLE,
    ).ask()

    style_preset = get_preset(preset_key) if preset_key else None
    if style_preset:
        print(f"\n  Preset: {style_preset.label}")
        if style_preset.has_phase3:
            print("  This preset will generate a Visual Monologue (text overlay narrative)")

    # Read workspace config for provider
    ws_path = Path(".vx.json")
    ws = json.loads(ws_path.read_text()) if ws_path.exists() else {}
    provider = ws.get("provider", "gemini")

    # Check API key
    key_var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_var):
        print(f"\n  Warning: {key_var} not set. Check your .env file.\n")
        if not questionary.confirm("Continue anyway?", default=False, style=VX_STYLE).ask():
            return

    print(f"\n  Creating project: {name}")
    print(f"  Source: {source_path}")
    print(f"  Style: {style}, Provider: {provider}")
    if style_preset:
        print(f"  Preset: {style_preset.label}")
    print()

    from .editorial_agent import (
        discover_source_clips,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
        _retry_failed_phase1,
    )

    ep = cfg.editorial_project(name)
    ep.ensure_dirs()

    # Save project metadata
    from datetime import datetime, timezone

    meta = {
        "name": name,
        "type": "editorial",
        "provider": provider,
        "style": style,
        "source_dir": str(source_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if preset_key:
        meta["style_preset"] = preset_key
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Discover
    clips = discover_source_clips(source_path)
    if not clips:
        print(f"  No video files found in {source_path}\n")
        return

    print(f"  Found {len(clips)} clips\n")

    # Let user deselect clips they don't want
    selected = questionary.checkbox(
        "Select clips to include:",
        choices=[questionary.Choice(c.name, value=c, checked=True) for c in clips],
        style=VX_STYLE,
    ).ask()
    if selected is None:
        return
    if not selected:
        print("  No clips selected.\n")
        return
    clips = selected

    meta["clip_count"] = len(clips)
    meta["included_clips"] = [c.stem for c in clips]
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    # Preprocess
    print(f"  Preprocessing {len(clips)} clips...\n")
    clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
    manifest = build_master_manifest(clip_metadata, ep, name)
    print(f"\n  Total footage: {manifest['total_duration_fmt']}")

    # Format analysis + selection
    clip_metadata, output_format = _run_format_selection(clip_metadata, meta, ep)

    # Rebuild manifest if clips were filtered (Live Photos excluded)
    if len(clip_metadata) != manifest["clip_count"]:
        manifest = build_master_manifest(clip_metadata, ep, name)

    from .tracing import ProjectTracer

    tracer = ProjectTracer(ep.root)

    # Resolve style supplements from preset
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None

    use_smart_briefing = bool(os.environ.get("GEMINI_API_KEY"))

    # Show initial pipeline state
    _render_tab_bar(_gather_pipeline_state(ep, meta), "ctx")

    # Smart briefing BEFORE transcription (Gemini path) — uploads proxies and
    # populates the shared File API cache, plus gathers user context (speaker names,
    # highlights) that improves transcription and Phase 1 quality.
    user_context = None
    if use_smart_briefing:
        from .briefing import run_smart_briefing

        user_context = run_smart_briefing(
            ep, style, gemini_model=cfg.transcribe.gemini_model, tracer=tracer
        )

    # Transcription (benefits from cached Gemini URIs + speaker context from briefing)
    if not questionary.confirm("Run transcription?", default=True, style=VX_STYLE).ask():
        print("\n  Skipped transcription. Run from project menu later.\n")
    else:
        _run_transcription(ep, clip_metadata, cfg)

    # Show progress after transcription
    _pj = ep.root / "project.json"
    meta = json.loads(_pj.read_text()) if _pj.exists() else meta
    _render_tab_bar(_gather_pipeline_state(ep, meta), "P1")

    # Phase 1
    if not questionary.confirm("Run Phase 1 clip reviews?", default=True, style=VX_STYLE).ask():
        print("\n  Skipped. Run 'vx analyze' later.\n")
        return

    print(f"\n  Phase 1: Reviewing clips with {provider}...\n")
    if provider == "gemini":
        reviews, failed = run_phase1_gemini(
            ep,
            manifest,
            cfg.gemini,
            tracer=tracer,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    else:
        reviews, failed = run_phase1_claude(
            ep,
            manifest,
            cfg.claude,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    reviews, failed = _retry_failed_phase1(
        failed,
        reviews,
        ep,
        manifest,
        provider,
        cfg,
        tracer=tracer,
        style_supplement=p1_supplement,
        user_context=user_context,
    )
    print(f"\n  Reviewed {len(reviews)} clips")

    # Manual briefing AFTER Phase 1 (non-Gemini path) — needs Phase 1 reviews
    # to generate smart questions about detected people and highlights.
    if not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, ep.root)

    # Show progress after Phase 1
    _pj = ep.root / "project.json"
    meta = json.loads(_pj.read_text()) if _pj.exists() else meta
    _render_tab_bar(_gather_pipeline_state(ep, meta), "P2")

    # Phase 2
    if not questionary.confirm(
        "Generate editorial storyboard?", default=True, style=VX_STYLE
    ).ask():
        print("\n  Context saved. Run 'vx analyze' later.\n")
        return

    # Ask about visual mode
    visual = False
    if provider == "gemini":
        visual = _ask_visual_phase2(ep, reviews)

    print("\n  Phase 2: Generating storyboard...\n")
    run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
        tracer=tracer,
        visual=visual,
        style_supplement=p2_supplement,
        review_config=cfg.review,
        interactive=True,
    )

    # Phase 3 — Visual Monologue (if preset supports it)
    if style_preset and style_preset.has_phase3:
        if questionary.confirm(
            "Generate visual monologue (text overlay plan)?", default=True, style=VX_STYLE
        ).ask():
            from .editorial_agent import run_monologue

            print("\n  Phase 3: Visual Monologue")
            run_monologue(
                editorial_paths=ep,
                provider=provider,
                gemini_cfg=cfg.gemini,
                claude_cfg=cfg.claude,
                style_preset=style_preset,
                user_context=user_context,
                tracer=tracer,
            )

    tracer.print_summary("Pipeline Total")
    print("\n  Storyboard ready!")
    _project_actions(name, cfg)


def _open_project_flow(cfg):
    """Open an existing project and show actions."""
    if not cfg.library_dir.exists():
        print("\n  No projects yet.\n")
        return

    projects = sorted(
        d.name for d in cfg.library_dir.iterdir() if d.is_dir() and (d / "project.json").exists()
    )
    if not projects:
        print("\n  No projects yet.\n")
        return

    name = questionary.select(
        "Select project:",
        choices=projects + [questionary.Choice("← Back", value="")],
        style=VX_STYLE,
    ).ask()
    if not name:
        return

    _project_actions(name, cfg)


def _project_actions(name, cfg):
    """Show actions for an open project with pipeline tab bar navigation."""
    meta_path = cfg.library_dir / name / "project.json"
    meta = json.loads(meta_path.read_text())
    ep = cfg.editorial_project(name)

    # Determine initial active node (rightmost with data)
    active_node = "story"  # default
    active_versions = {node: None for node in PIPELINE_NODES}  # None = latest
    state = _gather_pipeline_state(ep, meta)
    for node in reversed(PIPELINE_NODES):
        if state.get(node, {}).get("exists"):
            active_node = node
            break

    while True:
        # Refresh state each loop
        meta = json.loads(meta_path.read_text())
        state = _gather_pipeline_state(ep, meta)
        source_dir = meta.get("source_dir", "")
        offline = bool(source_dir) and not Path(source_dir).is_dir()

        # Render tab bar + node detail
        offline_tag = f"  {_DIM}[OFFLINE]{_RESET}" if offline else ""
        print(f"\n  {_BOLD}Project: {name}{_RESET}{offline_tag}\n")
        _render_tab_bar(state, active_node)
        _render_node_detail(state, active_node)

        # Build node-scoped action menu
        choices = _build_node_actions(active_node, state, ep, meta, offline)

        action = questionary.select(
            "Action:",
            choices=choices,
            style=VX_STYLE,
        ).ask()

        if action is None or action == "back":
            break

        # --- Navigation ---
        elif action.startswith("nav_"):
            active_node = action[4:]

        # --- Version switching ---
        elif action == "switch_version":
            node_state = state.get(active_node, {})
            versions = node_state.get("versions", [])
            if versions:
                from .versioning import list_artifacts

                phase_name = NODE_TO_PHASE.get(active_node, active_node)
                arts = [
                    a for a in list_artifacts(ep.root, phase=phase_name) if a.status == "complete"
                ]
                if arts:
                    version_choices = []
                    for art in sorted(arts, key=lambda a: a.version):
                        # Plain text for questionary (no ANSI codes)
                        lineage = ""
                        if art.parent_id:
                            lineage = f" <- {art.parent_id}"
                        elif art.inputs:
                            refs = [v for v in art.inputs.values() if v][:2]
                            if refs:
                                lineage = f" <- {', '.join(refs)}"
                        label = f"v{art.version}{lineage}"
                        version_choices.append(questionary.Choice(label, value=art.version))
                    selected = questionary.select(
                        f"{NODE_FULL_NAMES[active_node]} versions:",
                        choices=version_choices,
                        style=VX_STYLE,
                    ).ask()
                    if selected is not None:
                        active_versions[active_node] = selected
                        print(f"\n  Switched {NODE_LABELS[active_node]} to v{selected}")

        # --- Node-specific actions ---
        elif action == "rescan":
            from .briefing import run_quick_scan

            print("\n  Re-scanning footage...")
            run_quick_scan(ep, meta.get("provider", "gemini"))

        elif action == "view_scan":
            from .versioning import resolve_quick_scan_path

            sp = resolve_quick_scan_path(ep.root)
            if sp:
                data = json.loads(sp.read_text())
                print(f"\n  {data.get('overall_summary', 'No summary')}")
                if data.get("people"):
                    print("  People:")
                    for p in data["people"]:
                        print(f"    - {p.get('description', '?')}")
                if data.get("activities"):
                    print(f"  Activities: {', '.join(data['activities'])}")

        elif action == "edit_brief_ai":
            style = meta.get("style", "vlog")
            from .briefing import run_smart_briefing

            run_smart_briefing(ep, style, gemini_model=cfg.transcribe.gemini_model)

        elif action == "edit_brief_manual":
            reviews = _load_reviews(ep)
            style = meta.get("style", "vlog")
            from .briefing import run_briefing

            run_briefing(reviews, style, ep.root)

        elif action == "transcribe":
            _run_transcription_interactive(name, cfg)

        elif action == "rerun_p1":
            lineage = _confirm_phase_inputs(ep, meta, "review")
            if lineage is not None:
                _run_analyze(name, meta, cfg, phase1_only=True)

        elif action == "rerun_p2":
            lineage = _confirm_phase_inputs(ep, meta, "storyboard")
            if lineage is not None:
                _run_analyze(name, meta, cfg, phase2_only=True)

        elif action == "review_storyboard":
            _run_director_review(ep, meta, cfg)

        elif action == "chat_director":
            _chat_with_director(ep, meta, cfg)

        elif action == "rerun_p3":
            lineage = _confirm_phase_inputs(ep, meta, "monologue")
            if lineage is not None:
                from .editorial_agent import run_monologue
                from .style_presets import get_preset as _get_preset2
                from .tracing import ProjectTracer

                _sp2 = _get_preset2(meta.get("style_preset", ""))
                if _sp2:
                    provider = meta.get("provider", "gemini")
                    tracer = ProjectTracer(ep.root)
                    print(f"\n  Phase 3: Visual Monologue ({_sp2.label})")
                    run_monologue(
                        editorial_paths=ep,
                        provider=provider,
                        gemini_cfg=cfg.gemini,
                        claude_cfg=cfg.claude,
                        style_preset=_sp2,
                        tracer=tracer,
                    )
                    tracer.print_summary("Monologue")

        elif action == "run_full":
            _run_analyze(name, meta, cfg)

        elif action == "compare":
            _compare_versions_flow(name, ep)

        elif action == "open_preview":
            latest_export = ep.exports / "latest"
            if latest_export.exists():
                preview = latest_export / "preview.html"
                if preview.exists():
                    subprocess.run(["open", str(preview)])

        elif action == "open_cut":
            _cd = ep.exports / "cuts"
            cuts = sorted(_cd.glob("*/rough_cut*.mp4"), reverse=True) if _cd.exists() else []
            if not cuts:
                cuts = sorted(ep.exports.glob("*/rough_cut*.mp4"), reverse=True)
            if cuts:
                subprocess.run(["open", str(cuts[0])])

        elif action == "compose_cut":
            _compose_cut_flow(name, ep)

        elif action in ("assemble", "assemble_proxy"):
            from .rough_cut import run_rough_cut
            from .versioning import resolve_versioned_path

            is_proxy = action == "assemble_proxy"
            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                sb_path = resolve_versioned_path(json_files[0])
                # Check for monologue
                monologue = None
                mono_files = sorted(ep.storyboard.glob("monologue_*_latest.json"))
                if mono_files:
                    from .models import MonologuePlan

                    use_mono = questionary.confirm(
                        "Include text overlays from monologue?", default=True, style=VX_STYLE
                    ).ask()
                    if use_mono:
                        monologue = MonologuePlan.model_validate_json(
                            resolve_versioned_path(mono_files[0]).read_text()
                        )
                print(f"\n  Assembling {'PROXY ' if is_proxy else ''}rough cut...\n")
                result = run_rough_cut(sb_path, ep, monologue=monologue, proxy_mode=is_proxy)
                print(f"\n  Done! {result.get('cut_id', '')}")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(result["preview"])])

        elif action == "export_xml":
            from .fcpxml_export import export_fcpxml, export_srt_files
            from .models import EditorialStoryboard, MonologuePlan
            from .versioning import resolve_versioned_path

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if not json_files:
                print(f"\n  {_DIM}No storyboard found. Run analysis first.{_RESET}\n")
                continue

            try:
                sb_path = resolve_versioned_path(json_files[0])
                sb = EditorialStoryboard.model_validate_json(sb_path.read_text())
                ep.exports.mkdir(parents=True, exist_ok=True)
                output_path = ep.exports / f"{name}.fcpxml"

                # Check for monologue overlays
                monologue = None
                mono_files = sorted(ep.storyboard.glob("monologue_*_latest.json"))
                if mono_files:
                    use_mono = questionary.confirm(
                        "Include text overlays from monologue?", default=True, style=VX_STYLE
                    ).ask()
                    if use_mono:
                        monologue = MonologuePlan.model_validate_json(
                            resolve_versioned_path(mono_files[0]).read_text()
                        )

                seg_count = len(sb.segments)
                clip_count = len(set(s.clip_id for s in sb.segments))
                print(f"\n  Exporting FCPXML from {sb_path.name}...")
                print(f"  ({seg_count} segments from {clip_count} clips)\n")
                if monologue:
                    print(f"  Including {len(monologue.overlays)} monologue overlays\n")

                result_path = export_fcpxml(
                    storyboard=sb,
                    editorial_paths=ep,
                    output_path=output_path,
                    project_name=name,
                    monologue=monologue,
                )

                print(f"  {_GREEN}\u2713 FCPXML exported:{_RESET} {result_path}")

                # Export SRT files (monologue + captions as separate tracks)
                srt_dir = ep.exports / "subtitles"
                srt_files = export_srt_files(sb, ep, srt_dir, monologue=monologue)
                mono_srt = next((f for f in srt_files if f.name == "timeline_monologue.srt"), None)
                caption_srt = next(
                    (f for f in srt_files if f.name == "timeline_subtitles.srt"), None
                )
                per_clip_count = sum(1 for f in srt_files if f.parent == srt_dir)
                if mono_srt:
                    print(f"  {_GREEN}\u2713 Monologue:{_RESET}    {mono_srt.name} (text overlays)")
                if caption_srt:
                    print(
                        f"  {_GREEN}\u2713 Subtitles:{_RESET}    {caption_srt.name} (speech captions)"
                    )
                if per_clip_count > 0:
                    print(f"                     + {per_clip_count} per-clip SRT files")

                print(f"\n  {_BOLD}Import into DaVinci Resolve:{_RESET}")
                print(
                    f"    Timeline:  File \u2192 Import \u2192 Timeline \u2192 {output_path.name}"
                )
                if mono_srt or caption_srt:
                    names = ", ".join(f.name for f in [mono_srt, caption_srt] if f)
                    print(f"    Subtitles: File \u2192 Import \u2192 Subtitle \u2192 {names}")
                    if mono_srt and caption_srt:
                        print("               (import as separate tracks for proper layering)")
                print()
            except Exception as exc:
                print(f"\n  {_RED}Export failed:{_RESET} {exc}\n")

        elif action == "regen_preview":
            from .models import EditorialStoryboard
            from .render import render_html_preview
            from .versioning import (
                begin_version,
                commit_version,
                versioned_dir,
                update_latest_symlink,
            )

            json_files = sorted(ep.storyboard.glob("editorial_*_latest.json"))
            if json_files:
                from .versioning import resolve_versioned_path

                sb = EditorialStoryboard.model_validate_json(
                    resolve_versioned_path(json_files[0]).read_text()
                )
                art_meta = begin_version(
                    ep.root, phase="preview", provider="render", target_dir=ep.exports
                )
                v = art_meta.version
                vdir = versioned_dir(ep.exports, v)
                rough_cut_path = None
                for _rc_dir in [ep.exports / "cuts" / "latest", ep.exports / "latest"]:
                    if _rc_dir.exists():
                        rc = _rc_dir / "rough_cut.mp4"
                        if rc.exists():
                            rough_cut_path = rc.resolve()
                            break
                html = render_html_preview(
                    sb, clips_dir=ep.clips_dir, output_dir=vdir, rough_cut_path=rough_cut_path
                )
                preview_path = vdir / "preview.html"
                preview_path.write_text(html)
                commit_version(
                    ep.root, art_meta, output_paths=[preview_path], target_dir=ep.exports
                )
                update_latest_symlink(vdir)
                print(f"\n  Preview v{v} generated")
                if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                    subprocess.run(["open", str(preview_path)])

        elif action == "manage_clips":
            _manage_clips(name, meta, cfg)

        elif action == "lineage_tree":
            _render_lineage_tree(ep, meta)
            questionary.press_any_key_to_continue(style=VX_STYLE).ask()

        elif action == "version_history":
            _version_history_flow(name, ep)

        elif action == "show_status":
            _show_status(name, meta, cfg)

        elif action == "set_preset":
            from .style_presets import list_presets as _list_presets, get_preset as _get_preset3

            current_preset = meta.get("style_preset")
            all_presets = _list_presets()
            preset_choices = [
                questionary.Choice(
                    "None (standard editing)" + (" (current)" if not current_preset else ""),
                    value=None,
                )
            ]
            for p in all_presets:
                label = f"{p.label} — {p.description}"
                if p.key == current_preset:
                    label += " (current)"
                preset_choices.append(questionary.Choice(label, value=p.key))

            new_key = questionary.select(
                "Style preset:", choices=preset_choices, style=VX_STYLE
            ).ask()
            if new_key != current_preset:
                sp = _get_preset3(new_key) if new_key else None
                if new_key and sp:
                    meta["style_preset"] = new_key
                    print(f"\n  Set preset: {sp.label}")
                    if sp.has_phase3:
                        print("  This preset supports Visual Monologue (Phase 3)")
                else:
                    meta.pop("style_preset", None)
                    print("\n  Removed style preset.")
                (ep.root / "project.json").write_text(json.dumps(meta, indent=2))


def _compose_cut_flow(name, ep):
    """Guided flow: pick storyboard + monologue → save composition → optionally assemble."""
    from datetime import datetime, timezone
    from .versioning import list_artifacts, save_composition, list_compositions
    from .models import Composition, EditorialStoryboard
    from .storyboard_format import format_duration

    storyboards = [
        a for a in list_artifacts(ep.root) if a.phase == "storyboard" and a.status == "complete"
    ]
    if not storyboards:
        print("\n  No storyboards found. Run analysis first.")
        return

    # Pick storyboard
    sb_choices = []
    for sb in storyboards:
        # Load summary info
        label = f"{sb.artifact_id}"
        try:
            sb_path = ep.storyboard / [f for f in sb.output_files if f.endswith(".json")][0]
            if sb_path.exists():
                data = EditorialStoryboard.model_validate_json(sb_path.read_text())
                dur = format_duration(data.total_segments_duration)
                label += f"  ({len(data.segments)} segments, {dur})"
        except Exception:
            pass
        sb_choices.append(questionary.Choice(label, value=sb))

    selected_sb = questionary.select(
        "Pick storyboard version:",
        choices=sb_choices,
        style=VX_STYLE,
    ).ask()
    if not selected_sb:
        return

    # Pick monologue (optional)
    monologues = [
        a for a in list_artifacts(ep.root) if a.phase == "monologue" and a.status == "complete"
    ]
    selected_mono = None
    if monologues:
        mono_choices = [questionary.Choice("None (no text overlays)", value=None)]
        for m in monologues:
            mono_choices.append(questionary.Choice(f"{m.artifact_id}  (v{m.version})", value=m))

        selected_mono = questionary.select(
            "Include monologue?",
            choices=mono_choices,
            style=VX_STYLE,
        ).ask()

    # Name the composition
    existing = list_compositions(ep.root)
    default_name = f"comp-{len(existing) + 1}"
    comp_name = questionary.text(
        "Composition name:",
        default=default_name,
        style=VX_STYLE,
    ).ask()
    if not comp_name:
        return

    comp = Composition(
        name=comp_name,
        storyboard=selected_sb.artifact_id,
        monologue=selected_mono.artifact_id if selected_mono else None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save_composition(ep.root, comp)
    mono_str = f" + {selected_mono.artifact_id}" if selected_mono else ""
    print(f"\n  Saved: {comp_name} = {selected_sb.artifact_id}{mono_str}")

    # Offer to assemble now
    if questionary.confirm("Assemble rough cut now?", default=True, style=VX_STYLE).ask():
        from .versioning import resolve_artifact_path

        sb_path = resolve_artifact_path(ep.root, comp.storyboard)
        if sb_path:
            from .rough_cut import run_rough_cut
            from .models import MonologuePlan

            monologue_obj = None
            if comp.monologue:
                mono_path = resolve_artifact_path(ep.root, comp.monologue)
                if mono_path:
                    monologue_obj = MonologuePlan.model_validate_json(mono_path.read_text())

            result = run_rough_cut(
                storyboard_json_path=sb_path,
                editorial_paths=ep,
                monologue=monologue_obj,
            )
            v = result.get("version", "?")
            print(f"\n  Rough cut v{v} assembled.")
            if questionary.confirm("Open preview?", default=True, style=VX_STYLE).ask():
                subprocess.run(["open", str(result["preview"])])


def _compare_versions_flow(name, ep):
    """Side-by-side comparison of two storyboard versions."""
    from .versioning import list_artifacts
    from .models import EditorialStoryboard
    from .storyboard_format import format_duration

    storyboards = [
        a for a in list_artifacts(ep.root) if a.phase == "storyboard" and a.status == "complete"
    ]
    if len(storyboards) < 2:
        print("\n  Need at least 2 storyboard versions to compare.")
        return

    sb_choices = [questionary.Choice(f"{sb.artifact_id}", value=sb) for sb in storyboards]

    sb_a = questionary.select("First version:", choices=sb_choices, style=VX_STYLE).ask()
    if not sb_a:
        return
    sb_b = questionary.select(
        "Second version:",
        choices=[c for c in sb_choices if c.value != sb_a],
        style=VX_STYLE,
    ).ask()
    if not sb_b:
        return

    # Load both storyboards
    try:
        path_a = ep.storyboard / [f for f in sb_a.output_files if f.endswith(".json")][0]
        path_b = ep.storyboard / [f for f in sb_b.output_files if f.endswith(".json")][0]
        data_a = EditorialStoryboard.model_validate_json(path_a.read_text())
        data_b = EditorialStoryboard.model_validate_json(path_b.read_text())
    except Exception as e:
        print(f"\n  Error loading storyboards: {e}")
        return

    print(f"\n  Comparing {sb_a.artifact_id} vs {sb_b.artifact_id}:")
    print(f"  {'':36s} {'v' + str(sb_a.version):>10s}  {'v' + str(sb_b.version):>10s}")
    print(f"  {'Segments':36s} {len(data_a.segments):>10d}  {len(data_b.segments):>10d}")
    print(
        f"  {'Duration':36s} {format_duration(data_a.total_segments_duration):>10s}  {format_duration(data_b.total_segments_duration):>10s}"
    )

    clips_a = {s.clip_id for s in data_a.segments}
    clips_b = {s.clip_id for s in data_b.segments}
    only_a = clips_a - clips_b
    only_b = clips_b - clips_a
    if only_a:
        print(f"  Only in v{sb_a.version}: {', '.join(sorted(only_a)[:5])}")
    if only_b:
        print(f"  Only in v{sb_b.version}: {', '.join(sorted(only_b)[:5])}")
    if not only_a and not only_b:
        print("  Same clips used in both versions")

    discarded_a = len(data_a.discarded)
    discarded_b = len(data_b.discarded)
    if discarded_a != discarded_b:
        print(f"  {'Discarded clips':36s} {discarded_a:>10d}  {discarded_b:>10d}")


def _version_history_flow(name, ep):
    """Show version history for all phases."""
    from datetime import datetime
    from .versioning import list_artifacts, list_compositions

    artifacts = list_artifacts(ep.root, include_failed=True)

    if not artifacts:
        from .versioning import all_versions

        versions = all_versions(ep.root)
        if versions:
            print("\n  Legacy versioning (no artifact metadata):")
            for phase, v in sorted(versions.items()):
                print(f"    {phase}: v{v}")
        else:
            print("\n  No versions found.")
        return

    phases = {}
    for art in artifacts:
        phases.setdefault(art.phase, []).append(art)

    phase_labels = {
        "storyboard": "Storyboards (Phase 2)",
        "monologue": "Monologues (Phase 3)",
        "cut": "Rough Cuts",
        "preview": "Previews",
    }

    for phase, arts in phases.items():
        label = phase_labels.get(phase, phase.title())
        print(f"\n  {label}:")
        for art in arts:
            status = {"complete": "OK", "failed": "FAIL", "pending": "..."}.get(art.status, "?")
            try:
                ts = datetime.fromisoformat(art.created_at)
                ts_str = ts.strftime("%m-%d %H:%M")
            except Exception:
                ts_str = ""

            lineage = ""
            if art.inputs:
                lineage_parts = [f"{v}" for v in art.inputs.values() if v]
                if lineage_parts:
                    lineage = f"  <- {', '.join(lineage_parts[:3])}"

            print(f"    {art.artifact_id}  [{status}]  {ts_str}{lineage}")

    comps = list_compositions(ep.root)
    if comps:
        print("\n  Compositions:")
        for c in comps:
            mono_part = f" + {c.monologue}" if c.monologue else ""
            print(f"    {c.name}: {c.storyboard}{mono_part}")


def _manage_clips(name, meta, cfg):
    """Add or remove clips from an existing project."""
    import shutil

    from .editorial_agent import discover_source_clips, preprocess_all_clips, build_master_manifest

    ep = cfg.editorial_project(name)
    source_dir = Path(meta.get("source_dir", ""))
    if not source_dir.is_dir():
        print(f"\n  Source directory not found: {source_dir}\n")
        return

    # All available clips from source directory
    all_source_clips = discover_source_clips(source_dir)
    if not all_source_clips:
        print(f"\n  No video files found in {source_dir}\n")
        return

    # Currently included clip IDs
    current_clip_ids = set(ep.discover_clips())

    # Build checkbox: checked if already in project, unchecked if not
    choices = []
    for clip_file in all_source_clips:
        clip_id = clip_file.stem
        is_included = clip_id in current_clip_ids
        choices.append(questionary.Choice(clip_file.name, value=clip_file, checked=is_included))

    selected = questionary.checkbox(
        f"Select clips to include ({len(current_clip_ids)} currently included):",
        choices=choices,
        style=VX_STYLE,
    ).ask()
    if selected is None:
        return

    selected_ids = {c.stem for c in selected}

    # Determine adds and removes
    to_add = [c for c in selected if c.stem not in current_clip_ids]
    to_remove = current_clip_ids - selected_ids

    if not to_add and not to_remove:
        print("\n  No changes.\n")
        return

    if to_remove:
        print(f"\n  Removing {len(to_remove)} clip(s):")
        for cid in sorted(to_remove):
            clip_dir = ep.clips_dir / cid
            if clip_dir.exists():
                shutil.rmtree(clip_dir)
                print(f"    - {cid}")

    # Read existing manifest to preserve full clip metadata
    existing_clips_by_id = {}
    if ep.master_manifest.exists():
        old_manifest = json.loads(ep.master_manifest.read_text())
        for c in old_manifest.get("clips", []):
            existing_clips_by_id[c["clip_id"]] = c

    new_clip_metadata = []
    if to_add:
        print(f"\n  Adding {len(to_add)} clip(s), preprocessing...\n")
        new_clip_metadata = preprocess_all_clips(to_add, ep, cfg.preprocess)
    new_clips_by_id = {c["clip_id"]: c for c in new_clip_metadata}

    # Rebuild manifest with current clips in source order
    remaining_clips = [c for c in all_source_clips if c.stem in selected_ids]
    merged_metadata = []
    for c in remaining_clips:
        cid = c.stem
        if cid in new_clips_by_id:
            merged_metadata.append(new_clips_by_id[cid])
        elif cid in existing_clips_by_id:
            merged_metadata.append(existing_clips_by_id[cid])
    build_master_manifest(merged_metadata, ep, name)

    # Update project metadata
    meta["clip_count"] = len(selected_ids)
    meta["included_clips"] = sorted(selected_ids)
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))

    print(f"\n  Project now has {len(selected_ids)} clips.")
    if to_remove:
        print("  Note: re-run analysis to update storyboard.\n")


def _run_analyze(name, meta, cfg, phase1_only=False, phase2_only=False):
    """Run the analysis pipeline. Can run full, Phase 1 only, or Phase 2 only."""
    from .editorial_agent import (
        discover_source_clips,
        discover_clips_from_manifest,
        preprocess_all_clips,
        build_master_manifest,
        run_phase1_gemini,
        run_phase1_claude,
        run_phase2,
        _retry_failed_phase1,
    )
    from .tracing import ProjectTracer

    ep = cfg.editorial_project(name)
    provider = meta.get("provider", "gemini")
    style = meta.get("style", "vlog")
    source_dir = Path(meta["source_dir"])
    offline = not source_dir.is_dir()
    tracer = ProjectTracer(ep.root)

    # Resolve style preset
    style_preset = None
    preset_key = meta.get("style_preset")
    if preset_key:
        from .style_presets import get_preset as _gp

        style_preset = _gp(preset_key)
    p1_supplement = style_preset.phase1_supplement if style_preset else None
    p2_supplement = style_preset.phase2_supplement if style_preset else None

    if style_preset:
        print(f"\n  Style preset: {style_preset.label}")

    if offline:
        # Offline mode: load clip list from manifest instead of scanning source dir
        print("\n  OFFLINE MODE: Source drive unavailable, skipping preprocessing.")
        print("  Using cached project data.\n")
        clip_metadata, manifest = discover_clips_from_manifest(ep)
        if not clip_metadata:
            print("  No manifest found — run analysis with source drive connected first.\n")
            return
        # Filter to included clips
        included = meta.get("included_clips")
        if included:
            included_set = set(included)
            clip_metadata = [c for c in clip_metadata if c["clip_id"] in included_set]
        print(f"  {len(clip_metadata)} clips (from cached manifest)\n")
    else:
        all_clips = discover_source_clips(source_dir)
        # Only process clips included in the project (respects manage-clips changes)
        included = meta.get("included_clips")
        if included:
            included_set = set(included)
            clips = [c for c in all_clips if c.stem in included_set]
        else:
            clips = all_clips
        print(f"\n  {len(clips)} clips, preprocessing...\n")
        clip_metadata = preprocess_all_clips(clips, ep, cfg.preprocess)
        manifest = build_master_manifest(clip_metadata, ep, name)

        # Format analysis + selection
        clip_metadata, output_format = _run_format_selection(clip_metadata, meta, ep)
        if len(clip_metadata) != manifest["clip_count"]:
            manifest = build_master_manifest(clip_metadata, ep, name)

    use_smart_briefing = bool(os.environ.get("GEMINI_API_KEY"))

    # Smart briefing BEFORE transcription (Gemini path)
    user_context = None
    if use_smart_briefing:
        from .briefing import run_smart_briefing

        user_context = run_smart_briefing(
            ep, style, gemini_model=cfg.transcribe.gemini_model, tracer=tracer
        )

    # Transcription (benefits from cached Gemini URIs + speaker context from briefing)
    if not questionary.confirm("Run transcription?", default=True, style=VX_STYLE).ask():
        print("\n  Skipped transcription. Run from project menu later.\n")
    else:
        _run_transcription(ep, clip_metadata, cfg)

    # Check if cached Phase 1 reviews exist — offer to force re-run
    force_phase1 = False
    review_suffix = f"review_{provider}_latest.json"
    has_cached = any(
        (ep.clip_paths(c["clip_id"]).review / review_suffix).exists() for c in manifest["clips"]
    )
    if has_cached:
        force_phase1 = questionary.confirm(
            "Cached Phase 1 reviews found. Re-run from scratch?",
            default=False,
            style=VX_STYLE,
        ).ask()

    print("\n  Phase 1: Reviewing clips...\n")
    if provider == "gemini":
        reviews, failed = run_phase1_gemini(
            ep,
            manifest,
            cfg.gemini,
            force=force_phase1,
            tracer=tracer,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    else:
        reviews, failed = run_phase1_claude(
            ep,
            manifest,
            cfg.claude,
            force=force_phase1,
            style_supplement=p1_supplement,
            user_context=user_context,
        )
    reviews, failed = _retry_failed_phase1(
        failed,
        reviews,
        ep,
        manifest,
        provider,
        cfg,
        tracer=tracer,
        style_supplement=p1_supplement,
        user_context=user_context,
    )

    # Manual briefing AFTER Phase 1 (non-Gemini path)
    if not use_smart_briefing:
        from .briefing import run_briefing

        user_context = run_briefing(reviews, style, ep.root)

    # Ask about visual mode
    visual = False
    if provider == "gemini":
        visual = _ask_visual_phase2(ep, reviews)

    print("\n  Phase 2: Generating storyboard...\n")
    run_phase2(
        clip_reviews=reviews,
        editorial_paths=ep,
        project_name=name,
        provider=provider,
        gemini_cfg=cfg.gemini,
        claude_cfg=cfg.claude,
        style=style,
        user_context=user_context,
        tracer=tracer,
        visual=visual,
        style_supplement=p2_supplement,
        review_config=cfg.review,
        interactive=True,
    )

    # Phase 3 — Visual Monologue (if preset supports it)
    if style_preset and style_preset.has_phase3:
        if questionary.confirm(
            "Generate visual monologue (text overlay plan)?", default=True, style=VX_STYLE
        ).ask():
            from .editorial_agent import run_monologue

            print("\n  Phase 3: Visual Monologue")
            run_monologue(
                editorial_paths=ep,
                provider=provider,
                gemini_cfg=cfg.gemini,
                claude_cfg=cfg.claude,
                style_preset=style_preset,
                user_context=user_context,
                tracer=tracer,
            )

    tracer.print_summary("Analysis Total")
    print("\n  Storyboard ready!")


def _run_director_review(ep, meta, cfg):
    """Run the Editorial Director on the latest storyboard without regenerating Phase 2."""
    from .config import ReviewBudget
    from .models import EditorialStoryboard
    from .review_display import (
        print_change_diff,
        print_post_review,
        print_pre_review,
        print_turn,
    )
    from .tracing import ProjectTracer
    from .versioning import resolve_user_context_path

    provider = meta.get("provider", "gemini")

    # Load latest storyboard
    sb_path = None
    for p in ("gemini", "claude"):
        candidate = ep.storyboard / f"editorial_{p}_latest.json"
        if candidate.exists():
            sb_path = candidate
            provider = p
            break
    if not sb_path:
        print("\n  No storyboard found. Run Phase 2 first.\n")
        return

    try:
        storyboard = EditorialStoryboard.model_validate_json(sb_path.read_text())
    except Exception as e:
        print(f"\n  Error loading storyboard: {e}\n")
        return

    # Load clip reviews
    reviews = _load_reviews(ep)
    if not reviews:
        print("\n  No clip reviews found. Run Phase 1 first.\n")
        return

    # Load user context
    user_context = None
    ctx_path = resolve_user_context_path(ep.root)
    if ctx_path:
        user_context = json.loads(ctx_path.read_text())

    tracer = ProjectTracer(ep.root)
    budget = ReviewBudget.from_config(cfg.review)

    # Pre-review: compute and show eval scores
    from .director_prompts import build_eval_summary

    transcripts = {}
    clip_ids = {s.clip_id for s in storyboard.segments}
    from .editorial_director import _load_transcripts

    transcripts = _load_transcripts(ep.clips_dir, clip_ids)
    eval_summary = build_eval_summary(storyboard, reviews, user_context, transcripts)

    print()
    print_pre_review(
        eval_summary=eval_summary,
        seg_count=len(storyboard.segments),
        duration_sec=storyboard.total_segments_duration,
        budget=budget,
    )

    from .editorial_director import run_editorial_review

    # Load style guidelines from project preset (if any)
    style_guidelines = None
    preset_key = meta.get("style_preset")
    if preset_key:
        from .style_presets import get_preset

        preset = get_preset(preset_key)
        if preset:
            style_guidelines = preset.phase2_supplement

    # Snapshot before review — harness mutates storyboard in-place
    original_snapshot = storyboard.model_dump()

    reviewed, review_log = run_editorial_review(
        storyboard=storyboard,
        clip_reviews=reviews,
        user_context=user_context,
        clips_dir=ep.clips_dir,
        review_config=cfg.review,
        tracer=tracer,
        interactive=True,
        turn_callback=print_turn,
        style_guidelines=style_guidelines,
    )

    tracer.print_summary("Director Review")

    # Post-review summary
    had_changes = reviewed.model_dump() != original_snapshot
    print_post_review(review_log, had_changes)

    # Confirmation flow (only if there are changes)
    if not had_changes:
        return

    print("  Changes are NOT saved yet.")
    while True:
        action = questionary.select(
            "Save director's changes as new version?",
            choices=[
                questionary.Choice("Accept & save", value="accept"),
                questionary.Choice("Show full diff", value="diff"),
                questionary.Choice("Discard changes", value="reject"),
            ],
            style=VX_STYLE,
        ).ask()

        if action == "diff":
            print_change_diff(review_log)
            continue
        elif action == "reject" or action is None:
            print("\n  Changes discarded. Original storyboard unchanged.\n")
            return
        else:  # accept
            break

    # Save the reviewed storyboard
    from .render import render_html_preview, render_markdown
    from .versioning import (
        begin_version,
        commit_version,
        current_version,
        update_latest_symlink,
        versioned_dir,
        versioned_path,
    )

    rv_version = current_version(ep.root, f"review_{provider}")
    if rv_version == 0:
        rv_version = current_version(ep.root, "review")
    review_parent_id = f"rv.{rv_version}" if rv_version > 0 else None

    art_meta = begin_version(
        ep.root,
        phase="storyboard",
        provider=provider,
        inputs={"source": "director_review"},
        config_snapshot={"review_model": cfg.review.model},
        target_dir=ep.storyboard,
        parent_id=review_parent_id,
    )
    v = art_meta.version
    base = f"editorial_{provider}"

    json_path = versioned_path(ep.storyboard / f"{base}.json", v)
    json_path.write_text(reviewed.model_dump_json(indent=2))
    update_latest_symlink(json_path)

    md_path = versioned_path(ep.storyboard / f"{base}.md", v)
    md_path.write_text(render_markdown(reviewed))
    update_latest_symlink(md_path)

    export_dir = versioned_dir(ep.exports, v)
    preview_html = render_html_preview(reviewed, clips_dir=ep.clips_dir, output_dir=export_dir)
    preview_path = export_dir / "preview.html"
    preview_path.write_text(preview_html)
    update_latest_symlink(export_dir)

    commit_version(ep.root, art_meta, output_paths=[json_path, md_path], target_dir=ep.storyboard)

    print(f"\n  Storyboard updated (v{v}) with director fixes.")
    print(f"    JSON:    {json_path}")
    print(f"    Preview: {preview_path}")

    # Show full segment-by-segment diff
    print_change_diff(review_log)

    # Auto-open preview in browser
    import subprocess as _sp

    try:
        _sp.run(["open", str(preview_path)], check=False)
        print("  Preview opened in browser.")
    except Exception:
        pass
    print()


def _chat_with_director(ep, meta, cfg):
    """Open conversational director for user-driven storyboard editing.

    Sessions are persisted — each confirmed edit auto-saves a new storyboard version.
    Active sessions can be resumed.
    """
    from datetime import datetime, timezone

    from .config import ReviewBudget
    from .models import ChatSession, EditorialStoryboard
    from .review_display import (
        print_change_diff,
        print_post_review,
        print_pre_review,
        print_turn,
    )
    from .tracing import ProjectTracer
    from .versioning import current_version, resolve_user_context_path

    from .editorial_director import (
        _next_session_id,
        _session_dir,
        find_active_session,
        run_director_chat,
        save_session,
    )

    provider = meta.get("provider", "gemini")

    # Check for active session
    active = find_active_session(ep)
    session = None

    if active:
        choice = questionary.select(
            f"Found active session: {active.session_id} "
            f"(v{active.storyboard_version}, {active.total_edits} edits)",
            choices=[
                questionary.Choice("Resume session", value="resume"),
                questionary.Choice("Start new session", value="new"),
            ],
            style=VX_STYLE,
        ).ask()
        if choice == "resume":
            session = active
        elif choice == "new":
            # Archive the old session so it doesn't block the new one
            active.status = "archived"
            save_session(active, ep)

    # Load storyboard (from session version if resuming, else latest)
    if session:
        # Load the storyboard at the session's current version
        sb_path = ep.storyboard / f"editorial_{provider}_v{session.storyboard_version}.json"
        if not sb_path.exists():
            sb_path = ep.storyboard / f"editorial_{provider}_latest.json"
    else:
        sb_path = None
        for p in ("gemini", "claude"):
            candidate = ep.storyboard / f"editorial_{p}_latest.json"
            if candidate.exists():
                sb_path = candidate
                provider = p
                break

    if not sb_path or not sb_path.exists():
        print("\n  No storyboard found. Run Phase 2 first.\n")
        return

    try:
        storyboard = EditorialStoryboard.model_validate_json(sb_path.read_text())
    except Exception as e:
        print(f"\n  Error loading storyboard: {e}\n")
        return

    reviews = _load_reviews(ep)
    if not reviews:
        print("\n  No clip reviews found. Run Phase 1 first.\n")
        return

    user_context = None
    ctx_path = resolve_user_context_path(ep.root)
    if ctx_path:
        user_context = json.loads(ctx_path.read_text())

    # Style guidelines
    style_guidelines = None
    preset_key = meta.get("style_preset")
    if preset_key:
        from .style_presets import get_preset

        preset = get_preset(preset_key)
        if preset:
            style_guidelines = preset.phase2_supplement

    # Create new session if not resuming
    if not session:
        sd = _session_dir(ep)
        sid = _next_session_id(sd)
        # Find current storyboard version for the starting_version
        sv = current_version(ep.root, f"editorial_{provider}")
        if sv == 0:
            sv = current_version(ep.root, "storyboard")
        session = ChatSession(
            session_id=sid,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            storyboard_version=sv,
            starting_version=sv,
            provider=provider,
            style_preset=preset_key or "",
        )
        save_session(session, ep)
        print(f"\n  Started session {sid}")
        print(
            f"  Tip: Run {_BOLD}vx preview --serve{_RESET} "
            f"in another terminal for live-updating preview."
        )

    tracer = ProjectTracer(ep.root)
    budget = ReviewBudget.from_config(cfg.review)

    # Pre-review summary
    from .director_prompts import build_eval_summary
    from .editorial_director import _load_transcripts

    clip_ids = {r.get("clip_id", "") for r in reviews} - {""}
    transcripts = _load_transcripts(ep.clips_dir, clip_ids)
    eval_summary = build_eval_summary(storyboard, reviews, user_context, transcripts)

    print()
    print_pre_review(
        eval_summary=eval_summary,
        seg_count=len(storyboard.segments),
        duration_sec=storyboard.total_segments_duration,
        budget=budget,
    )

    # Input callback
    def get_user_input():
        try:
            result = questionary.text(
                "You:",
                style=VX_STYLE,
                instruction="(type 'done' to finish, Esc to cancel)",
            ).ask()
            return result
        except KeyboardInterrupt:
            return None

    reviewed, review_log = run_director_chat(
        storyboard=storyboard,
        clip_reviews=reviews,
        user_context=user_context,
        clips_dir=ep.clips_dir,
        review_config=cfg.review,
        tracer=tracer,
        style_guidelines=style_guidelines,
        turn_callback=print_turn,
        input_callback=get_user_input,
        editorial_paths=ep,
        session=session,
    )

    tracer.print_summary("Director Chat")

    # Post-chat summary (edits were already auto-saved)
    had_changes = bool(review_log.changes)
    print_post_review(review_log, had_changes)

    if had_changes:
        print(
            f"  All changes saved (v{session.starting_version} -> "
            f"v{session.storyboard_version}, "
            f"{session.total_edits} edits)."
        )
        print_change_diff(review_log)
    print()


def _load_reviews(ep):
    """Load all Phase 1 reviews for a project."""
    reviews = []
    for clip_id in ep.discover_clips():
        cp = ep.clip_paths(clip_id)
        for pattern in ["review_*_latest.json", "review_*.json"]:
            found = [
                f
                for f in cp.review.glob(pattern)
                if not f.name.endswith("_latest.json") or f.is_symlink()
            ]
            if found:
                reviews.append(json.loads(found[0].read_text()))
                break
    return reviews


def _run_format_selection(clip_metadata, meta, ep):
    """Analyze source formats, filter Live Photos, let user pick output format.

    Updates meta and writes project.json. Returns (filtered_clip_metadata, OutputFormat).
    """
    from .config import OutputFormat
    from .format_analyzer import (
        analyze_source_formats,
        recommend_output_format,
        build_format_choices,
        format_summary_text,
    )

    analysis = analyze_source_formats(clip_metadata)
    print(f"\n{format_summary_text(analysis, clip_metadata)}\n")

    # Live Photo filtering
    live_ids = analysis["live_photo_ids"]
    if live_ids:
        display = ", ".join(f"{cid}" for cid in live_ids[:6])
        if len(live_ids) > 6:
            display += "..."
        print(f"  Possible Live Photo clips ({len(live_ids)}): {display}\n")

        action = questionary.select(
            "How to handle Live Photo clips?",
            choices=[
                "Include all",
                "Exclude Live Photos",
                "Choose individually",
            ],
            style=VX_STYLE,
        ).ask()

        if action == "Exclude Live Photos":
            clip_metadata = [c for c in clip_metadata if c["clip_id"] not in live_ids]
            print(f"  Excluded {len(live_ids)} Live Photos, {len(clip_metadata)} clips remain\n")
            # Re-analyze without live photos
            analysis = analyze_source_formats(clip_metadata)
        elif action == "Choose individually":
            keep = questionary.checkbox(
                "Select Live Photo clips to keep:",
                choices=[
                    questionary.Choice(
                        f"{cid} ({next((c['duration_sec'] for c in clip_metadata if c['clip_id'] == cid), 0):.1f}s)",
                        value=cid,
                        checked=False,
                    )
                    for cid in live_ids
                ],
                style=VX_STYLE,
            ).ask()
            if keep is None:
                keep = []
            exclude = set(live_ids) - set(keep)
            if exclude:
                clip_metadata = [c for c in clip_metadata if c["clip_id"] not in exclude]
                print(f"  Excluded {len(exclude)} Live Photos, {len(clip_metadata)} clips remain\n")
                analysis = analyze_source_formats(clip_metadata)

    # Format recommendation
    recommended, rationale = recommend_output_format(analysis)
    print(f"  {rationale}\n")

    if analysis["has_mixed_resolutions"] or analysis["has_mixed_aspects"]:
        # Mixed sources — let user choose
        choices = build_format_choices(analysis)
        choice_labels = [c["label"] for c in choices]
        selected = questionary.select(
            "Output format:",
            choices=choice_labels,
            style=VX_STYLE,
        ).ask()
        if selected:
            chosen = next(c for c in choices if c["label"] == selected)
            output_format = OutputFormat(
                width=chosen["width"],
                height=chosen["height"],
                fps=chosen["fps"],
                orientation=chosen["orientation"],
                label=selected.replace(" (recommended)", ""),
            )
        else:
            output_format = recommended
    else:
        # Uniform — confirm recommendation
        if not questionary.confirm(
            f"Use {recommended.label} @ {recommended.fps}fps?",
            default=True,
            style=VX_STYLE,
        ).ask():
            choices = build_format_choices(analysis)
            choice_labels = [c["label"] for c in choices]
            selected = questionary.select(
                "Output format:",
                choices=choice_labels,
                style=VX_STYLE,
            ).ask()
            if selected:
                chosen = next(c for c in choices if c["label"] == selected)
                output_format = OutputFormat(
                    width=chosen["width"],
                    height=chosen["height"],
                    fps=chosen["fps"],
                    orientation=chosen["orientation"],
                    label=selected.replace(" (recommended)", ""),
                )
            else:
                output_format = recommended
        else:
            output_format = recommended

    # Fit mode
    if analysis["has_mixed_aspects"]:
        fit = questionary.select(
            "How to handle different aspect ratios?",
            choices=[
                questionary.Choice(
                    "Pad (black bars, preserve full frame)",
                    value="pad",
                ),
                questionary.Choice(
                    "Crop to fill (no bars, may lose edges)",
                    value="crop",
                ),
            ],
            style=VX_STYLE,
        ).ask()
        if fit:
            output_format.fit_mode = fit

    # Codec
    codec = questionary.select(
        "Output codec:",
        choices=[
            questionary.Choice(
                "Auto (hardware-accelerated on Apple Silicon, software fallback)", value="auto"
            ),
            questionary.Choice(
                "H.264 software (libx264, universal compatibility)", value="libx264"
            ),
            questionary.Choice("H.265 software (libx265, smaller files)", value="libx265"),
        ],
        style=VX_STYLE,
    ).ask()
    if codec:
        output_format.codec = codec

    # Persist
    meta["output_format"] = output_format.to_dict()
    (ep.root / "project.json").write_text(json.dumps(meta, indent=2))
    print(
        f"\n  Output format: {output_format.label}, {output_format.width}x{output_format.height}"
        f" @ {output_format.fps}fps, {output_format.codec}, fit={output_format.fit_mode}\n"
    )

    return clip_metadata, output_format


def _ask_visual_phase2(ep, reviews):
    """Show concat status and ask about visual Phase 2. Returns bool.

    Proxies are concatenated into bundles (≤40 min each) to work around Gemini's
    10-video-per-prompt limit. Works for any number of clips.
    """
    unique_ids = list(dict.fromkeys(r.get("clip_id", "") for r in reviews))
    total = len(unique_ids)

    # Check if concat bundles already exist
    concat_dir = ep.root / "concat_proxies"
    has_concat = concat_dir.exists() and list(concat_dir.glob("bundle_*.mp4"))

    print(f"\n  Visual Phase 2: {total} clips (concatenated for Gemini)")
    if has_concat:
        print("  Concat bundles cached (no rebuild needed)")

    return questionary.confirm(
        "Include proxy videos in Phase 2? (AI sees footage, better edits)",
        default=False,
        style=VX_STYLE,
    ).ask()


def _run_transcription(ep, clip_metadata, cfg):
    """Run transcription as part of a pipeline flow (non-interactive provider selection)."""
    from .editorial_agent import _resolve_transcribe_provider, transcribe_all_clips

    t_provider = _resolve_transcribe_provider(cfg.transcribe)
    if not t_provider:
        print("\n  Skipping transcription (no provider available)")
        return

    # Load speaker context from briefing if available
    from .versioning import resolve_user_context_path

    speaker_context = None
    _uc_path = resolve_user_context_path(ep.root)
    if _uc_path:
        ctx = json.loads(_uc_path.read_text())
        speaker_context = ctx.get("people", "") or None

    print(f"\n  Transcribing audio ({t_provider})...\n")
    transcripts = transcribe_all_clips(
        clip_metadata, ep, cfg.transcribe, provider=t_provider, speaker_context=speaker_context
    )
    count = len(transcripts)
    print(f"\n  Transcribed {count}/{len(clip_metadata)} clips with speech")


def _run_transcription_interactive(name, cfg):
    """Run transcription from project actions menu with provider choice."""
    from .editorial_agent import (
        transcribe_all_clips,
    )

    ep = cfg.editorial_project(name)
    # Let user pick provider
    available = []
    try:
        import mlx_whisper  # noqa: F401

        available.append("mlx (local, fast, no API cost)")
    except ImportError:
        pass
    if os.environ.get("GEMINI_API_KEY"):
        available.append("gemini (cloud, speakers + sound events)")
    if not available:
        print("\n  No transcription provider available.")
        print("  Install mlx-whisper or set GEMINI_API_KEY.\n")
        return

    if len(available) == 1:
        t_provider = available[0].split(" ")[0]
    else:
        choice = questionary.select(
            "Transcription provider:",
            choices=available,
            style=VX_STYLE,
        ).ask()
        if not choice:
            return
        t_provider = choice.split(" ")[0]

    # Build clip metadata from existing clips
    clips = ep.discover_clips()
    if not clips:
        print("\n  No clips found.\n")
        return
    clip_metadata = [{"clip_id": cid} for cid in clips]

    # Check for existing transcripts and offer overwrite
    cached = [cid for cid in clips if ep.clip_paths(cid).has_transcript()]
    if cached:
        print(f"\n  {len(cached)}/{len(clips)} clips already have transcripts.")
        if not questionary.confirm(
            "Overwrite existing transcripts?", default=False, style=VX_STYLE
        ).ask():
            print("  Keeping cached transcripts (only un-transcribed clips will be processed).")
        else:
            for cid in cached:
                audio_dir = ep.clip_paths(cid).audio
                for f in [
                    "transcript.json",
                    "transcript_latest.json",
                    "transcript.vtt",
                    "transcript_preview.html",
                ]:
                    p = audio_dir / f
                    if p.exists() or p.is_symlink():
                        p.unlink()
            print(f"  Cleared {len(cached)} cached transcripts.")

    # Load speaker context from briefing
    from .versioning import resolve_user_context_path

    speaker_context = None
    _uc_path = resolve_user_context_path(ep.root)
    if _uc_path:
        ctx = json.loads(_uc_path.read_text())
        speaker_context = ctx.get("people", "") or None

    print(f"\n  Transcribing {len(clips)} clips ({t_provider})...\n")
    transcripts = transcribe_all_clips(
        clip_metadata, ep, cfg.transcribe, provider=t_provider, speaker_context=speaker_context
    )
    count = len(transcripts)
    print(f"\n  Done. {count}/{len(clips)} clips have speech\n")


def _show_status(name, meta, cfg):
    """Print project status."""
    ep = cfg.editorial_project(name)
    clips = ep.discover_clips()
    provider = meta.get("provider", "gemini")
    print(f"\n  Type: {meta['type']}, Provider: {provider}, Style: {meta.get('style', '?')}")
    print(f"  Clips: {len(clips)}")
    for cid in clips:
        cp = ep.clip_paths(cid)
        cached = [k for k, v in cp.cache_status().items() if v]
        transcribed = "transcribed" if cp.has_transcript() else ""
        reviewed = "reviewed" if cp.has_review(provider) else "pending"
        parts = ", ".join(cached)
        if transcribed:
            parts += f" | {transcribed}"
        parts += f" | {reviewed}"
        print(f"    {cid}: {parts}")

    storyboards = list(ep.storyboard.glob("editorial_*_v*.json")) if ep.storyboard.exists() else []
    if storyboards:
        print(f"  Storyboards: {len(storyboards)}")
        for s in sorted(storyboards):
            print(f"    {s.name}")

    # LLM usage
    from .tracing import load_all_traces, summarize_traces

    traces = load_all_traces(ep.root)
    if traces:
        ts = summarize_traces(traces)
        print(
            f"  LLM Usage: {ts['calls']} calls | "
            f"{ts['total_tokens']:,} tokens | "
            f"~${ts['estimated_cost_usd']:.4f}"
        )
        for phase, ps in ts.get("by_phase", {}).items():
            print(
                f"    {phase}: {ps['calls']} calls, "
                f"{ps['total_tokens']:,} tokens, "
                f"~${ps['estimated_cost_usd']:.4f}"
            )
    print()


def _settings_flow(cfg):
    """Edit workspace settings."""
    ws_path = Path(".vx.json")
    ws = (
        json.loads(ws_path.read_text())
        if ws_path.exists()
        else {"provider": "gemini", "style": "vlog"}
    )

    provider_choices = ["gemini", "claude"]
    provider_default = ws.get("provider", "gemini")
    if provider_default not in provider_choices:
        provider_default = "gemini"

    provider = questionary.select(
        "Default AI provider:",
        choices=provider_choices,
        default=provider_default,
        style=VX_STYLE,
    ).ask()
    if provider:
        ws["provider"] = provider

    style_choices = [
        "vlog",
        "travel-vlog",
        "family-video",
        "event-recap",
        "cinematic",
        "short-form",
        questionary.Choice("Custom...", value="__custom__"),
    ]
    style_default = ws.get("style", "vlog")
    if style_default not in [c if isinstance(c, str) else c.value for c in style_choices]:
        style_default = "vlog"

    style = questionary.select(
        "Default video style:",
        choices=style_choices,
        default=style_default,
        style=VX_STYLE,
    ).ask()
    if style == "__custom__":
        style = questionary.text(
            "Describe your video style:",
            instruction="(e.g., 'lo-fi daily journal', 'drone landscape showcase')",
            style=VX_STYLE,
        ).ask()
    if style:
        ws["style"] = style

    ws_path.write_text(json.dumps(ws, indent=2) + "\n")
    print(f"\n  Settings saved: provider={ws['provider']}, style={ws['style']}\n")
