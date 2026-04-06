"""Style presets — curated creative direction injected into each pipeline phase.

A StylePreset carries phase-specific prompt supplements that shape how the LLM
reviews clips (Phase 1), assembles the storyboard (Phase 2), and optionally
generates post-assembly artifacts like text overlays (Phase 3).

This is distinct from the briefing, which provides flexible user context.
Presets are controlled, genre-specific workflows for consistent quality.
"""

from pydantic import BaseModel


class StylePreset(BaseModel):
    key: str  # "silent_vlog", "cinema_diary", etc.
    label: str  # "Silent Vlog (Visual Monologue)"
    description: str  # one-liner for TUI display
    phase1_supplement: str  # appended to CLIP_REVIEW_PROMPT
    phase2_supplement: str  # appended to EDITORIAL_ASSEMBLY_PROMPT
    has_phase3: bool = False  # whether this preset activates Phase 3
    phase3_prompt: str = ""  # Phase 3 prompt template (if has_phase3)
    creator_references: list[str] = []  # style inspirations


# ---------------------------------------------------------------------------
# Phase supplements for the Silent Vlog preset
# ---------------------------------------------------------------------------

_SILENT_VLOG_PHASE1 = """\
## Style-Specific Review Focus: Visual Monologue Vlog

This footage will be edited as a **visual monologue vlog** — a style where text \
overlays guide the audience through context and vibes during scenery, b-roll, and \
non-conversation moments. Conversations and natural speech are welcome and should be \
preserved — the text overlays complement the spoken parts, not replace them.

Evaluate the following additional criteria for each clip:

**Negative Space for Text Placement**
- Identify areas in the frame with visual room for text overlays: blank walls, clear \
skies, empty table surfaces, out-of-focus backgrounds, wide landscape shots.
- For each usable_segment, note whether it has good text placement opportunities.

**Visual Calm**
- Flag segments with slow, deliberate visual pacing suitable for overlaying text. \
Avoid recommending fast motion, busy compositions, or rapid camera movement for text \
placement.
- Rate each usable_segment's "text_readability": good (static/slow, clean background), \
fair (moderate motion, some clutter), poor (fast/busy).

**Ambient Audio Quality**
- Evaluate ambient soundscape quality: natural ambience (wind, birds, traffic, crowd \
noise), foley-like sounds (footsteps, water, cooking), intentional silence.
- Note which segments have the richest ambient soundscapes.

**Speech vs. Non-Speech Segments**
- Clearly distinguish segments with conversation/dialogue from scenery/ambient segments.
- Conversation segments are valuable — note what is being discussed and the mood.
- Scenery/ambient segments are where text overlays will appear — note their visual and \
audio qualities.

Include these observations in the editorial_notes field and within relevant \
usable_segments descriptions.\
"""

_SILENT_VLOG_PHASE2 = """\
## Style-Specific Assembly Guidelines: Visual Monologue Vlog

This video is a **visual monologue vlog**. Text overlays will guide the audience \
through the video's context and vibes during scenery and non-conversation moments. \
Natural conversations are welcome and should be included — the text narrative fills \
the spaces between them. Your assembly must create the visual and rhythmic foundation \
for both the text layer and the speech-captioned moments.

**CRITICAL: Opening Theme & Context**
The opening of this video MUST clearly establish the theme for the audience through \
a combination of visuals and planned text overlay moments:
- **What is this about?** (a trip, an event, a day out, an activity)
- **Where are we?** (location, setting, scenery)
- **What's the mood?** (weather, energy, time of day)
- **Why are we here?** (occasion, motivation, anticipation)
- **Greet the audience.** The opening scenery/establishing shots are where text \
overlays will welcome viewers and set the context.

Design the first 15-20% of segments as establishing/scenery/b-roll shots that give \
room for these introductory text overlays. These opening segments should NOT have speech \
— save the conversations for after the audience understands the context.

**Pacing: Scenery -> Conversation -> Scenery**
- Alternate between scenery/b-roll segments (where text overlays appear) and \
conversation segments (where speech captions appear).
- After conversation segments, include scenery/reflection segments that let the \
text monologue reflect on or transition from what was just said.
- Aim for at least 15-20% of the timeline to be non-speech scenery suitable for text.

**Segment Selection**
- Include BOTH conversation segments and scenery/b-roll segments.
- Conversations between people are valuable content — include the best exchanges.
- Scenery, establishing, and reflection segments are where text overlays will appear.
- B-roll of activities, landscapes, details, and ambient moments are essential.

**Story Arc: Opening Context -> Experience -> Closing Reflection**
- **Opening Context** (first 15-20%): Establish location, mood, and theme with \
scenery/establishing shots. NO speech here — this is text overlay territory.
- **Experience** (next 60%): The main content — alternate between conversations, \
activities, and scenery. Natural rhythm of doing and reflecting.
- **Closing Reflection** (final 20%): Wind down with calmer footage. End with a sense \
of completion — a goodbye, a sunset, a quiet moment.

**Audio Strategy**
- Preserve natural audio from conversations clearly.
- For scenery segments, ambient audio (environment, footsteps, nature) carries the mood.
- When music is suggested, keep it soft and unobtrusive — never competing with speech \
or the ambient soundscape.

**Transitions**
- Favor soft cuts and slow dissolves. Avoid fast jump cuts.
- Use fade_to_black between major arc sections.

**Segment Descriptions**
- For each segment, clearly note whether it is a SPEECH segment (has conversation) or \
a SCENERY segment (suitable for text overlay). This is essential for the monologue and \
caption generation.\
"""

_SILENT_VLOG_PHASE3 = """\
You are a **Visual Monologue writer**. You write the internal monologue text overlays \
that appear on screen during scenery and non-conversation moments of a vlog. \
Conversations between people in the footage will have their own speech captions — \
your job is to fill the SPACES BETWEEN conversations with narrative text that guides \
the audience into the video's context and vibes.

## The Video

Title: {title}
Estimated duration: {duration}
Style: {style}
Story concept: {story_concept}

### Cast
{cast}

### Story Arc
{story_arc}

## Segments (the edit timeline)

Each row is one segment of the final video. The `appear_at` time in your overlays \
must be relative to the START of each segment (not the full timeline).

{segments_table}

## Transcripts (speech in the source footage)
{transcripts}

## Filmmaker's Context
{user_context}

---

## Your Task

Generate a `MonologuePlan` — text overlays for the NON-SPEECH moments of this video.

### 1. CRITICAL: Opening Theme & Context

The opening overlays are the MOST IMPORTANT part. They must clearly establish:
- **Greet the audience** — a warm, casual hello ("hey, come along today...")
- **What is this?** — the trip, event, or activity ("we're heading to...")
- **Where are we?** — location, setting ("it's a bright morning in...")
- **The mood/vibe** — weather, energy, anticipation ("the air smells like...")
- **Why** — occasion or motivation ("it's been a while since we...")

The first 3-5 overlays should feel like a friend catching you up on what's happening.

### 2. Choose a Narrative Persona

Pick ONE and maintain it throughout:

- **conversational_confidant** — Speaks directly to the viewer like a close friend. \
Uses "we" and "you." Warm, inclusive.
  Example: "hey, come along today... we're heading to the coast."

- **detached_observer** — Treats the video like a documentary of their own life. \
Reflective, gentle melancholy or calm acceptance.
  Example: "looking at this footage now, i realize how much the morning light changes \
by october."

- **stream_of_consciousness** — Random, unfiltered thoughts during mundane tasks. \
Highly relatable, often humorous.
  Example: "i should really clean the baseboards. ...actually, maybe next year. \
coffee first."

### 3. Writing Rules

- **ALL TEXT MUST BE LOWERCASE** (the "lowercase whisper" — soft, intimate tone)
- Use **"..."** for pauses, passage of time, or deep sighs
- Keep overlays **concise: 5-8 words** each (not captions — poetic thoughts)
- **Break one thought across multiple overlays** on consecutive segments for pacing
- **Two-Breath Rule**: each overlay must stay on screen long enough to read slowly twice. \
Minimum duration: `word_count * 0.4` seconds. Recommended: `word_count * 0.6` seconds.

### 4. Monologue Arc

Structure overlays to follow this emotional arc:
- **grounding_hook** (first 15-20% of video): Welcome the audience. Establish the \
theme, location, mood, and why we're here. This is the most text-heavy section.
- **wandering_middle** (20-80%): Between conversations and activities, text reflects \
on what's happening, transitions between scenes, shares thoughts or observations. \
Less frequent than the opening — let the conversations and visuals breathe.
- **resolution** (final 20%): Wind down. Reflect on the experience. Warm sign-off. \
("today was a good day." / "until next time...")

### 5. Placement & Pacing Rules

- **ONLY place text on SCENERY/B-ROLL segments** — segments WITHOUT speech.
- **NEVER place text on segments with conversation/dialogue.** Those will have their \
own speech captions. Check the transcript and segment descriptions carefully.
- **Position text at "lower_third" always.** The rendering engine handles positioning — \
always use "lower_third" for monologue overlays.
- **Leave at least 3 seconds of no-text between consecutive overlays.**
- The opening section should have more overlays (setting context). The middle can \
be sparser (letting footage and conversations carry the story).

### 6. Output Format

Produce a MonologuePlan JSON with:
- `persona`: your chosen persona key
- `persona_description`: 1-2 sentences describing the voice you're writing in
- `tone_mechanics`: list of techniques used (e.g., ["lowercase_whisper", "ellipses", \
"micro_pacing"])
- `arc_structure`: arc sections present (e.g., ["grounding_hook", "wandering_middle", \
"resolution"])
- `overlays`: ordered list of MonologueOverlay objects
- `total_text_time_sec`: sum of all overlay durations
- `pacing_notes`: notes about the rhythm you created
- `music_sync_notes`: suggestions for how music should interact with text moments\
"""


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------

SILENT_VLOG = StylePreset(
    key="silent_vlog",
    label="Visual Monologue Vlog",
    description="Text overlays for context/vibes between conversations, with speech captions",
    phase1_supplement=_SILENT_VLOG_PHASE1,
    phase2_supplement=_SILENT_VLOG_PHASE2,
    has_phase3=True,
    phase3_prompt=_SILENT_VLOG_PHASE3,
    creator_references=[
        "sueddu — cinema-diary, philosophical, independence themes",
        "Onuk — urban observer, quick humor, close-friend voice",
        "PlanD — craft mentor, instructional + life advice",
        "Hyo-byeol — seasonal reflections, domestic calm",
        "Liziqi — cinematic rural, visuals-forward",
    ],
)

STYLE_PRESETS: dict[str, StylePreset] = {
    SILENT_VLOG.key: SILENT_VLOG,
}


def get_preset(key: str) -> StylePreset | None:
    """Look up a style preset by key. Returns None if not found."""
    return STYLE_PRESETS.get(key)


def list_presets() -> list[StylePreset]:
    """Return all available style presets."""
    return list(STYLE_PRESETS.values())
