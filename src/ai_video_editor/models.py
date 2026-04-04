"""Pydantic models for the editorial storyboard — the single source of truth.

These models are used for:
1. Gemini structured output (response_schema)
2. Claude JSON parsing (model_validate_json)
3. Rendering to markdown and HTML
4. ffmpeg rough cut assembly
"""

from pydantic import BaseModel


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
    start: float  # seconds from clip start
    end: float
    text: str
    speaker: str | None = None  # "Person_A", specific name, or None for non-speech
    type: str = "speech"  # speech | music | sound_effect | silence


class GeminiTranscript(BaseModel):
    language: str
    segments: list[GeminiTranscriptSegment]
    speakers: list[str] = []  # unique speaker labels
    has_speech: bool


# ---------------------------------------------------------------------------
# Quick scan model (for smart briefing — one LLM call across all clips)
# ---------------------------------------------------------------------------


class PersonSighting(BaseModel):
    description: str  # "man in green PUMA shirt with race bib #30860"
    estimated_appearances: int  # how many clips they appear in
    role_guess: str  # "main subject", "companion", "bystander", "camera person"


class ClipSummary(BaseModel):
    clip_id: str
    summary: str  # one-line description
    energy: str  # "high", "medium", "low"


class QuickScanResult(BaseModel):
    overall_summary: str  # 2-3 sentences about the footage as a whole
    people: list[PersonSighting]
    activities: list[str]  # observed activities/locations
    mood: str  # overall mood/energy description
    suggested_questions: list[str]  # targeted questions to ask the filmmaker
    clip_summaries: list[ClipSummary] = []


# ---------------------------------------------------------------------------
# Editorial storyboard models
# ---------------------------------------------------------------------------


class CastMember(BaseModel):
    name: str
    description: str
    role: str  # main_subject, companion, bystander
    appears_in: list[str]  # clip_ids


class Segment(BaseModel):
    index: int
    clip_id: str
    in_sec: float  # seconds from clip start
    out_sec: float  # seconds from clip start
    purpose: str  # hook, establish, context, action, reaction, b_roll, cutaway, climax, payoff, reflection, outro, stakes, build_up, tension
    description: str
    transition: str  # cut, dissolve, fade_in, fade_out, fade_to_black, j_cut, l_cut, wipe
    audio_note: str = ""
    text_overlay: str = ""

    @property
    def duration_sec(self) -> float:
        return self.out_sec - self.in_sec


class DiscardedClip(BaseModel):
    clip_id: str
    reason: str


class MusicCue(BaseModel):
    section: str
    strategy: str
    notes: str = ""


class StoryArcSection(BaseModel):
    title: str  # Opening Hook, Introduction, Body, Climax, Outro, etc.
    description: str
    segment_indices: list[int] = []  # indices into segments list


class EditorialStoryboard(BaseModel):
    title: str
    estimated_duration_sec: float
    style: str
    story_concept: str
    cast: list[CastMember] = []
    story_arc: list[StoryArcSection] = []
    segments: list[Segment]
    discarded: list[DiscardedClip] = []
    music_plan: list[MusicCue] = []
    technical_notes: list[str] = []
    pacing_notes: list[str] = []

    @property
    def total_segments_duration(self) -> float:
        return sum(s.duration_sec for s in self.segments)


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
