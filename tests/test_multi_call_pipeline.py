"""Tests for the multi-call Phase 2 and Phase 3 pipeline implementation.

Uses real data from the family-hiking-in-Shipai project. Tests cover:
- Pre-processing: cast extraction, clip condensing, transcript trimming
- Prompt building: all multi-call prompts for Phase 2 and Phase 3
- Constraint formatting: new format_context_for_prompt()
- Deterministic validation: monologue overlay auto-fix
- Integration: full prompt chain dry-run (no API calls)

Run: uv run pytest tests/test_multi_call_pipeline.py -v
"""

import json
from pathlib import Path

import pytest

# Project root and test data paths
PROJECT_ROOT = Path(__file__).parent.parent
LIBRARY = PROJECT_ROOT / "library" / "family-hiking-in-Shipai"

# Skip all tests if the project data isn't available
pytestmark = pytest.mark.skipif(
    not LIBRARY.exists(),
    reason="family-hiking-in-Shipai project data not available",
)


# ---------------------------------------------------------------------------
# Fixtures: load real project data once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def user_context():
    return json.loads((LIBRARY / "user_context_latest.json").read_text())


@pytest.fixture(scope="module")
def clip_reviews():
    reviews = []
    clips_dir = LIBRARY / "clips"
    for clip_dir in sorted(clips_dir.iterdir()):
        rf = clip_dir / "review" / "review_gemini_latest.json"
        if rf.exists():
            reviews.append(json.loads(rf.read_text()))
    return reviews


@pytest.fixture(scope="module")
def storyboard():
    from ai_video_editor.models import EditorialStoryboard

    sb_path = LIBRARY / "storyboard" / "editorial_gemini_latest.json"
    return EditorialStoryboard.model_validate_json(sb_path.read_text())


@pytest.fixture(scope="module")
def transcripts(clip_reviews):
    """Load formatted transcripts for all clips (simulating _load_all_transcripts_for_prompt)."""
    result = {}
    for r in clip_reviews:
        cid = r.get("clip_id", "")
        clip_dir = LIBRARY / "clips" / cid / "audio"
        vtt = clip_dir / "transcript.vtt"
        if vtt.exists():
            result[cid] = vtt.read_text()[:2000]
    return result


# ---------------------------------------------------------------------------
# Phase 1: Constraint formatting
# ---------------------------------------------------------------------------


class TestConstraintFormatting:
    def test_constraints_and_preferences_separated(self, user_context):
        from ai_video_editor.briefing import format_context_for_prompt

        output = format_context_for_prompt(user_context)

        assert "FILMMAKER CONSTRAINTS" in output
        assert "non-negotiable" in output
        assert "MUST INCLUDE" in output
        assert "FILMMAKER PREFERENCES" in output
        assert "MUST explain why" in output

    def test_highlights_become_must_include(self, user_context):
        from ai_video_editor.briefing import format_context_for_prompt

        output = format_context_for_prompt(user_context)

        # The highlights text should appear after "MUST INCLUDE:"
        constraints_section = output.split("FILMMAKER PREFERENCES")[0]
        assert "MUST INCLUDE:" in constraints_section
        assert "軍艦岩" in constraints_section  # part of the highlights

    def test_tone_is_preference_not_constraint(self, user_context):
        from ai_video_editor.briefing import format_context_for_prompt

        output = format_context_for_prompt(user_context)

        preferences_section = output.split("FILMMAKER PREFERENCES")[1]
        assert "Warm and nostalgic" in preferences_section

    def test_empty_context_returns_empty(self):
        from ai_video_editor.briefing import format_context_for_prompt

        assert format_context_for_prompt({}) == ""
        assert format_context_for_prompt(None) == ""

    def test_no_avoid_omits_must_exclude(self, user_context):
        from ai_video_editor.briefing import format_context_for_prompt

        # This project has no "avoid" key
        output = format_context_for_prompt(user_context)
        assert "MUST EXCLUDE" not in output

    def test_context_qa_in_preferences(self, user_context):
        from ai_video_editor.briefing import format_context_for_prompt

        output = format_context_for_prompt(user_context)
        preferences_section = output.split("FILMMAKER PREFERENCES")[1]
        assert "軍艦岩" in preferences_section  # from context_qa answer


# ---------------------------------------------------------------------------
# Phase 2: Pre-processing functions
# ---------------------------------------------------------------------------


class TestCastExtraction:
    def test_deduplicates_people(self, clip_reviews):
        from ai_video_editor.editorial_prompts import extract_cast_from_reviews

        cast = extract_cast_from_reviews(clip_reviews)

        # Should have far fewer entries than total people across all reviews
        total_people = sum(len(r.get("people", [])) for r in clip_reviews)
        assert len(cast) < total_people
        assert len(cast) > 0

    def test_cast_tracks_appearances(self, clip_reviews):
        from ai_video_editor.editorial_prompts import extract_cast_from_reviews

        cast = extract_cast_from_reviews(clip_reviews)

        for person in cast:
            assert "label" in person
            assert "appears_in" in person
            assert isinstance(person["appears_in"], list)
            assert len(person["appears_in"]) > 0

    def test_cast_has_descriptions(self, clip_reviews):
        from ai_video_editor.editorial_prompts import extract_cast_from_reviews

        cast = extract_cast_from_reviews(clip_reviews)

        for person in cast:
            assert person.get("description"), f"Missing description for {person['label']}"


class TestClipCondensing:
    def test_condensed_has_required_fields(self, clip_reviews):
        from ai_video_editor.editorial_prompts import condense_clip_for_planning

        condensed = condense_clip_for_planning(clip_reviews[0])

        assert "clip_id" in condensed
        assert "total_usable_sec" in condensed
        assert "content_type" in condensed
        assert "usable_segments" in condensed
        assert "key_moments" in condensed
        assert "has_speech" in condensed

    def test_usable_segments_have_indices(self, clip_reviews):
        from ai_video_editor.editorial_prompts import condense_clip_for_planning

        condensed = condense_clip_for_planning(clip_reviews[0])

        for seg in condensed["usable_segments"]:
            assert "index" in seg
            assert "in_sec" in seg
            assert "out_sec" in seg
            assert "description" in seg

    def test_condensed_is_smaller_than_original(self, clip_reviews):
        from ai_video_editor.editorial_prompts import condense_clip_for_planning

        original = json.dumps(clip_reviews[0])
        condensed = json.dumps(condense_clip_for_planning(clip_reviews[0]))

        # Condensed should be significantly smaller (no people arrays, no editorial notes)
        assert len(condensed) < len(original)

    def test_all_clips_condense(self, clip_reviews):
        from ai_video_editor.editorial_prompts import condense_clip_for_planning

        for r in clip_reviews:
            c = condense_clip_for_planning(r)
            assert c["clip_id"] == r["clip_id"]


class TestTranscriptTrimming:
    def test_keeps_lines_within_usable_range(self):
        from ai_video_editor.editorial_prompts import trim_transcript_to_usable

        transcript = "[0:05] Hello world\n[0:15] Middle part\n[0:45] End part\n"
        usable = [{"in_sec": 0, "out_sec": 20}]

        trimmed = trim_transcript_to_usable(transcript, usable)

        assert "[0:05]" in trimmed
        assert "[0:15]" in trimmed
        assert "[0:45]" not in trimmed

    def test_keeps_header_lines(self):
        from ai_video_editor.editorial_prompts import trim_transcript_to_usable

        transcript = "WEBVTT\n\n[0:05] Hello\n[5:00] Far away\n"
        usable = [{"in_sec": 0, "out_sec": 10}]

        trimmed = trim_transcript_to_usable(transcript, usable)

        assert "WEBVTT" in trimmed
        assert "[0:05]" in trimmed
        assert "[5:00]" not in trimmed

    def test_handles_empty_inputs(self):
        from ai_video_editor.editorial_prompts import trim_transcript_to_usable

        assert trim_transcript_to_usable("", []) == ""
        assert trim_transcript_to_usable(None, []) == ""


# ---------------------------------------------------------------------------
# Phase 2: Prompt building
# ---------------------------------------------------------------------------


class TestPhase2APrompt:
    def test_builds_reasoning_prompt(self, clip_reviews, user_context):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )
        from ai_video_editor.briefing import format_context_for_prompt

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]
        ctx_text = format_context_for_prompt(user_context)

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
            user_context_text=ctx_text,
        )

        assert "CONSTRAINT CHECK" in prompt
        assert "SEGMENT SEQUENCE" in prompt
        assert "DISCARDED CLIPS" in prompt
        assert "MUST INCLUDE" in prompt  # from constraint formatting
        assert "Think freely" in prompt
        # Should contain all clip IDs
        for r in clip_reviews[:3]:
            assert r["clip_id"] in prompt

    def test_prompt_contains_cast(self, clip_reviews, user_context):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
        )

        assert "Cast (deduplicated across all clips)" in prompt

    def test_prompt_size_is_reasonable(self, clip_reviews, user_context):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )
        from ai_video_editor.briefing import format_context_for_prompt

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]
        ctx_text = format_context_for_prompt(user_context)

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
            user_context_text=ctx_text,
        )

        # Should be significantly smaller than the old single-call prompt
        # Old prompt was ~30K chars for 17 clips; this should be ~16K or less
        # With 31 clips it'll be larger but still under the raw review size
        total_review_size = sum(len(json.dumps(r)) for r in clip_reviews)
        assert len(prompt) < total_review_size, (
            f"Condensed prompt ({len(prompt)} chars) should be smaller "
            f"than raw reviews ({total_review_size} chars)"
        )


class TestPhase2AStructuringPrompt:
    def test_builds_structuring_prompt(self):
        from ai_video_editor.editorial_prompts import build_phase2a_structuring_prompt

        plan_text = "I plan to use clip C0003 segment 0 as the hook..."
        clip_ids = ["20260315162915_C0003", "20260315162955_C0004"]

        prompt = build_phase2a_structuring_prompt(plan_text, clip_ids)

        assert "faithful translation" in prompt.lower()
        assert plan_text in prompt
        assert "20260315162915_C0003" in prompt


class TestPhase2BPrompt:
    def test_builds_assembly_prompt_with_bounded_windows(self, clip_reviews):
        from ai_video_editor.editorial_prompts import build_phase2b_assembly_prompt
        from ai_video_editor.models import (
            StoryPlan, PlannedSegment, CastMember, StoryArcSection, DiscardedClip,
        )

        # Build a minimal StoryPlan from real data
        first_review = clip_reviews[0]
        cid = first_review["clip_id"]
        plan = StoryPlan(
            title="Test Hike",
            style="vlog",
            story_concept="A family hike",
            cast=[CastMember(name="Max", description="filmmaker", role="main_subject", appears_in=[cid])],
            story_arc=[StoryArcSection(title="Opening", description="Start the story")],
            planned_segments=[
                PlannedSegment(
                    clip_id=cid,
                    usable_segment_index=0,
                    purpose="hook",
                    arc_phase="opening_context",
                    narrative_role="Opening shot of the trail",
                    audio_strategy="ambient_only",
                    is_speech_segment=False,
                ),
            ],
            discarded=[DiscardedClip(clip_id="fake_clip", reason="not needed")],
        )

        prompt = build_phase2b_assembly_prompt(
            story_plan=plan,
            clip_reviews=[first_review],
            style="vlog",
        )

        # Must contain bounded timestamp windows
        assert "Usable range:" in prompt
        assert "HARD CONSTRAINTS" in prompt
        assert cid in prompt
        assert "Select in_sec and out_sec" in prompt

    def test_assembly_prompt_has_transcript(self, clip_reviews, transcripts):
        from ai_video_editor.editorial_prompts import build_phase2b_assembly_prompt
        from ai_video_editor.models import StoryPlan, PlannedSegment

        first_review = clip_reviews[0]
        cid = first_review["clip_id"]
        plan = StoryPlan(
            title="Test",
            style="vlog",
            story_concept="Test",
            planned_segments=[
                PlannedSegment(
                    clip_id=cid,
                    usable_segment_index=0,
                    purpose="hook",
                    arc_phase="opening_context",
                    narrative_role="Test",
                    audio_strategy="preserve_dialogue",
                    is_speech_segment=True,
                ),
            ],
        )

        prompt = build_phase2b_assembly_prompt(
            story_plan=plan,
            clip_reviews=[first_review],
            transcripts=transcripts,
        )

        # Transcript should be inlined if available for this clip
        if cid in transcripts:
            assert "Transcript:" in prompt


# ---------------------------------------------------------------------------
# Phase 3: Monologue multi-call
# ---------------------------------------------------------------------------


class TestMonologueCall1Prompt:
    def test_builds_analysis_prompt(self, storyboard, transcripts, user_context):
        from ai_video_editor.editorial_prompts import build_monologue_call1_prompt
        from ai_video_editor.briefing import format_context_for_prompt

        prompt = build_monologue_call1_prompt(
            storyboard=storyboard,
            transcripts=transcripts,
            user_context_text=format_context_for_prompt(user_context),
        )

        assert "ELIGIBLE" in prompt
        assert "NOT ELIGIBLE" in prompt
        assert "arc_phase" in prompt
        assert "OverlayPlan" in prompt
        # Should contain all segment indices
        for seg in storyboard.segments[:3]:
            assert f"[{seg.index}]" in prompt


class TestMonologueCall2Prompt:
    def test_builds_creative_prompt(self, storyboard):
        from ai_video_editor.editorial_prompts import build_monologue_call2_prompt
        from ai_video_editor.models import OverlayPlan, EligibleSegment

        plan = OverlayPlan(
            persona_recommendation="conversational_confidant",
            persona_rationale="Warm tone matches the family hike footage",
            eligible_segments=[
                EligibleSegment(
                    segment_index=0,
                    segment_duration_sec=8.0,
                    arc_phase="grounding_hook",
                    intent="Welcome the viewer, establish the trail",
                    max_overlay_count=2,
                ),
            ],
        )

        prompt = build_monologue_call2_prompt(
            overlay_plan=plan,
            storyboard=storyboard,
        )

        assert "conversational_confidant" in prompt
        assert "lowercase" in prompt.lower()
        assert "5-8 words" in prompt
        assert "OverlayDrafts" in prompt
        assert "Duration: 8.0s" in prompt

    def test_persona_hint_overrides(self, storyboard):
        from ai_video_editor.editorial_prompts import build_monologue_call2_prompt
        from ai_video_editor.models import OverlayPlan, EligibleSegment

        plan = OverlayPlan(
            persona_recommendation="conversational_confidant",
            persona_rationale="Default choice",
            eligible_segments=[
                EligibleSegment(
                    segment_index=0,
                    segment_duration_sec=8.0,
                    arc_phase="grounding_hook",
                    intent="Test",
                    max_overlay_count=1,
                ),
            ],
        )

        prompt = build_monologue_call2_prompt(
            overlay_plan=plan,
            storyboard=storyboard,
            persona_hint="detached_observer",
        )

        assert "detached_observer" in prompt


class TestMonologueValidation:
    def test_clamps_timing_to_segment(self):
        from ai_video_editor.editorial_prompts import validate_monologue_overlays
        from ai_video_editor.models import OverlayDraft, EligibleSegment

        overlays = [
            OverlayDraft(
                segment_index=0,
                text="hello... this is a test",
                appear_at=5.0,
                duration_sec=6.0,  # exceeds 8.0s boundary
                word_count=5,
                arc_phase="grounding_hook",
            ),
        ]
        eligible = [
            EligibleSegment(
                segment_index=0,
                segment_duration_sec=8.0,
                arc_phase="grounding_hook",
                intent="Test",
            ),
        ]

        fixed, log = validate_monologue_overlays(overlays, eligible, [])

        assert len(fixed) == 1
        assert fixed[0].appear_at + fixed[0].duration_sec <= 8.0
        assert any("clamped" in entry for entry in log)

    def test_enforces_lowercase(self):
        from ai_video_editor.editorial_prompts import validate_monologue_overlays
        from ai_video_editor.models import OverlayDraft, EligibleSegment

        overlays = [
            OverlayDraft(
                segment_index=0,
                text="Hello World Test",
                appear_at=0.0,
                duration_sec=3.0,
                word_count=3,
                arc_phase="grounding_hook",
            ),
        ]
        eligible = [
            EligibleSegment(
                segment_index=0,
                segment_duration_sec=10.0,
                arc_phase="grounding_hook",
                intent="Test",
            ),
        ]

        fixed, log = validate_monologue_overlays(overlays, eligible, [])

        assert fixed[0].text == "hello world test"
        assert any("Lowercased" in entry for entry in log)

    def test_removes_ineligible_segment_overlays(self):
        from ai_video_editor.editorial_prompts import validate_monologue_overlays
        from ai_video_editor.models import OverlayDraft, EligibleSegment

        overlays = [
            OverlayDraft(
                segment_index=5,  # not in eligible list
                text="this should be removed",
                appear_at=0.0,
                duration_sec=3.0,
                word_count=4,
            ),
        ]
        eligible = [
            EligibleSegment(
                segment_index=0,
                segment_duration_sec=10.0,
                arc_phase="grounding_hook",
                intent="Test",
            ),
        ]

        fixed, log = validate_monologue_overlays(overlays, eligible, [])

        assert len(fixed) == 0
        assert any("non-eligible" in entry for entry in log)

    def test_enforces_two_breath_rule(self):
        from ai_video_editor.editorial_prompts import validate_monologue_overlays
        from ai_video_editor.models import OverlayDraft, EligibleSegment

        overlays = [
            OverlayDraft(
                segment_index=0,
                text="one two three four five six",  # 6 words, min = 2.4s
                appear_at=0.0,
                duration_sec=1.0,  # too short
                word_count=6,
            ),
        ]
        eligible = [
            EligibleSegment(
                segment_index=0,
                segment_duration_sec=10.0,
                arc_phase="grounding_hook",
                intent="Test",
            ),
        ]

        fixed, log = validate_monologue_overlays(overlays, eligible, [])

        assert fixed[0].duration_sec >= 6 * 0.4  # two-breath minimum
        assert any("below minimum" in entry for entry in log)

    def test_recalculates_word_count(self):
        from ai_video_editor.editorial_prompts import validate_monologue_overlays
        from ai_video_editor.models import OverlayDraft, EligibleSegment

        overlays = [
            OverlayDraft(
                segment_index=0,
                text="hey come along today",
                appear_at=0.0,
                duration_sec=3.0,
                word_count=99,  # wrong
            ),
        ]
        eligible = [
            EligibleSegment(
                segment_index=0,
                segment_duration_sec=10.0,
                arc_phase="grounding_hook",
                intent="Test",
            ),
        ]

        fixed, _ = validate_monologue_overlays(overlays, eligible, [])

        assert fixed[0].word_count == 4


# ---------------------------------------------------------------------------
# Integration: editorial_reasoning field description
# ---------------------------------------------------------------------------


class TestModelFieldDescriptions:
    def test_editorial_reasoning_requires_constraint_check(self):
        from ai_video_editor.models import EditorialStoryboard

        desc = EditorialStoryboard.model_fields["editorial_reasoning"].description

        assert "CONSTRAINT CHECK" in desc
        assert "MUST-INCLUDE" in desc
        assert "MUST-EXCLUDE" in desc
        assert "explain why" in desc.lower()

    def test_storyplan_has_constraint_satisfaction(self):
        from ai_video_editor.models import StoryPlan

        assert "constraint_satisfaction" in StoryPlan.model_fields

    def test_planned_segment_uses_index_not_timestamps(self):
        from ai_video_editor.models import PlannedSegment

        fields = PlannedSegment.model_fields
        assert "usable_segment_index" in fields
        assert "in_sec" not in fields
        assert "out_sec" not in fields


# ---------------------------------------------------------------------------
# Integration: config defaults
# ---------------------------------------------------------------------------


class TestConfig:
    def test_temperature_defaults(self):
        from ai_video_editor.config import GeminiConfig, ClaudeConfig

        g = GeminiConfig()
        c = ClaudeConfig()

        assert g.phase2_temperature == 0.6
        assert c.phase2_temperature == 0.6
        assert g.phase2b_temperature == 0.3

    def test_split_pipeline_off_by_default(self):
        from ai_video_editor.config import GeminiConfig

        assert GeminiConfig().use_split_pipeline is False

    def test_structuring_model_configured(self):
        from ai_video_editor.config import GeminiConfig

        assert GeminiConfig().structuring_model == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# Integration: full prompt chain dry-run
# ---------------------------------------------------------------------------


class TestFullPromptChain:
    """End-to-end test building all prompts from real project data (no API calls)."""

    def test_phase2_full_chain(self, clip_reviews, user_context, transcripts):
        """Build Call 2A → 2A.5 → 2B prompts from real data, verify no errors."""
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            trim_transcript_to_usable,
            build_phase2a_reasoning_prompt,
            build_phase2a_structuring_prompt,
            build_phase2b_assembly_prompt,
        )
        from ai_video_editor.briefing import format_context_for_prompt
        from ai_video_editor.models import (
            StoryPlan, PlannedSegment, CastMember, StoryArcSection,
        )

        # Pre-process
        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]
        trimmed = {}
        for r in clip_reviews:
            cid = r["clip_id"]
            if cid in transcripts:
                trimmed[cid] = trim_transcript_to_usable(
                    transcripts[cid], r.get("usable_segments", [])
                )
        ctx_text = format_context_for_prompt(user_context)

        # Call 2A prompt
        prompt_2a = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
            transcripts=trimmed,
            user_context_text=ctx_text,
        )
        assert len(prompt_2a) > 1000

        # Simulate Call 2A output → Call 2A.5 prompt
        fake_plan_text = "I will use clip C0003 segment 0 as the hook..."
        clip_ids = [c["clip_id"] for c in condensed]
        prompt_2a5 = build_phase2a_structuring_prompt(fake_plan_text, clip_ids)
        assert len(prompt_2a5) > 100

        # Simulate Call 2A.5 output → Call 2B prompt
        first_cid = clip_reviews[0]["clip_id"]
        story_plan = StoryPlan(
            title="Family Hike at 軍艦岩",
            style="vlog",
            story_concept="A warm family outing",
            cast=[CastMember(name="Max", description="filmmaker", role="main_subject", appears_in=[first_cid])],
            story_arc=[StoryArcSection(title="Opening", description="Start")],
            planned_segments=[
                PlannedSegment(
                    clip_id=first_cid,
                    usable_segment_index=0,
                    purpose="hook",
                    arc_phase="opening_context",
                    narrative_role="Opening shot",
                    audio_strategy="ambient_only",
                    is_speech_segment=False,
                ),
            ],
        )
        prompt_2b = build_phase2b_assembly_prompt(
            story_plan=story_plan,
            clip_reviews=[clip_reviews[0]],
            transcripts=transcripts,
            style="vlog",
        )
        assert "Usable range:" in prompt_2b
        assert first_cid in prompt_2b

        # Verify total prompt sizes are reasonable
        print(f"\n  Prompt sizes (31-clip project):")
        print(f"    Call 2A:   {len(prompt_2a):,} chars (~{len(prompt_2a) // 4:,} tokens)")
        print(f"    Call 2A.5: {len(prompt_2a5):,} chars")
        print(f"    Call 2B:   {len(prompt_2b):,} chars (~{len(prompt_2b) // 4:,} tokens)")


# ---------------------------------------------------------------------------
# Phase 5: Context compression, constraint resolution, few-shot
# ---------------------------------------------------------------------------


class TestTieredContextCompression:
    def test_classify_clip_priority(self, clip_reviews):
        from ai_video_editor.editorial_prompts import classify_clip_priority

        priorities = [classify_clip_priority(r) for r in clip_reviews]

        # Should have a mix of priorities in a real 31-clip project
        assert "high" in priorities
        assert len(set(priorities)) >= 2, "Expected at least 2 priority levels"

    def test_tiered_is_smaller_than_full(self, clip_reviews, transcripts):
        from ai_video_editor.editorial_prompts import _format_clip_reviews_text

        full = _format_clip_reviews_text(clip_reviews, transcripts, tiered=False)
        tiered = _format_clip_reviews_text(clip_reviews, transcripts, tiered=True)

        # With 31 clips (>= 15 threshold), tiered should be smaller
        assert len(tiered) < len(full), (
            f"Tiered ({len(tiered)}) should be smaller than full ({len(full)})"
        )
        savings_pct = (1 - len(tiered) / len(full)) * 100
        print(f"\n  Tiered compression: {len(full):,} → {len(tiered):,} chars ({savings_pct:.0f}% savings)")

    def test_tiered_disabled_below_threshold(self):
        from ai_video_editor.editorial_prompts import _format_clip_reviews_text

        # With < 15 clips, tiered=True should produce same output as tiered=False
        small_reviews = [{"clip_id": f"clip_{i}", "summary": "test"} for i in range(5)]

        full = _format_clip_reviews_text(small_reviews, tiered=False)
        tiered = _format_clip_reviews_text(small_reviews, tiered=True)

        assert full == tiered

    def test_high_priority_clips_have_full_detail(self, clip_reviews, transcripts):
        from ai_video_editor.editorial_prompts import (
            _format_clip_reviews_text, classify_clip_priority,
        )

        tiered = _format_clip_reviews_text(clip_reviews, transcripts, tiered=True)

        # Find a high-priority clip and verify it has full detail markers
        for r in clip_reviews:
            if classify_clip_priority(r) == "high":
                cid = r["clip_id"]
                # High-priority clips should have "Usable segments:" section
                # Find the clip's block in the output
                if cid in tiered and "Usable segments:" in tiered.split(cid)[1].split("##")[0]:
                    break
        else:
            pytest.skip("No high-priority clip with usable segments found")


class TestConstraintResolution:
    def test_resolves_highlights_to_clips(self, user_context, clip_reviews):
        from ai_video_editor.editorial_prompts import resolve_constraints_to_clips

        resolved = resolve_constraints_to_clips(user_context, clip_reviews)

        # Should find matches for at least some highlight mentions
        assert resolved, "Expected constraint resolution to find matches"
        assert "Clip references" in resolved
        assert "→" in resolved  # match indicators

    def test_empty_context_returns_empty(self, clip_reviews):
        from ai_video_editor.editorial_prompts import resolve_constraints_to_clips

        assert resolve_constraints_to_clips({}, clip_reviews) == ""
        assert resolve_constraints_to_clips({"tone": "warm"}, clip_reviews) == ""

    def test_resolution_in_prompt(self, clip_reviews, user_context):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )
        from ai_video_editor.briefing import format_context_for_prompt

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]
        ctx_text = format_context_for_prompt(user_context)

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
            user_context_text=ctx_text,
            user_context=user_context,
        )

        assert "Clip references for filmmaker constraints" in prompt


class TestFewShotExample:
    def test_example_in_reasoning_prompt(self, clip_reviews, user_context):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
        )

        assert "<example>" in prompt
        assert "CONSTRAINT CHECK" in prompt.split("<example>")[1].split("</example>")[0]
        assert "MUST-INCLUDE" in prompt
        assert "MUST-EXCLUDE" in prompt

    def test_example_shows_segment_sequence(self, clip_reviews):
        from ai_video_editor.editorial_prompts import (
            extract_cast_from_reviews,
            condense_clip_for_planning,
            build_phase2a_reasoning_prompt,
        )

        cast = extract_cast_from_reviews(clip_reviews)
        condensed = [condense_clip_for_planning(r) for r in clip_reviews]

        prompt = build_phase2a_reasoning_prompt(
            clip_reviews=clip_reviews,
            style="vlog",
            total_duration_sec=600.0,
            cast=cast,
            condensed_clips=condensed,
        )

        example = prompt.split("<example>")[1].split("</example>")[0]
        assert "hook" in example
        assert "climax" in example
        assert "DISCARDED" in example


# ---------------------------------------------------------------------------
# Phase 6: Evaluation harness
# ---------------------------------------------------------------------------


class TestEvalScoring:
    """Test the evaluation scoring module against real storyboard data."""

    def test_score_storyboard_produces_report(self, storyboard, clip_reviews, user_context):
        from ai_video_editor.eval import score_storyboard

        report = score_storyboard(storyboard, clip_reviews, user_context)

        assert report.total_segments > 0
        assert report.total_clips_available == len(clip_reviews)
        assert report.clips_used > 0
        assert report.has_editorial_reasoning
        print(f"\n{report.summary()}")

    def test_constraint_satisfaction_scoring(self, storyboard, user_context):
        from ai_video_editor.eval import score_constraint_satisfaction

        results = score_constraint_satisfaction(storyboard, user_context)

        # Should have parsed multiple constraint phrases from highlights
        assert len(results) > 0
        for r in results:
            assert r.constraint_type == "must_include"  # this project has no "avoid"
            assert r.text
            assert r.evidence

        sat_count = sum(1 for r in results if r.satisfied)
        total = len(results)
        print(f"\n  Constraint satisfaction: {sat_count}/{total}")
        for r in results:
            status = "PASS" if r.satisfied else "FAIL"
            print(f"    [{status}] {r.text[:50]}... → {r.evidence[:60]}")

    def test_timestamp_precision_scoring(self, storyboard, clip_reviews):
        from ai_video_editor.eval import score_timestamp_precision

        total, valid, clamped, invalid = score_timestamp_precision(storyboard, clip_reviews)

        assert total > 0
        assert valid + clamped + invalid <= total
        assert invalid == 0, f"Found {invalid} segments with unknown clip IDs"

        print(f"\n  Timestamps: {valid}/{total} valid, {clamped} need clamping")

    def test_report_summary_is_printable(self, storyboard, clip_reviews, user_context):
        from ai_video_editor.eval import score_storyboard

        report = score_storyboard(storyboard, clip_reviews, user_context)
        summary = report.summary()

        assert "Storyboard Evaluation Report" in summary
        assert "Constraints:" in summary
        assert "Timestamps:" in summary
        assert "Structure:" in summary
        assert "Coverage:" in summary

    def test_eval_without_user_context(self, storyboard, clip_reviews):
        from ai_video_editor.eval import score_storyboard

        report = score_storyboard(storyboard, clip_reviews, user_context=None)

        # Should still produce a valid report, just no constraint results
        assert len(report.constraints) == 0
        assert report.constraint_satisfaction_rate() == 1.0
        assert report.total_segments > 0

    def test_constraint_rates(self, storyboard, clip_reviews, user_context):
        from ai_video_editor.eval import score_storyboard

        report = score_storyboard(storyboard, clip_reviews, user_context)

        sat_rate = report.constraint_satisfaction_rate()
        ts_rate = report.timestamp_precision_rate()

        assert 0.0 <= sat_rate <= 1.0
        assert 0.0 <= ts_rate <= 1.0

        print(f"\n  Constraint satisfaction rate: {sat_rate:.0%}")
        print(f"  Timestamp precision rate: {ts_rate:.0%}")


class TestEvalEdgeCases:
    def test_empty_storyboard(self, clip_reviews):
        from ai_video_editor.eval import score_storyboard
        from ai_video_editor.models import EditorialStoryboard

        empty = EditorialStoryboard(
            editorial_reasoning="",
            title="Empty",
            estimated_duration_sec=0,
            style="vlog",
            story_concept="Nothing",
            segments=[],
        )
        report = score_storyboard(empty, clip_reviews)

        assert report.total_segments == 0
        assert report.timestamp_precision_rate() == 1.0  # 0/0 = 1.0 by convention
        assert not report.has_editorial_reasoning

    def test_constraint_with_avoid(self):
        from ai_video_editor.eval import score_constraint_satisfaction
        from ai_video_editor.models import EditorialStoryboard, Segment

        sb = EditorialStoryboard(
            editorial_reasoning="test",
            title="Test",
            estimated_duration_sec=60,
            style="vlog",
            story_concept="test",
            segments=[
                Segment(
                    index=0,
                    clip_id="clip_001",
                    in_sec=0,
                    out_sec=10,
                    purpose="hook",
                    description="Walking on the bus ride through town",
                    transition="cut",
                ),
            ],
        )
        ctx = {"avoid": "bus ride footage"}

        results = score_constraint_satisfaction(sb, ctx)

        assert len(results) == 1
        assert results[0].constraint_type == "must_exclude"
        assert not results[0].satisfied  # "bus ride" appears in segment description

    def test_fuzzy_matching_quality(self):
        from ai_video_editor.eval import _fuzzy_match

        # Should match on keyword overlap
        assert _fuzzy_match("sunset at the temple", "golden sunset over the ancient temple")
        assert _fuzzy_match("climbing shot trail", "rock climbing section on the trail")

        # Should not match on single common word
        assert not _fuzzy_match("the view", "walking through the park")

        # Should handle short queries gracefully
        assert not _fuzzy_match("a", "something completely different")


class TestEvalComparison:
    """Test comparing two storyboard variants — the core evaluation use case."""

    def test_compare_reports(self, storyboard, clip_reviews, user_context):
        """Simulate comparing baseline vs improved pipeline."""
        from ai_video_editor.eval import score_storyboard

        # Score the existing (baseline) storyboard
        baseline = score_storyboard(storyboard, clip_reviews, user_context)

        # Compare key metrics (in a real evaluation, we'd have two different storyboards)
        print("\n  Pipeline Comparison (baseline only — split pipeline requires API call):")
        print(f"    Constraint satisfaction: {baseline.constraint_satisfaction_rate():.0%}")
        print(f"    Timestamp precision:     {baseline.timestamp_precision_rate():.0%}")
        print(f"    Segments:                {baseline.total_segments}")
        print(f"    Clips used:              {baseline.clips_used}/{baseline.total_clips_available}")
        print(f"    Reasoning quality:       {'mentions constraints' if baseline.reasoning_mentions_constraints else 'does NOT mention constraints'}")
        print(f"    Duration:                {baseline.estimated_duration_sec:.0f}s")
