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
        description="Your editorial thinking process. Address these in order: "
        "1) CONSTRAINT CHECK — for each filmmaker MUST-INCLUDE/MUST-EXCLUDE, state which "
        "clip and usable segment satisfies it. If a constraint cannot be satisfied, explain why. "
        "2) Story concept — what story does this footage tell? "
        "3) Opening hook — what is the strongest first 10 seconds? "
        "4) Arc structure — beginning/middle/end with clip assignments. "
        "5) Pacing plan — where is the edit fast vs slow, energetic vs contemplative?"
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
# Visual Monologue models (text-driven narrative overlays for silent vlog style)
# ---------------------------------------------------------------------------


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
    style: TextOverlayStyle = TextOverlayStyle()
    synergy: str = "harmony"  # "harmony" | "dissonance"
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
