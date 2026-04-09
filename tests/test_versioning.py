"""Unit tests for versioning.py — two-phase commit protocol and path helpers."""

from ai_video_editor.versioning import (
    begin_version,
    build_lineage_id,
    commit_version,
    fail_version,
    list_versions,
    read_project_meta,
    update_latest_symlink,
    versioned_dir,
    versioned_path,
    write_project_meta,
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestVersionedPath:
    def test_adds_version_suffix(self, tmp_path):
        base = tmp_path / "review_gemini.json"
        assert versioned_path(base, 3).name == "review_gemini_v3.json"

    def test_preserves_extension(self, tmp_path):
        base = tmp_path / "storyboard.md"
        assert versioned_path(base, 1).name == "storyboard_v1.md"


class TestVersionedDir:
    def test_creates_versioned_subdir(self, tmp_path):
        vdir = versioned_dir(tmp_path / "exports", 2)
        assert vdir.name == "v2"
        assert vdir.exists()


class TestUpdateLatestSymlink:
    def test_creates_symlink_for_file(self, tmp_path):
        target = tmp_path / "review_gemini_v2.json"
        target.write_text("{}")
        link = update_latest_symlink(target)
        assert link.name == "review_gemini_latest.json"
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_replaces_existing_symlink(self, tmp_path):
        v1 = tmp_path / "review_gemini_v1.json"
        v2 = tmp_path / "review_gemini_v2.json"
        v1.write_text("{}")
        v2.write_text("{}")
        update_latest_symlink(v1)
        link = update_latest_symlink(v2)
        assert link.resolve() == v2.resolve()


class TestListVersions:
    def test_finds_versioned_files(self, tmp_path):
        (tmp_path / "review_gemini_v1.json").write_text("{}")
        (tmp_path / "review_gemini_v2.json").write_text("{}")
        (tmp_path / "review_gemini_latest.json").symlink_to("review_gemini_v2.json")
        versions = list_versions(tmp_path, "review_gemini_v*.json")
        assert len(versions) == 2
        assert versions[0][0] == 1
        assert versions[1][0] == 2

    def test_empty_directory(self, tmp_path):
        assert list_versions(tmp_path, "review_v*.json") == []

    def test_nonexistent_directory(self, tmp_path):
        assert list_versions(tmp_path / "nope", "review_v*.json") == []


# ---------------------------------------------------------------------------
# Project metadata
# ---------------------------------------------------------------------------


class TestProjectMeta:
    def test_read_write_roundtrip(self, tmp_path):
        write_project_meta(tmp_path, {"name": "test", "versions": {"review": 3}})
        meta = read_project_meta(tmp_path)
        assert meta["name"] == "test"
        assert meta["versions"]["review"] == 3

    def test_read_missing_returns_empty(self, tmp_path):
        assert read_project_meta(tmp_path) == {}


# ---------------------------------------------------------------------------
# Lineage IDs
# ---------------------------------------------------------------------------


class TestBuildLineageId:
    def test_root_node(self):
        assert build_lineage_id("quick_scan", 1) == "sc.1"

    def test_with_parent(self):
        assert build_lineage_id("storyboard", 3, "rv.1") == "sb:rv1.3"

    def test_monologue_from_storyboard(self):
        assert build_lineage_id("monologue", 1, "sb.3") == "mn:sb3.1"

    def test_unknown_phase_uses_raw(self):
        assert build_lineage_id("custom", 2) == "custom.2"


# ---------------------------------------------------------------------------
# Two-phase commit
# ---------------------------------------------------------------------------


class TestBeginVersion:
    def test_creates_pending_sidecar(self, tmp_path):
        target_dir = tmp_path / "storyboard"
        target_dir.mkdir()
        # Create project.json
        write_project_meta(tmp_path, {"name": "test"})

        meta = begin_version(
            tmp_path,
            phase="storyboard",
            provider="gemini",
            config_snapshot={"model": "test"},
            target_dir=target_dir,
        )

        assert meta.status == "pending"
        assert meta.version == 1
        assert meta.phase == "storyboard"
        assert meta.provider == "gemini"

        # Pending sidecar should exist
        pending = target_dir / ".pending_storyboard_gemini_v1.meta.json"
        assert pending.exists()

    def test_increments_version(self, tmp_path):
        target_dir = tmp_path / "storyboard"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        begin_version(tmp_path, "storyboard", "gemini", target_dir=target_dir)
        # Simulate commit by writing a versioned file
        (target_dir / "editorial_gemini_v1.json").write_text("{}")
        # Rename pending to final sidecar
        pending = target_dir / ".pending_storyboard_gemini_v1.meta.json"
        final = target_dir / "editorial_gemini_v1.meta.json"
        pending.rename(final)

        meta2 = begin_version(tmp_path, "storyboard", "gemini", target_dir=target_dir)
        assert meta2.version == 2


class TestCommitVersion:
    def test_marks_complete_and_updates_project(self, tmp_path):
        target_dir = tmp_path / "storyboard"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        meta = begin_version(tmp_path, "storyboard", "gemini", target_dir=target_dir)
        output = target_dir / "editorial_gemini_v1.json"
        output.write_text('{"title": "test"}')

        committed = commit_version(tmp_path, meta, [output], target_dir=target_dir)

        assert committed.status == "complete"
        assert committed.completed_at is not None

        # Pending sidecar removed
        pending = target_dir / ".pending_storyboard_gemini_v1.meta.json"
        assert not pending.exists()

        # Final sidecar written
        sidecar = target_dir / "editorial_gemini_v1.meta.json"
        assert sidecar.exists()

        # project.json updated
        proj = read_project_meta(tmp_path)
        assert proj["versions"]["storyboard"] >= 1

    def test_creates_latest_symlink(self, tmp_path):
        target_dir = tmp_path / "storyboard"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        meta = begin_version(tmp_path, "storyboard", "gemini", target_dir=target_dir)
        output = target_dir / "editorial_gemini_v1.json"
        output.write_text("{}")

        commit_version(tmp_path, meta, [output], target_dir=target_dir)

        latest = target_dir / "editorial_gemini_latest.json"
        assert latest.is_symlink()


class TestFailVersion:
    def test_marks_failed_no_symlink(self, tmp_path):
        target_dir = tmp_path / "storyboard"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        meta = begin_version(tmp_path, "storyboard", "gemini", target_dir=target_dir)
        fail_version(tmp_path, meta, "API error", target_dir=target_dir)

        # fail_version mutates meta in-place
        assert meta.status == "failed"
        assert meta.error == "API error"

        # No latest symlink created
        latest = target_dir / "editorial_gemini_latest.json"
        assert not latest.exists()

        # Pending sidecar removed
        pending = target_dir / ".pending_storyboard_gemini_v1.meta.json"
        assert not pending.exists()


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_begin_commit_begin_increments(self, tmp_path):
        """Two successful versions should produce v1 and v2."""
        target_dir = tmp_path / "review"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        # Version 1
        m1 = begin_version(tmp_path, "review", "gemini", target_dir=target_dir)
        out1 = target_dir / "review_gemini_v1.json"
        out1.write_text("{}")
        commit_version(tmp_path, m1, [out1], target_dir=target_dir)

        # Version 2
        m2 = begin_version(tmp_path, "review", "gemini", target_dir=target_dir)
        assert m2.version == 2
        out2 = target_dir / "review_gemini_v2.json"
        out2.write_text("{}")
        commit_version(tmp_path, m2, [out2], target_dir=target_dir)

        # Latest points to v2
        latest = target_dir / "review_gemini_latest.json"
        assert latest.is_symlink()
        assert "v2" in str(latest.resolve())

    def test_failed_version_doesnt_increment_counter(self, tmp_path):
        """A failed version should not prevent the next version from using the same number."""
        target_dir = tmp_path / "review"
        target_dir.mkdir()
        write_project_meta(tmp_path, {"name": "test"})

        # Begin and fail v1
        m1 = begin_version(tmp_path, "review", "gemini", target_dir=target_dir)
        assert m1.version == 1
        fail_version(tmp_path, m1, "timeout", target_dir=target_dir)

        # project.json should NOT have been updated
        proj = read_project_meta(tmp_path)
        assert proj.get("versions", {}).get("review", 0) == 0
