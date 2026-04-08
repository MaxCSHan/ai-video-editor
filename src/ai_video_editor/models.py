"""Pydantic models for the editorial storyboard — the single source of truth.

These models are used for:
1. Gemini structured output (response_schema)
2. Claude JSON parsing (model_validate_json)
3. Rendering to markdown and HTML
4. ffmpeg rough cut assembly
"""

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Transcript models (mlx-whisper local or Gemini structured output)
# ---------------------------------------------------------------------------


class TranscriptWord(BaseModel):
    word: str
    start: float
    end: float


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[TranscriptWord] = []
    speaker: str | None = None  # "Jinx", "Speaker_A", etc. (Gemini only)
    type: str = "speech"  # speech | music | sound_effect | silence


class Transcript(BaseModel):
    source_audio: str
    model: str
    language: str
    text: str
    segments: list[TranscriptSegment]
    duration_sec: float
    has_speech: bool
    speakers: list[str] = []  # unique speaker labels detected (Gemini only)
    provider: str = "mlx"  # "mlx" | "gemini"


# Dedicated Gemini response model — lean, no word-level detail, no mlx fields.
# Used as response_schema for Gemini structured output. Transformed into the
# canonical transcript.json format after receiving the response.


class GeminiTranscriptSegment(BaseModel):
    start: float = Field(description="Start time in seconds from clip start")
    end: float = Field(description="End time in seconds from clip start")
    text: str = Field(description="Transcribed text, or [inaudible] if speech is unclear")
    speaker: str | None = Field(
        default=None,
        description="Speaker name or label (Speaker_A, Speaker_B). None for non-speech segments.",
    )
    type: str = Field(default="speech", description="speech, music, sound_effect, or silence")


class GeminiTranscript(BaseModel):
    language: str = Field(description="Primary language detected (e.g. 'en', 'ja', 'zh')")
    segments: list[GeminiTranscriptSegment] = Field(
        description="Chronological list of audio segments"
    )
    speakers: list[str] = Field(default=[], description="All unique speaker labels detected")
    has_speech: bool = Field(description="Whether any human speech was detected")


# ---------------------------------------------------------------------------
# Quick scan model (for smart briefing — one LLM call across all clips)
# ---------------------------------------------------------------------------


class PersonSighting(BaseModel):
    description: str = Field(
        description="Detailed visual appearance: clothing, hair, build, distinguishing features"
    )
    estimated_appearances: int = Field(description="How many clips this person appears in")
    role_guess: str = Field(description="main_subject, companion, bystander, or camera_person")


class ClipSummary(BaseModel):
    clip_id: str = Field(description="Exact clip identifier matching the filename")
    summary: str = Field(description="One-line description of what happens in this clip")
    energy: str = Field(description="high, medium, or low")


class QuickScanResult(BaseModel):
    overall_summary: str = Field(description="2-3 sentences about the footage as a whole")
    people: list[PersonSighting] = Field(description="All distinct people observed across clips")
    activities: list[str] = Field(description="Activities, locations, and events observed")
    mood: str = Field(description="Overall mood and energy of the footage")
    suggested_questions: list[str] = Field(
        description="Specific questions to ask the filmmaker based on what you observed "
        "— focus on identifying people, relationships, and story context"
    )
    clip_summaries: list[ClipSummary] = Field(
        default=[], description="Per-clip one-line summary and energy level"
    )


# ---------------------------------------------------------------------------
# Creative brief models (enhanced user context for editorial direction)
# ---------------------------------------------------------------------------


class AudienceSpec(BaseModel):
    """Who watches this video and where."""

    platform: str = Field(
        default="",
        description="Target platform: youtube, tiktok, instagram, family_archive, personal",
    )
    viewer: str = Field(
        default="",
        description="Target viewer description: 'friends and family', 'travel enthusiasts', etc.",
    )


class NarrativeDirection(BaseModel):
    """Story structure hints for the editor."""

    story_thesis: str = Field(
        default="",
        description="One sentence: what is this video about? The editorial north star.",
    )
    story_hook: str = Field(
        default="", description="What grabs the viewer in the first 10 seconds?"
    )
    key_beats: list[str] = Field(
        default=[], description="Ordered narrative beats the editor should hit"
    )
    ending_note: str = Field(default="", description="How should the video end emotionally?")
    structure: str = Field(
        default="",
        description="Narrative structure: chronological, thematic, circular, or vignettes",
    )


class StyleDirection(BaseModel):
    """Visual and audio style preferences for the edit."""

    pacing: str = Field(
        default="",
        description="Pacing preference: slow-contemplative, balanced, punchy, or builds-to-climax",
    )
    music_mood: str = Field(
        default="",
        description="Music direction: acoustic, lo-fi, orchestral, ambient, natural-audio-only",
    )
    energy_curve: str = Field(
        default="",
        description="Energy shape: steady, low-high-low, builds, or peaks-and-valleys",
    )
    transitions: str = Field(
        default="",
        description="Transition preference: soft-dissolves, hard-cuts, or mixed",
    )
    visual_tone: str = Field(
        default="",
        description="Visual tone: warm, cool, cinematic, bright, or natural",
    )


class CreativeBrief(BaseModel):
    """Creative direction for the edit — strict superset of legacy user_context.

    All legacy fields (people, activity, tone, highlights, avoid, duration, context_qa)
    remain top-level strings for backward compatibility. Enhanced fields add structured
    editorial direction without breaking old projects.

    Loading: ``CreativeBrief(**old_user_context_dict)`` works for legacy dicts.
    """

    # --- Legacy fields (backward compatible with user_context.json) ---
    people: str = ""
    activity: str = ""
    tone: str = ""
    highlights: str = ""
    avoid: str = ""
    duration: str = ""
    context_qa: list[dict] = Field(
        default=[], description="AI-suggested Q&A pairs: [{question, answer}]"
    )

    # --- Enhanced fields ---
    intent: str = Field(
        default="",
        description="What should the viewer feel or do after watching? The creative north star.",
    )
    audience: AudienceSpec | None = None
    narrative: NarrativeDirection | None = None
    style: StyleDirection | None = None
    references: list[str] = Field(
        default=[], description="Style inspiration: creators, videos, moods"
    )
    notes: str = Field(default="", description="Free-form creative notes for the editor")
    creative_direction_text: str = Field(
        default="",
        description="Raw freeform creative direction from a file. When present, injected "
        "as-is into the CREATIVE DIRECTION prompt tier — the LLM extracts what it needs.",
    )

    # --- Metadata ---
    brief_version: int = Field(
        default=1, description="1 = legacy flat user_context, 2 = enhanced creative brief"
    )
    source: str = Field(default="tui", description="Input source: tui, file, or preset")
    preset_key: str = Field(
        default="", description="Creative preset key if brief was pre-filled from a preset"
    )

    def has_creative_direction(self) -> bool:
        """Return True if any enhanced fields are populated."""
        return bool(
            self.intent
            or self.audience
            or self.narrative
            or self.style
            or self.references
            or self.creative_direction_text
        )

    def to_legacy_dict(self) -> dict:
        """Export only legacy fields for backward-compatible serialization."""
        d: dict = {}
        for key in ("people", "activity", "tone", "highlights", "avoid", "duration"):
            val = getattr(self, key)
            if val:
                d[key] = val
        if self.context_qa:
            d["context_qa"] = self.context_qa
        return d


class CreativePreset(BaseModel):
    """User-defined reusable creative direction template.

    Captures non-project-specific style and intent fields that can be
    applied to new projects as a starting point for the creative brief.
    Stored in ``~/.vx/presets/{key}.json``.
    """

    key: str = Field(description="Unique identifier: 'my-travel-style', 'family-vlogs'")
    label: str = Field(description="Human-readable name: 'Max's Travel Style'")
    description: str = ""
    # Partial brief fields (no project-specific data like people/activity/highlights)
    intent: str = ""
    tone: str = ""
    audience: AudienceSpec | None = None
    narrative_defaults: NarrativeDirection | None = Field(
        default=None,
        description="Default narrative settings (structure, pacing — not key_beats)",
    )
    style: StyleDirection | None = None
    references: list[str] = []
    style_preset_key: str = Field(
        default="", description="Optionally also activate a code-defined StylePreset"
    )
    created_at: str = ""


# ---------------------------------------------------------------------------
# Phase 1 clip review models (Gemini response_schema)
# ---------------------------------------------------------------------------


class ReviewQuality(BaseModel):
    overall: str = Field(description="good, fair, or poor")
    stability: str = Field(description="steady, slightly_shaky, or very_shaky")
    lighting: str = Field(description="well_lit, mixed, dark, or overexposed")
    focus: str = Field(description="sharp, soft, or out_of_focus")
    composition: str = Field(description="intentional, casual, or accidental")


class ReviewPerson(BaseModel):
    label: str = Field(description="Consistent label: person_A, person_B, etc.")
    description: str = Field(
        description="Detailed appearance: clothing, hair, build, "
        "distinguishing features for cross-clip matching"
    )
    role: str = Field(description="main_subject, companion, bystander, or crowd")
    screen_time_pct: float = Field(
        description="Approximate fraction of clip where this person is visible (0.0-1.0)"
    )
    speaking: bool = Field(description="Whether this person speaks in the clip")
    timestamps: list[str] = Field(
        default=[], description="Time ranges when visible, e.g. ['0:05-0:30', '1:10-1:25']"
    )


class ReviewKeyMoment(BaseModel):
    timestamp: str = Field(description="Human-readable timestamp M:SS")
    timestamp_sec: float = Field(description="Timestamp in seconds from clip start")
    description: str = Field(description="What happens at this moment")
    editorial_value: str = Field(description="high, medium, or low")
    suggested_use: str = Field(
        description="opening_hook, establishing, context, action, reaction, "
        "b_roll, cutaway, climax, or outro"
    )


class ReviewUsableSegment(BaseModel):
    in_point: str = Field(description="Human-readable start time M:SS")
    in_sec: float = Field(description="Start time in seconds from clip start")
    out_point: str = Field(description="Human-readable end time M:SS")
    out_sec: float = Field(description="End time in seconds from clip start")
    duration_sec: float = Field(description="Segment duration in seconds")
    description: str = Field(description="What this segment contains")
    quality: str = Field(description="good or fair")


class ReviewDiscardSegment(BaseModel):
    in_point: str = Field(description="Human-readable start time M:SS")
    out_point: str = Field(description="Human-readable end time M:SS")
    reason: str = Field(
        description="blurry, shaky, accidental, redundant, boring, lens_cap, or out_of_focus"
    )


class ReviewAudio(BaseModel):
    has_speech: bool = Field(description="Whether speech is present")
    speech_language: str | None = Field(default=None, description="Detected language or null")
    speech_summary: str | None = Field(
        default=None, description="Key things said, or null if no speech"
    )
    ambient_description: str = Field(
        description="Ambient sounds: wind, crowd, traffic, silence, etc."
    )
    music_potential: str = Field(
        description="good_for_music_bed, needs_music_overlay, or has_natural_soundtrack"
    )


class ClipReview(BaseModel):
    clip_id: str = Field(description="Exact clip identifier — must match the clip_id provided")
    summary: str = Field(description="2-3 sentence visual summary of the clip")
    quality: ReviewQuality
    content_type: list[str] = Field(
        description="Types: talking_head, b_roll, action, landscape, "
        "transition, establishing, accidental"
    )
    people: list[ReviewPerson] = Field(default=[], description="All people observed in this clip")
    key_moments: list[ReviewKeyMoment] = Field(
        default=[], description="Notable moments with editorial value"
    )
    usable_segments: list[ReviewUsableSegment] = Field(
        description="Every usable segment — be thorough"
    )
    discard_segments: list[ReviewDiscardSegment] = Field(
        default=[], description="Segments to exclude and why"
    )
    audio: ReviewAudio
    editorial_notes: str = Field(description="Free-form: how this clip might fit into a final edit")


# ---------------------------------------------------------------------------
# Editorial storyboard models
# ---------------------------------------------------------------------------


class CastMember(BaseModel):
    name: str = Field(description="Person's name or label (e.g. 'person_A', 'Max')")
    description: str = Field(
        description="Visual appearance and distinguishing features for cross-clip matching"
    )
    role: str = Field(description="main_subject, companion, or bystander")
    appears_in: list[str] = Field(description="List of clip_ids where this person appears")


class Segment(BaseModel):
    index: int = Field(description="Sequential position in the final edit (0-based)")
    clip_id: str = Field(
        description="Exact clip_id from the clip reviews — must not be abbreviated or shortened"
    )
    in_sec: float = Field(
        description="Start time in seconds from clip start. "
        "Must fall within a usable_segment from the clip review."
    )
    out_sec: float = Field(
        description="End time in seconds from clip start. Must not exceed the clip's duration."
    )
    purpose: str = Field(
        description="Editorial role of this segment: "
        "hook (attention-grabbing opener), establish (set location/mood), "
        "context (provide background), action (key activity), "
        "reaction (emotional response), b_roll (visual variety), "
        "cutaway (brief insert), climax (peak moment), "
        "payoff (resolution), reflection (quiet beat), outro (closing)"
    )
    description: str = Field(
        description="Why this segment is included — what it contributes to the narrative arc, "
        "not just what is visually happening"
    )
    transition: str = Field(
        description="Transition into this segment: "
        "cut (hard cut), dissolve (cross-dissolve), "
        "fade_in, fade_out, j_cut (audio leads), l_cut (audio trails)"
    )
    audio_note: str = Field(
        default="",
        description="Audio strategy: preserve_dialogue, music_bed, ambient, voice_over, mute",
    )
    text_overlay: str = Field(
        default="", description="On-screen text if needed, empty string if none"
    )

    @property
    def duration_sec(self) -> float:
        return self.out_sec - self.in_sec


class DiscardedClip(BaseModel):
    clip_id: str = Field(description="Exact clip_id being discarded")
    reason: str = Field(description="Why this clip is not used in the final edit")


class MusicCue(BaseModel):
    section: str = Field(description="Which story arc section this music covers")
    strategy: str = Field(
        description="Music approach: upbeat_background, emotional_underscore, "
        "ambient_texture, silence, or natural_audio_only"
    )
    notes: str = Field(default="", description="Tempo, mood, or genre suggestions")


class StoryArcSection(BaseModel):
    title: str = Field(description="Section name: Opening Hook, Rising Action, Climax, Outro, etc.")
    description: str = Field(description="What this section accomplishes narratively")
    segment_indices: list[int] = Field(
        default=[], description="Indices into the segments list that belong to this section"
    )


class EditorialStoryboard(BaseModel):
    editorial_reasoning: str = Field(
        default="",
        description="Your editorial thinking process. Address these in order: "
        "1) CONSTRAINT CHECK — for each filmmaker MUST-INCLUDE/MUST-EXCLUDE, state which "
        "clip and usable segment satisfies it. If a constraint cannot be satisfied, explain why. "
        "2) Story concept — what story does this footage tell? "
        "3) Opening hook — what is the strongest first 10 seconds? "
        "4) Arc structure — beginning/middle/end with clip assignments. "
        "5) Pacing plan — where is the edit fast vs slow, energetic vs contemplative?",
    )
    title: str = Field(description="Creative title for the final video")
    estimated_duration_sec: float = Field(
        description="Target total duration of the final edit in seconds"
    )
    style: str = Field(description="Video style matching the request (e.g. vlog, recap, highlight)")
    story_concept: str = Field(
        description="The narrative thesis — what makes this edit compelling, "
        "not a plot summary but the editorial angle"
    )
    cast: list[CastMember] = Field(
        default=[], description="People identified across clips with consistent identities"
    )
    story_arc: list[StoryArcSection] = Field(
        default=[], description="Narrative structure dividing the edit into dramatic sections"
    )
    segments: list[Segment] = Field(
        description="Ordered list of every segment in the final edit, from first to last"
    )
    discarded: list[DiscardedClip] = Field(
        default=[], description="Clips intentionally excluded and why"
    )
    music_plan: list[MusicCue] = Field(
        default=[], description="Music strategy for each section of the edit"
    )
    technical_notes: list[str] = Field(
        default=[], description="Notes on color grading, aspect ratio, or format considerations"
    )
    pacing_notes: list[str] = Field(
        default=[], description="Notes on rhythm, energy shifts, and timing"
    )

    @property
    def total_segments_duration(self) -> float:
        return sum(s.duration_sec for s in self.segments)


# ---------------------------------------------------------------------------
# Story Plan models (intermediate output for multi-call Phase 2 pipeline)
# ---------------------------------------------------------------------------


class PlannedSegment(BaseModel):
    """A segment in the editorial plan — references clips by usable_segment index, no timestamps."""

    clip_id: str = Field(
        description="Full clip ID from the available clips list — never abbreviated"
    )
    usable_segment_index: int = Field(
        description="Index into the clip's usable_segments array from the Phase 1 review"
    )
    purpose: str = Field(
        description="opening_hook, establishing, context, action, reaction, "
        "b_roll, cutaway, climax, payoff, reflection, or outro"
    )
    arc_phase: str = Field(description="opening_context, experience, or closing_reflection")
    narrative_role: str = Field(
        description="What this segment contributes to the story — 1 sentence"
    )
    audio_strategy: str = Field(description="preserve_dialogue, music_bed, or ambient_only")
    is_speech_segment: bool = Field(description="True if primary content is dialogue")


class StoryPlan(BaseModel):
    """Structured editorial plan produced by Call 2A.5 — no timestamps, only segment references."""

    title: str = Field(description="Creative title for the final video")
    style: str = Field(description="Video style: vlog, recap, highlight, etc.")
    story_concept: str = Field(description="2-3 sentence narrative summary")
    cast: list[CastMember] = Field(
        default=[], description="People identified across clips with consistent identities"
    )
    story_arc: list[StoryArcSection] = Field(
        default=[], description="Narrative structure dividing the edit into dramatic sections"
    )
    planned_segments: list[PlannedSegment] = Field(
        description="Ordered list of segments in the planned edit"
    )
    discarded: list[DiscardedClip] = Field(
        default=[], description="Clips intentionally excluded and why"
    )
    pacing_notes: str = Field(default="", description="Rhythm, energy shifts, and timing strategy")
    music_direction: str = Field(default="", description="Overall music and audio strategy")
    constraint_satisfaction: str = Field(
        default="",
        description="For each filmmaker constraint, how it was satisfied or why it could not be",
    )


# ---------------------------------------------------------------------------
# Section-based pipeline models (Divide & Conquer Phase 2)
# ---------------------------------------------------------------------------


class Section(BaseModel):
    """A group of clips within a single activity/scene."""

    section_id: str = Field(description="Unique identifier, e.g. 'day1_scene2'")
    label: str = Field(description="Human-readable label, e.g. 'Morning temple visit'")
    clip_ids: list[str] = Field(
        description="Clip IDs in this section (aesthetic order, not necessarily chronological)"
    )
    time_range: str = Field(default="", description="Approximate time range, e.g. '09:30-11:15'")
    activity: str = Field(default="", description="Detected or user-assigned activity/scene type")


class SectionGroup(BaseModel):
    """A day containing multiple sections, in chronological order."""

    group_id: str = Field(description="Unique identifier, e.g. 'day1'")
    date: str = Field(description="ISO date string, e.g. '2026-04-05'")
    label: str = Field(description="Human-readable label, e.g. 'Day 1 — Apr 5'")
    sections: list[Section] = Field(description="Sections within this day, in chronological order")


class SectionNarrative(BaseModel):
    """Narrative assignment for one section within the storyline."""

    section_id: str = Field(description="References Section.section_id")
    narrative_role: str = Field(
        description="What this section contributes to the overall story arc"
    )
    arc_phase: str = Field(
        description="opening_context, rising_action, experience, climax, or closing_reflection"
    )
    energy: str = Field(description="high, medium, or low")
    target_duration_sec: float = Field(default=0, description="Suggested duration for this section")


class SectionPlan(BaseModel):
    """LLM output: narrative storyline across all sections."""

    title: str = Field(description="Creative title for the final video")
    style: str = Field(description="Video style: vlog, recap, etc.")
    story_concept: str = Field(description="2-3 sentence narrative thesis")
    section_narratives: list[SectionNarrative] = Field(
        description="Per-section narrative role in the overall story"
    )
    hook_section_id: str = Field(description="Section ID that provides the opening hook material")
    hook_description: str = Field(description="What the hook should show and why")
    pacing_notes: str = Field(default="")
    music_direction: str = Field(default="")
    constraint_satisfaction: str = Field(default="")


class SectionStoryboard(BaseModel):
    """Per-section LLM output: segments + narrative summary for context passing."""

    section_id: str = Field(description="Which section this covers")
    segments: list[Segment] = Field(description="Ordered segments for this section")
    discarded: list[DiscardedClip] = Field(
        default=[], description="Clips from this section not used"
    )
    cast: list[CastMember] = Field(default=[])
    narrative_summary: str = Field(
        description="2-3 sentence summary of what this section covers, "
        "passed as context to subsequent sections"
    )
    music_cue: MusicCue | None = Field(default=None)
    editorial_reasoning: str = Field(default="")


class HookStoryboard(BaseModel):
    """Opening hook LLM output."""

    segments: list[Segment] = Field(
        description="Hook segments (typically 2-5, ~10-15 seconds total)"
    )
    editorial_reasoning: str = Field(default="")
    hook_concept: str = Field(description="What makes this hook compelling")


# ---------------------------------------------------------------------------
# Visual Monologue models (text-driven narrative overlays for silent vlog style)
# ---------------------------------------------------------------------------


class EligibleSegment(BaseModel):
    """A segment eligible for text overlays — output of monologue Call 1."""

    segment_index: int = Field(description="Index into the storyboard segments list")
    segment_duration_sec: float = Field(description="Duration of this segment in seconds")
    arc_phase: str = Field(description="grounding_hook, wandering_middle, or resolution")
    intent: str = Field(description="1 sentence: what the overlay should accomplish narratively")
    preceding_context: str | None = Field(
        default=None, description="1-line summary of the speech in the preceding segment"
    )
    following_context: str | None = Field(
        default=None, description="1-line summary of the speech in the following segment"
    )
    max_overlay_count: int = Field(
        default=2, description="Maximum number of overlays for this segment (1-3)"
    )
    notes: str = Field(default="", description="Additional placement or timing notes")


class OverlayPlan(BaseModel):
    """Segment analysis and arc planning — output of monologue Call 1."""

    persona_recommendation: str = Field(
        description="Recommended persona: conversational_confidant, detached_observer, "
        "or stream_of_consciousness"
    )
    persona_rationale: str = Field(
        description="1-2 sentences explaining why this persona fits the footage"
    )
    eligible_segments: list[EligibleSegment] = Field(
        description="Segments eligible for text overlays (no speech, scenery/b-roll only)"
    )


class OverlayDraft(BaseModel):
    """A single overlay draft — output of monologue Call 2."""

    segment_index: int = Field(description="Which storyboard segment this overlay belongs to")
    text: str = Field(description="The overlay text — lowercase, 5-8 words")
    appear_at: float = Field(description="Seconds from segment start to show the overlay")
    duration_sec: float = Field(description="How long the overlay stays on screen")
    word_count: int = Field(description="Number of words in the text")
    arc_phase: str = Field(
        default="", description="grounding_hook, wandering_middle, or resolution"
    )


class OverlayDrafts(BaseModel):
    """Collection of overlay drafts — output of monologue Call 2."""

    overlays: list[OverlayDraft] = Field(description="All overlay drafts in chronological order")


class TextOverlayStyle(BaseModel):
    font: str = "sans-serif"  # "sans-serif" | "handwritten"
    case: str = "lowercase"  # "lowercase" | "sentence"
    size: str = "medium"  # "small" | "medium" | "large"
    position: str = "lower_third"  # "lower_third" | "center" | "upper_third"
    alignment: str = "center"  # "left" | "center" | "right"


class MonologueOverlay(BaseModel):
    index: int
    segment_index: int  # references Segment.index in storyboard
    text: str  # the overlay text (e.g. "the city decided to wash itself clean...")
    appear_at: float  # seconds from segment start
    duration_sec: float  # how long text stays on screen
    note: str = ""  # editorial note about why this text here


class MonologuePlan(BaseModel):
    persona: str  # "conversational_confidant" | "detached_observer" | "stream_of_consciousness"
    persona_description: str  # LLM's characterization of the voice
    tone_mechanics: list[str] = []  # "lowercase_whisper", "ellipses", "micro_pacing"
    arc_structure: list[str] = []  # ["grounding_hook", "wandering_middle", "resolution"]
    overlays: list[MonologueOverlay]
    total_text_time_sec: float  # sum of overlay durations
    pacing_notes: list[str] = []
    music_sync_notes: list[str] = []


# ---------------------------------------------------------------------------
# Editorial Director: review loop models
# ---------------------------------------------------------------------------


class SegmentIssue(BaseModel):
    """A specific problem found during editorial review."""

    segment_index: int = Field(description="Which segment has the issue (-1 for global)")
    dimension: str = Field(
        description="Which rubric dimension flagged this: constraint_satisfaction, "
        "timestamp_precision, structural_completeness, speech_cut_safety, "
        "narrative_flow, segment_coherence, transcription_coherence"
    )
    severity: str = Field(description="critical | warning | suggestion")
    description: str = Field(description="Human-readable description of the problem")
    suggested_fix: str = Field(default="", description="What the fix should look like")


class ReviewVerdict(BaseModel):
    """Result of a single review assessment."""

    passed: bool = Field(description="Whether the storyboard meets the quality bar")
    scores: dict[str, float] = Field(
        default={}, description="Dimension name → score (0.0-1.0 for computable, 0-2 for LLM)"
    )
    issues: list[SegmentIssue] = Field(default=[], description="All issues found")
    summary: str = Field(default="", description="Agent's editorial assessment")


class ReviewIteration(BaseModel):
    """Record of a single iteration in the review loop."""

    turn: int
    tool_name: str = ""
    tool_args: dict = {}
    result_summary: str = ""
    cost_usd: float = 0.0
    duration_sec: float = 0.0


class SegmentChange(BaseModel):
    """Record of a single segment modification during review."""

    change_type: str  # "fix", "delete", "reorder"
    segment_index: int = -1
    fields_changed: list[str] = []
    before: dict = {}
    after: dict = {}


class ReviewLog(BaseModel):
    """Full audit trail of an editorial review session."""

    iterations: list[ReviewIteration] = Field(default=[])
    final_verdict: ReviewVerdict | None = None
    total_turns: int = 0
    total_fixes: int = 0
    total_cost_usd: float = 0.0
    total_duration_sec: float = 0.0
    convergence_reason: str = ""  # "finalized" | "budget" | "timeout" | "no_tool_calls"
    changes: list[SegmentChange] = Field(default=[])
    eval_before: str = ""
    eval_after: str = ""


# ---------------------------------------------------------------------------
# Chat session persistence
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """Serialized representation of one message in a director chat session."""

    role: str  # "user" | "model"
    text: str = ""  # concatenated text parts
    tool_calls: list[dict] = Field(default=[])  # [{name, args}]
    tool_responses: list[dict] = Field(default=[])  # [{name, result_summary}]
    timestamp: str = ""


class ChatSession(BaseModel):
    """Persistent state for a director chat session."""

    session_id: str  # "session_001"
    created_at: str
    updated_at: str
    status: str = "active"  # "active" | "completed"
    storyboard_version: int  # current storyboard version being edited
    starting_version: int  # version when session started
    provider: str = "gemini"
    messages: list[ChatMessage] = Field(default=[])
    budget_state: dict = Field(default={})  # {turns_used, fixes_used, cost_used_usd}
    total_edits: int = 0
    style_preset: str = ""


# ---------------------------------------------------------------------------
# Versioning: artifact metadata and composition
# ---------------------------------------------------------------------------


class ArtifactMeta(BaseModel):
    """Sidecar metadata for every versioned output.

    Lives next to the versioned file: editorial_gemini_v4.json → editorial_gemini_v4.meta.json
    Tracks lineage (which inputs produced this output), status, and config snapshot.
    """

    artifact_id: str  # "sb:rv1.3" or "mn:sb3.1" (lineage-prefixed)
    phase: str  # "review", "storyboard", "monologue", "cut", "preview", "quick_scan", "user_context", "transcript"
    provider: str  # "gemini", "claude", "mlx", "user", "ffmpeg"
    version: int  # iteration number within parent scope
    status: str = "pending"  # "pending" | "complete" | "failed"
    created_at: str  # ISO timestamp
    completed_at: str | None = None
    parent_id: str | None = None  # direct parent artifact ID for lineage prefix
    inputs: dict[str, str] = {}  # role → artifact_id (full lineage tracking)
    clip_id: str | None = None  # set for per-clip artifacts (reviews, transcripts)
    track: str = "main"  # experiment track namespace
    config_snapshot: dict = {}  # model, temperature, style used for this run
    output_files: list[str] = []  # relative paths of outputs produced
    error: str | None = None  # failure reason if status="failed"


class Composition(BaseModel):
    """A named combination of artifact versions for rendering a rough cut.

    Allows mixing storyboard v2 + monologue v1 instead of always using _latest.
    """

    name: str  # "default", "narrative-first-take1"
    storyboard: str  # artifact_id, e.g. "storyboard:gemini:v3"
    monologue: str | None = None  # artifact_id, optional
    created_at: str  # ISO timestamp
    notes: str = ""


class CutComposition(BaseModel):
    """Full provenance manifest written to each cut directory as composition.json.

    Records every upstream artifact that went into producing the video,
    enabling full DAG lineage tracing from any rough cut back to its inputs.
    """

    cut_id: str  # "cut_001"
    created_at: str
    storyboard: dict  # {artifact_id, file, segments, duration_sec}
    monologue: dict | None = None  # {artifact_id, file, overlays}
    transcription_provider: str = ""
    briefing: str = ""  # user_context artifact_id or filename
    style_preset: str = ""
    output_format: dict = {}  # {width, height, fps, codec, label}
