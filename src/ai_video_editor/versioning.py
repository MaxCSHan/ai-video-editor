"""Composable versioning for LLM-generated artifacts.

Two-phase commit protocol:
  1. begin_version()  — reserve a version number, write a "pending" sidecar
  2. commit_version() — mark complete, update project.json counter, update _latest symlink
     OR fail_version() — mark failed, no counter/symlink update

Every versioned output gets a .meta.json sidecar recording lineage (inputs),
status, and config. This enables composition: mixing storyboard v2 + monologue v1
for a rough cut, and tracing exactly which inputs produced any given output.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import ArtifactMeta, Composition


# ---------------------------------------------------------------------------
# Project metadata (project.json)
# ---------------------------------------------------------------------------


def read_project_meta(project_root: Path) -> dict:
    meta_path = project_root / "project.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {}


def write_project_meta(project_root: Path, meta: dict):
    (project_root / "project.json").write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Path helpers (existing API, preserved)
# ---------------------------------------------------------------------------


def versioned_path(base_path: Path, version: int) -> Path:
    """Add version suffix to a file path: foo.md → foo_v1.md"""
    return base_path.with_stem(f"{base_path.stem}_v{version}")


def versioned_dir(base_dir: Path, version: int) -> Path:
    """Create a versioned subdirectory: exports/ → exports/v1/"""
    vdir = base_dir / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def update_latest_symlink(target: Path, link_name: str | None = None):
    """Create or update a 'latest' symlink pointing to the target.

    For files:  foo_v2.md → foo_latest.md
    For dirs:   exports/v2/ → exports/latest/
    """
    if target.is_dir():
        link = target.parent / "latest"
    else:
        if link_name:
            link = target.parent / link_name
        else:
            stem = target.stem
            base_stem = re.sub(r"_v\d+$", "", stem)
            link = target.parent / f"{base_stem}_latest{target.suffix}"

    if link.exists() or link.is_symlink():
        link.unlink()

    try:
        rel = target.relative_to(link.parent)
    except ValueError:
        rel = target
    link.symlink_to(rel)
    return link


def list_versions(directory: Path, pattern: str) -> list[tuple[int, Path]]:
    """Find all versioned files matching a pattern like 'editorial_gemini_v*.md'.

    Returns sorted list of (version_number, path) tuples.
    """
    results = []
    if not directory.exists():
        return results
    for f in directory.iterdir():
        m = re.match(pattern.replace("*", r"(\d+)"), f.name)
        if m:
            results.append((int(m.group(1)), f))
    return sorted(results)


# ---------------------------------------------------------------------------
# Sidecar (.meta.json) helpers
# ---------------------------------------------------------------------------


def _sidecar_path_for(output_path: Path) -> Path:
    """Get the .meta.json sidecar path for a versioned output file.

    editorial_gemini_v4.json → editorial_gemini_v4.meta.json
    """
    return output_path.with_suffix(".meta.json")


def _scan_meta_files(
    directory: Path, phase: str | None = None, provider: str | None = None
) -> list[ArtifactMeta]:
    """Scan a directory for .meta.json sidecars and return parsed ArtifactMeta objects."""
    results = []
    if not directory.exists():
        return results
    for f in directory.glob("*.meta.json"):
        try:
            meta = ArtifactMeta.model_validate_json(f.read_text())
            if phase and meta.phase != phase:
                continue
            if provider and meta.provider != provider:
                continue
            results.append(meta)
        except Exception:
            continue
    return sorted(results, key=lambda m: m.version)


def _build_artifact_id(
    phase: str, provider: str, version: int, clip_id: str | None = None, track: str = "main"
) -> str:
    """Build a human-readable artifact ID.

    Examples: "storyboard:gemini:v3", "review:gemini:C0073:v2"
    """
    parts = [phase, provider]
    if clip_id:
        parts.append(clip_id)
    if track != "main":
        parts.append(track)
    parts.append(f"v{version}")
    return ":".join(parts)


def _next_version_number(directory: Path, phase: str, provider: str) -> int:
    """Determine the next version number by scanning existing .meta.json sidecars
    AND versioned files (for legacy compat). Returns max + 1."""
    max_v = 0

    # Check .meta.json sidecars
    for meta in _scan_meta_files(directory, phase, provider):
        max_v = max(max_v, meta.version)

    # Also check versioned files directly (handles pre-migration projects)
    if directory.exists():
        for f in directory.iterdir():
            if f.suffix == ".json" and not f.name.endswith(".meta.json"):
                m = re.search(r"_v(\d+)\.json$", f.name)
                if m:
                    max_v = max(max_v, int(m.group(1)))

    return max_v + 1


# ---------------------------------------------------------------------------
# Two-phase commit protocol
# ---------------------------------------------------------------------------


def begin_version(
    project_root: Path,
    phase: str,
    provider: str = "",
    inputs: dict[str, str] | None = None,
    clip_id: str | None = None,
    track: str = "main",
    config_snapshot: dict | None = None,
    target_dir: Path | None = None,
) -> ArtifactMeta:
    """Reserve a version number and create a 'pending' artifact sidecar.

    Does NOT update the project.json counter or _latest symlink.
    The sidecar is written immediately to reserve the version number.

    Args:
        project_root: Project root (or clip root for per-clip Phase 1).
        phase: Phase name ("review", "storyboard", "monologue", "cut", "preview").
        provider: LLM provider ("gemini", "claude").
        inputs: Lineage map — role → artifact_id of each input used.
        clip_id: Set for per-clip artifacts (Phase 1 reviews).
        track: Experiment track name (default "main").
        config_snapshot: Model/temperature/style settings used.
        target_dir: Directory where the sidecar will be written.
                    If None, inferred from phase and project_root.
    """
    if target_dir is None:
        target_dir = _phase_dir(project_root, phase, track)

    target_dir.mkdir(parents=True, exist_ok=True)
    v = _next_version_number(target_dir, phase, provider)

    meta = ArtifactMeta(
        artifact_id=_build_artifact_id(phase, provider, v, clip_id, track),
        phase=phase,
        provider=provider,
        version=v,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
        inputs=inputs or {},
        clip_id=clip_id,
        track=track,
        config_snapshot=config_snapshot or {},
    )

    # Write pending sidecar to reserve the version number
    sidecar = target_dir / f".pending_{phase}_{provider}_v{v}.meta.json"
    sidecar.write_text(meta.model_dump_json(indent=2))
    return meta


def commit_version(
    project_root: Path,
    meta: ArtifactMeta,
    output_paths: list[Path],
    target_dir: Path | None = None,
) -> ArtifactMeta:
    """Mark an artifact as complete. Updates project.json counter and _latest symlink.

    Args:
        project_root: Project root (or clip root for per-clip Phase 1).
        meta: The ArtifactMeta returned by begin_version().
        output_paths: List of output files produced (for sidecar record + symlink updates).
        target_dir: Directory containing the sidecar. If None, inferred.
    """
    if target_dir is None:
        target_dir = _phase_dir(project_root, meta.phase, meta.track)

    # Update meta
    meta.status = "complete"
    meta.completed_at = datetime.now(timezone.utc).isoformat()
    meta.output_files = [
        str(p.relative_to(target_dir)) if p.is_relative_to(target_dir) else p.name
        for p in output_paths
    ]

    # Remove pending sidecar
    pending = target_dir / f".pending_{meta.phase}_{meta.provider}_v{meta.version}.meta.json"
    if pending.exists():
        pending.unlink()

    # Write final sidecar next to the primary output
    if output_paths:
        primary = output_paths[0]
        sidecar = _sidecar_path_for(primary)
    else:
        sidecar = target_dir / f"{meta.phase}_{meta.provider}_v{meta.version}.meta.json"
    sidecar.write_text(meta.model_dump_json(indent=2))

    # Update project.json version counter
    proj_meta = read_project_meta(project_root)
    versions = proj_meta.setdefault("versions", {})
    phase_key = f"{meta.phase}_{meta.provider}" if meta.clip_id else meta.phase
    # For backward compat with existing keys like "analyze", "monologue", "cut"
    compat_key = _compat_phase_key(meta.phase, meta.provider)
    if compat_key:
        versions[compat_key] = max(versions.get(compat_key, 0), meta.version)
    versions[phase_key] = max(versions.get(phase_key, 0), meta.version)
    write_project_meta(project_root, proj_meta)

    # Update _latest symlinks for each output
    for p in output_paths:
        if p.is_dir():
            update_latest_symlink(p)
        elif p.exists():
            update_latest_symlink(p)

    return meta


def fail_version(
    project_root: Path,
    meta: ArtifactMeta,
    error: str | None = None,
    target_dir: Path | None = None,
):
    """Mark an artifact as failed. Does NOT update counter or _latest symlink.

    The failed sidecar remains on disk for inspection.
    """
    if target_dir is None:
        target_dir = _phase_dir(project_root, meta.phase, meta.track)

    meta.status = "failed"
    meta.completed_at = datetime.now(timezone.utc).isoformat()
    meta.error = error

    # Remove pending sidecar
    pending = target_dir / f".pending_{meta.phase}_{meta.provider}_v{meta.version}.meta.json"
    if pending.exists():
        pending.unlink()

    # Write failed sidecar
    sidecar = target_dir / f".failed_{meta.phase}_{meta.provider}_v{meta.version}.meta.json"
    sidecar.write_text(meta.model_dump_json(indent=2))


def _compat_phase_key(phase: str, provider: str) -> str | None:
    """Map phase names to existing project.json keys for backward compat."""
    mapping = {
        "storyboard": "analyze",
        "review": f"review_{provider}",
        "monologue": "monologue",
        "cut": "cut",
        "preview": "preview",
    }
    return mapping.get(phase)


def _phase_dir(project_root: Path, phase: str, track: str = "main") -> Path:
    """Resolve the directory for a phase's outputs, track-aware."""
    base_dirs = {
        "review": project_root / "review",  # per-clip: project_root is clip root
        "storyboard": project_root / "storyboard",
        "monologue": project_root / "storyboard",
        "cut": project_root / "exports",
        "preview": project_root / "exports",
    }
    base = base_dirs.get(phase, project_root)
    if track != "main":
        return base / track
    return base


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------


def list_artifacts(
    project_root: Path,
    phase: str | None = None,
    provider: str | None = None,
    include_failed: bool = False,
) -> list[ArtifactMeta]:
    """Discover all artifacts by scanning .meta.json sidecars.

    Searches storyboard/, exports/, and per-clip review/ directories.
    """
    _maybe_migrate_legacy(project_root)

    results = []
    search_dirs = [
        project_root / "storyboard",
        project_root / "exports",
    ]
    # Also search track subdirectories
    for d in search_dirs:
        if d.exists():
            for sub in d.iterdir():
                if sub.is_dir() and sub.name not in ("latest",) and not sub.name.startswith("v"):
                    search_dirs.append(sub)

    for directory in search_dirs:
        if not directory.exists():
            continue
        for f in directory.glob("*.meta.json"):
            if f.name.startswith(".") and not include_failed:
                if f.name.startswith(".failed_"):
                    continue
                if f.name.startswith(".pending_"):
                    continue
            try:
                meta = ArtifactMeta.model_validate_json(f.read_text())
                if phase and meta.phase != phase:
                    continue
                if provider and meta.provider != provider:
                    continue
                if not include_failed and meta.status == "failed":
                    continue
                results.append(meta)
            except Exception:
                continue

    return sorted(results, key=lambda m: (m.phase, m.version))


def list_clip_artifacts(
    project_root: Path,
    clip_id: str,
    phase: str | None = None,
    provider: str | None = None,
) -> list[ArtifactMeta]:
    """Discover artifacts for a specific clip (Phase 1 reviews)."""
    clip_review_dir = project_root / "clips" / clip_id / "review"
    return _scan_meta_files(clip_review_dir, phase, provider)


def get_artifact(project_root: Path, artifact_id: str) -> ArtifactMeta | None:
    """Find an artifact by its ID."""
    for meta in list_artifacts(project_root, include_failed=True):
        if meta.artifact_id == artifact_id:
            return meta
    return None


def resolve_artifact_path(project_root: Path, artifact_id: str) -> Path | None:
    """Given an artifact_id, return the path to its primary output file."""
    meta = get_artifact(project_root, artifact_id)
    if not meta or not meta.output_files:
        return None

    target_dir = _phase_dir(project_root, meta.phase, meta.track)
    primary = target_dir / meta.output_files[0]
    return primary if primary.exists() else None


# ---------------------------------------------------------------------------
# Composition management
# ---------------------------------------------------------------------------


def _compositions_path(project_root: Path) -> Path:
    return project_root / "compositions.json"


def list_compositions(project_root: Path) -> list[Composition]:
    path = _compositions_path(project_root)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [Composition.model_validate(c) for c in data]


def save_composition(project_root: Path, composition: Composition):
    """Save or update a composition (upserts by name)."""
    comps = list_compositions(project_root)
    comps = [c for c in comps if c.name != composition.name]
    comps.append(composition)
    path = _compositions_path(project_root)
    path.write_text(json.dumps([c.model_dump() for c in comps], indent=2))


def get_composition(project_root: Path, name: str) -> Composition | None:
    for c in list_compositions(project_root):
        if c.name == name:
            return c
    return None


def delete_composition(project_root: Path, name: str) -> bool:
    comps = list_compositions(project_root)
    filtered = [c for c in comps if c.name != name]
    if len(filtered) == len(comps):
        return False
    path = _compositions_path(project_root)
    path.write_text(json.dumps([c.model_dump() for c in filtered], indent=2))
    return True


# ---------------------------------------------------------------------------
# Backward-compatible next_version (wraps begin + immediate commit)
# ---------------------------------------------------------------------------


def next_version(project_root: Path, phase: str) -> int:
    """Increment and return the next version number for a phase.

    DEPRECATED: Use begin_version() / commit_version() for new code.
    Kept for backward compatibility — immediately commits the version.
    """
    meta = read_project_meta(project_root)
    versions = meta.setdefault("versions", {})
    v = versions.get(phase, 0) + 1
    versions[phase] = v
    write_project_meta(project_root, meta)
    return v


def current_version(project_root: Path, phase: str) -> int:
    """Get the current (latest) version number for a phase. Returns 0 if none."""
    meta = read_project_meta(project_root)
    return meta.get("versions", {}).get(phase, 0)


def all_versions(project_root: Path) -> dict[str, int]:
    """Get all version counters."""
    meta = read_project_meta(project_root)
    return meta.get("versions", {})


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------


def _maybe_migrate_legacy(project_root: Path):
    """One-time migration: scan existing versioned files and create .meta.json sidecars.

    Called lazily on first list_artifacts() or begin_version() call.
    Only runs if versioned files exist but no .meta.json sidecars are found.
    """
    meta = read_project_meta(project_root)
    if meta.get("versions_migrated"):
        return

    storyboard_dir = project_root / "storyboard"
    has_versioned_files = False
    has_meta_files = False

    if storyboard_dir.exists():
        for f in storyboard_dir.iterdir():
            if f.suffix == ".json" and not f.name.endswith(".meta.json"):
                if re.search(r"_v\d+\.json$", f.name):
                    has_versioned_files = True
            if f.name.endswith(".meta.json"):
                has_meta_files = True

    if not has_versioned_files or has_meta_files:
        # Nothing to migrate, or already migrated
        meta["versions_migrated"] = True
        write_project_meta(project_root, meta)
        return

    _migrate_legacy_versions(project_root)
    meta = read_project_meta(project_root)
    meta["versions_migrated"] = True
    write_project_meta(project_root, meta)


def _migrate_legacy_versions(project_root: Path):
    """Scan existing versioned files and create .meta.json sidecars retroactively."""

    # Migrate storyboard files (Phase 2 and Phase 3)
    storyboard_dir = project_root / "storyboard"
    if storyboard_dir.exists():
        for f in storyboard_dir.glob("editorial_*_v*.json"):
            if f.name.endswith(".meta.json"):
                continue
            _create_legacy_sidecar(f, "storyboard")
        for f in storyboard_dir.glob("monologue_*_v*.json"):
            if f.name.endswith(".meta.json"):
                continue
            _create_legacy_sidecar(f, "monologue")

    # Migrate per-clip reviews (Phase 1)
    clips_dir = project_root / "clips"
    if clips_dir.exists():
        for clip_dir in clips_dir.iterdir():
            if not clip_dir.is_dir():
                continue
            review_dir = clip_dir / "review"
            if not review_dir.exists():
                continue
            for f in review_dir.glob("review_*_v*.json"):
                if f.name.endswith(".meta.json"):
                    continue
                _create_legacy_sidecar(f, "review", clip_id=clip_dir.name)


def _create_legacy_sidecar(file_path: Path, phase: str, clip_id: str | None = None):
    """Create a .meta.json sidecar for an existing versioned file."""
    m = re.search(r"_(\w+)_v(\d+)\.json$", file_path.name)
    if not m:
        return

    provider = m.group(1)
    version = int(m.group(2))
    artifact_id = _build_artifact_id(phase, provider, version, clip_id)

    # Use file mtime as creation timestamp
    try:
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        mtime = datetime.now(timezone.utc)

    meta = ArtifactMeta(
        artifact_id=artifact_id,
        phase=phase,
        provider=provider,
        version=version,
        status="complete",
        created_at=mtime.isoformat(),
        completed_at=mtime.isoformat(),
        inputs={},  # unknown lineage for legacy artifacts
        clip_id=clip_id,
    )

    sidecar = _sidecar_path_for(file_path)
    if not sidecar.exists():
        sidecar.write_text(meta.model_dump_json(indent=2))
