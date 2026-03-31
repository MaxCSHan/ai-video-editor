"""Pydantic models for the editorial storyboard — the single source of truth.

These models are used for:
1. Gemini structured output (response_schema)
2. Claude JSON parsing (model_validate_json)
3. Rendering to markdown and HTML
4. ffmpeg rough cut assembly
"""

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Transcript models (from mlx-whisper speech-to-text)
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


class Transcript(BaseModel):
    source_audio: str
    model: str
    language: str
    text: str
    segments: list[TranscriptSegment]
    duration_sec: float
    has_speech: bool


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
