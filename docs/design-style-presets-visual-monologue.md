# Design: Style Presets & Visual Monologue System

## Problem

VX currently treats "style" as a bare label (`"vlog"`, `"cinematic"`, etc.) with no creative depth — the same string is inserted into Phase 2's prompt and that's it. This means all LLM output follows the same generic editing heuristics regardless of the intended video aesthetic.

For styles with strong creative conventions — like the "Silent Vlog" (text-driven narrative vlogging) — we need style-specific creative direction that permeates the *entire* pipeline, from how clips are reviewed to how the final video is assembled and narrated.

## How This Differs from Briefing

| | **Briefing** | **Style Preset** |
|---|---|---|
| **Purpose** | User-specific context (who, what, when) | Genre-specific creative direction (how) |
| **Flexibility** | Maximum — free-form user input | Controlled — curated workflow for consistent quality |
| **Scope** | Phase 2 only (injected as context) | All phases (shapes what LLM looks for and how it creates) |
| **Persistence** | Per-project `user_context.json` | Per-project in `project.json` + global preset registry |
| **Examples** | "The woman in blue is my sister Amy" | "Favor long b-roll, evaluate negative space for text, build Action→Reflection rhythm" |

Briefing and presets are complementary: the preset controls the *technique*, the briefing provides the *context*. A silent vlog about a family trip needs both: the preset ensures the right pacing, text style, and ambient focus; the briefing tells the LLM who the people are.

---

## Architecture

### Two-Layer Design

```
┌──────────────────────────────────────────────────────┐
│                   Style Preset                       │
│  (creative direction for each pipeline phase)        │
│                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐ │
│  │  Phase 1     │  │  Phase 2    │  │  Phase 3     │ │
│  │  Supplement  │  │  Supplement │  │  (optional)  │ │
│  │             │  │             │  │  Monologue   │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬───────┘ │
└─────────┼────────────────┼─────────────────┼─────────┘
          │                │                 │
          ▼                ▼                 ▼
   ┌─────────────┐  ┌─────────────┐  ┌──────────────┐
   │  Phase 1    │  │  Phase 2    │  │  Phase 3     │
   │  Per-Clip   │  │  Editorial  │  │  Visual      │
   │  Review     │  │  Assembly   │  │  Monologue   │
   │             │  │             │  │  Generation  │
   │  (existing) │  │  (existing) │  │  (NEW)       │
   └─────────────┘  └─────────────┘  └──────────────┘
```

Each Style Preset is a `StylePreset` object containing:
1. **Phase 1 supplement** — additional review criteria appended to the clip review prompt
2. **Phase 2 supplement** — additional assembly guidelines appended to the editorial prompt
3. **Phase 3 definition** (optional) — a complete prompt template for post-storyboard generation (e.g., text overlays)

Not all presets need Phase 3. A "cinematic" preset might only inject Phase 1/2 supplements (look for dramatic compositions, favor slow dissolves). The "Silent Vlog" preset uses all three phases because it needs to generate the text narrative layer.

---

## The Silent Vlog Preset

The first and primary style preset, based on the [Silent Vlog technique guide](ideas/The-Art-of-the-Silent-Vlog-A-Guide-to-Text-Driven-Narrative-Vlogging.md).

### What It Does at Each Phase

#### Phase 1 Supplement (Per-Clip Review)

Appended to `CLIP_REVIEW_PROMPT`, instructs the LLM to additionally evaluate:

- **Negative space**: Where in the frame is there room for text overlays? (blank walls, clear skies, empty surfaces, out-of-focus backgrounds)
- **Visual calm**: Which segments have slow, deliberate visual pacing suitable for overlaying text? Avoid fast motion or busy compositions.
- **Ambient audio quality**: Rate the ASMR potential — crisp foley sounds (knife cuts, kettle hiss, pencil scratch), natural ambience, intentional silence. These carry the narrative between text moments.
- **Text-friendly moments**: Flag specific timestamps where the visual composition invites a text overlay (static wide shot, slow pan, establishing shot).
- **Speech vs. silence ratio**: Quantify how much of the clip has dialogue vs. ambient-only audio. Silent vlog style prefers ambient-heavy footage.

This ensures Phase 1 reviews contain the metadata Phase 2 and Phase 3 need to make style-appropriate decisions.

#### Phase 2 Supplement (Editorial Assembly)

Appended to `EDITORIAL_ASSEMBLY_PROMPT`, instructs the LLM to:

- **Pacing**: Build an **Action → Reflection → Action** rhythm. After every action-oriented segment, include a calm/b-roll segment that serves as breathing room for text.
- **Segment selection**: Favor b-roll, establishing, and reflection-purpose segments. Minimize talking-head segments. When speech exists, prefer ambient speech (overheard, background) over direct-to-camera.
- **Story arc**: Structure as **Grounding Hook** (first 15-20% of video) → **Wandering Middle** (next 60%) → **Resolution** (final 20%). The hook sets mood, the middle alternates between tasks and philosophical drift, the resolution returns to the present with acceptance.
- **Timeline allocation**: Ensure at least 10-15% of the total timeline is visually calm enough for text overlays.
- **Audio strategy**: Prefer ambient soundscapes over music beds. When music is used, keep it soft and unobtrusive (lo-fi, acoustic instrumental). Never let music compete with the silence.
- **Transitions**: Favor soft cuts, slow dissolves, and fade-to-black. No fast jump cuts.

#### Phase 3: Visual Monologue Generation (NEW)

A new LLM call that takes the completed storyboard and generates the text overlay plan.

**Input:**
- Editorial storyboard (segments, story arc, cast, pacing notes)
- Per-clip transcripts (speech intervals to avoid)
- User context (tone, people, occasion)

**Output: `MonologuePlan`** — a structured plan containing:

**Persona selection** — the LLM picks one narrative voice:
| Persona | Voice | Example |
|---------|-------|---------|
| Conversational Confidant | Direct address, "we"/"you", close-friend warmth | "did you also have a week that felt a month long? let's just breathe for a bit." |
| Detached Observer | Documentary-like, reflective, gentle melancholy | "looking at this footage now, i realize how much the morning light changes by october." |
| Stream of Consciousness | Random, relatable, humorous inner thoughts | "i should really clean the baseboards. ...actually, maybe next year. coffee first." |

**Written tone mechanics:**
- **Lowercase whisper**: All text is lowercase — soft, unassuming, intimate
- **Ellipses as breathing room**: `...` represents passage of time or a deep sigh
- **Micro-pacing**: Break one sentence across multiple overlays on consecutive segments

**Text overlays** — each overlay specifies:
- Which storyboard segment it belongs to (`segment_index`)
- The text content (5-8 words, lowercase)
- When it appears relative to the segment start (`appear_at` seconds)
- How long it stays on screen (`duration_sec`, minimum: word_count * 0.4s per two-breath rule)
- Visual-text synergy: **harmony** (text matches visual mood) or **dissonance** (text undercuts for humor/relatability)
- Position and style (font, size, alignment)

**Monologue arc** — text overlays follow the same three-act structure as the video:
1. **Grounding Hook**: Acknowledge viewer, set current emotional state
2. **Wandering Middle**: Alternate between task-related observations and deeper philosophical drift
3. **Resolution**: Return to present, warm sign-off

**Constraints enforced in prompt:**
- No text over segments with audible speech (uses transcript to identify speech intervals)
- Minimum 3 seconds gap between consecutive overlays
- 10-15% of total video duration should have text on screen
- Text only on visually calm segments (b-roll, reflection, establishing shots)
- Two-breath rule: `duration_sec >= word_count * 0.4`

### Creator Reference Styles

The Silent Vlog preset can be further refined with creator-inspired sub-styles (future work):

| Creator | Style Key | Characteristics |
|---------|-----------|-----------------|
| sueddu | `cinema_diary` | Philosophical, themes of loneliness and independence, cinematic framing |
| Onuk | `urban_observer` | Quick observations, humor, close-friend voice, city life |
| PlanD | `craft_mentor` | Instructional + life advice, mentoring warmth, skill-focused |
| Hyo-byeol | `seasonal_reflector` | Domestic changes, passing seasons, deep calming reflections |
| Liziqi | `cinematic_rural` | Large-scale rural craft, minimal text, visuals-forward |

---

## Data Models

### Style Preset (in `models.py` or `style_presets.py`)

```python
class StylePreset(BaseModel):
    key: str                           # "silent_vlog"
    label: str                         # "Silent Vlog (Visual Monologue)"
    description: str                   # one-liner for TUI
    phase1_supplement: str             # appended to clip review prompt
    phase2_supplement: str             # appended to editorial assembly prompt
    has_phase3: bool = False           # whether this preset activates Phase 3
    phase3_prompt: str = ""            # Phase 3 prompt template
    creator_references: list[str] = [] # style inspirations
```

### Monologue Output (in `models.py`)

```python
class TextOverlayStyle(BaseModel):
    font: str = "sans-serif"           # "sans-serif" | "handwritten"
    case: str = "lowercase"            # "lowercase" | "sentence"
    size: str = "medium"               # "small" | "medium" | "large"
    position: str = "lower_third"      # "lower_third" | "center" | "upper_third"
    alignment: str = "left"            # "left" | "center" | "right"

class MonologueOverlay(BaseModel):
    index: int                         # sequential order
    segment_index: int                 # which storyboard segment
    text: str                          # the overlay text (lowercase)
    appear_at: float                   # seconds from segment start
    duration_sec: float                # on-screen duration
    style: TextOverlayStyle = TextOverlayStyle()
    synergy: str = "harmony"           # "harmony" | "dissonance"
    note: str = ""                     # editorial note

class MonologuePlan(BaseModel):
    persona: str                       # persona key
    persona_description: str           # voice characterization
    tone_mechanics: list[str] = []     # techniques used
    arc_structure: list[str] = []      # arc sections present
    overlays: list[MonologueOverlay]   # ordered text overlays
    total_text_time_sec: float         # sum of overlay durations
    pacing_notes: list[str] = []       # rhythm notes
    music_sync_notes: list[str] = []   # music-text interaction notes
```

---

## Pipeline Flow (with Style Preset)

```
User selects style + optional preset
         │
         ▼
┌──────────────────────┐
│  Phase 1: Clip Review│ ◄── preset.phase1_supplement
│  (per-clip, parallel)│     (e.g., evaluate negative space,
│                      │      ambient audio, text-friendly moments)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Briefing            │ ◄── user context (unchanged)
│  (user Q&A or scan)  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Phase 2: Assembly   │ ◄── preset.phase2_supplement
│  (storyboard)        │     (e.g., Action→Reflection rhythm,
│                      │      soft transitions, ambient-first audio)
└──────────┬───────────┘
           │
           ▼ (only if preset.has_phase3)
┌──────────────────────┐
│  Phase 3: Monologue  │ ◄── preset.phase3_prompt
│  (text overlay plan) │     (persona, writing rules, arc,
│                      │      pacing, visual-text synergy)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  Render & Cut        │ ◄── monologue plan (optional)
│  (HTML preview +     │     (drawtext filters for ffmpeg)
│   ffmpeg assembly)   │
└──────────────────────┘
```

---

## ffmpeg Text Rendering

Text overlays are rendered during segment extraction using ffmpeg's `drawtext` filter.

Since `MonologueOverlay.appear_at` is relative to segment start, overlays are applied per-segment in `_extract_segment()`:

```
ffmpeg ... -vf "scale=1920:1080,...,
  drawtext=text='the city decided to wash itself clean...':
    fontfile=/System/Library/Fonts/Helvetica.ttc:
    fontsize=36:fontcolor=white@0.95:
    x=(w-tw)/2:y=h*0.78:
    enable='between(t,3.0,7.2)'"
```

**Font mapping (macOS):**
- `"sans-serif"` → `/System/Library/Fonts/Helvetica.ttc` (clean, modern)
- `"handwritten"` → `/System/Library/Fonts/Noteworthy.ttc` (intimate, diary-like)

**Position mapping:**
- `"lower_third"` → `y=h*0.78` (safe zone, standard subtitle area)
- `"center"` → `y=(h-th)/2` (emphasis moments)
- `"upper_third"` → `y=h*0.15` (when lower frame is busy)

**Text stays readable** by applying a subtle shadow: `shadowcolor=black@0.5:shadowx=2:shadowy=2`

---

## CLI & TUI Integration

### TUI: New Project Flow

```
Video style:
  > vlog
    travel-vlog
    family-video
    event-recap
    cinematic
    short-form

Style preset (optional — adds AI creative direction):
  > None (standard editing)
    Silent Vlog (text-driven narrative, ambient focus, no voice)

Selected: Silent Vlog
  This preset will:
  - Phase 1: Evaluate negative space, ambient audio, text-friendly moments
  - Phase 2: Build reflection-heavy pacing with breathing room for text
  - Phase 3: Generate a Visual Monologue (text overlay narrative)
```

### TUI: Project Actions

When a preset with Phase 3 is active:
```
What would you like to do?
  > Open preview in browser
    Regenerate preview
    Generate visual monologue    ← NEW (only shown for Phase 3 presets)
    Assemble rough cut
    Assemble rough cut with text overlays   ← NEW (only when monologue exists)
    ...
```

### CLI Commands

```bash
vx new my-trip ~/footage/ --preset silent_vlog   # Create with preset
vx analyze my-trip                                 # Phases 1+2+3 (preset auto-loaded)
vx monologue my-trip                               # Phase 3 standalone
vx monologue my-trip --persona detached_observer   # Hint persona
vx monologue my-trip --force                       # Re-generate
vx monologue my-trip --dry-run                     # Cost estimate
vx cut my-trip --overlays                          # Burn text into video
vx status my-trip                                  # Shows monologue version info
```

---

## Versioning & Storage

- Preset key stored in `project.json`: `"style_preset": "silent_vlog"`
- Monologue output: `storyboard/monologue_{provider}_v{N}.json` with `_latest` symlink
- Version phase key: `"monologue"` in `project.json` versions dict
- Rough cut with overlays: `exports/v{N}/rough_cut_overlays.mp4`

---

## Cost Considerations

Phase 3 is a single text-only LLM call (storyboard JSON + transcripts + prompt). No video upload.
- Estimated input: ~5K-15K tokens (storyboard + transcripts + prompt)
- Estimated output: ~2K-5K tokens (monologue plan JSON)
- Cost: roughly 0.3-0.5x of a text-only Phase 2 call

The preset supplements to Phase 1 and Phase 2 add ~200-500 tokens to each prompt — negligible cost increase.
