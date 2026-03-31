"""Run versioning for LLM-generated artifacts.

Each LLM run (analyze, cut) increments a version counter stored in project.json.
Outputs are named with version suffixes, and a 'latest' symlink always points to
the most recent version.
"""

import json
import os
from pathlib import Path


def read_project_meta(project_root: Path) -> dict:
    meta_path = project_root / "project.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {}


def write_project_meta(project_root: Path, meta: dict):
    (project_root / "project.json").write_text(json.dumps(meta, indent=2))


def next_version(project_root: Path, phase: str) -> int:
    """Increment and return the next version number for a phase (analyze, cut, review)."""
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
            # foo_v2.md → foo_latest.md
            stem = target.stem
            # Remove _vN suffix to get base stem
            import re
            base_stem = re.sub(r"_v\d+$", "", stem)
            link = target.parent / f"{base_stem}_latest{target.suffix}"

    # Remove existing symlink or file
    if link.exists() or link.is_symlink():
        link.unlink()

    # Use relative path for portability
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
    import re
    results = []
    if not directory.exists():
        return results
    for f in directory.iterdir():
        m = re.match(pattern.replace("*", r"(\d+)"), f.name)
        if m:
            results.append((int(m.group(1)), f))
    return sorted(results)
