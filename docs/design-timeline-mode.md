# Timeline Mode: System Design

Timeline Mode is an alternative Phase 2 editorial pipeline that enforces chronological order for vlog-style videos. Instead of giving the LLM all clips at once (Story Mode), it groups footage into scenes, plans the narrative arc, then edits each scene independently with focused goals.

## Why Timeline Mode Exists

When given 20-50 clips at once, LLMs consistently break chronological order in vlogs — reordering scenes "creatively" in ways that destroy the narrative timeline. Timeline Mode enforces chronological order **structurally** (by pipeline design, not by hoping the LLM complies) while preserving full aesthetic freedom within each scene.

## Pipeline Overview

```mermaid
graph TD
    subgraph "Shared Pipeline (unchanged)"
        A["Preprocessing<br/><i>ffmpeg: proxy, frames, audio</i>"] --> B["Briefing<br/><i>CreativeBrief with constraints</i>"]
        B --> C["Transcription<br/><i>mlx-whisper or Gemini</i>"]
        C --> D["Phase 1: Clip Reviews<br/><i>one LLM call per clip</i>"]
    end

    D --> E["<b>Section Grouping</b><br/><i>deterministic</i>"]

    subgraph "Timeline Mode Phase 2"
        E --> F["<b>Storyline Planner</b><br/><i>1 LLM call</i>"]
        F --> G["<b>Opening Hook</b><br/><i>1 LLM call</i>"]
        F --> H1["<b>Section 1 Editor</b><br/><i>1 LLM call</i>"]
        H1 -->|narrative summary| H2["<b>Section 2 Editor</b><br/><i>1 LLM call</i>"]
        H2 -->|narrative summary| H3["<b>Section N Editor</b><br/><i>1 LLM call</i>"]
        G --> I["<b>Merge</b><br/><i>deterministic</i>"]
        H3 --> I
    end

    I --> J["Validation + Auto-Clamp"]
    J --> K["Director Review<br/><i>optional</i>"]
    K --> L["Render + Save<br/><i>JSON, MD, HTML preview</i>"]

    style E fill:#e1f5fe,stroke:#0288d1
    style F fill:#fff3e0,stroke:#f57c00
    style G fill:#fff3e0,stroke:#f57c00
    style H1 fill:#fff3e0,stroke:#f57c00
    style H2 fill:#fff3e0,stroke:#f57c00
    style H3 fill:#fff3e0,stroke:#f57c00
    style I fill:#e1f5fe,stroke:#0288d1
    style J fill:#e1f5fe,stroke:#0288d1
```

**Legend**: Blue = deterministic (no LLM) | Orange = LLM call

**LLM call count**: 2 + N (storyline + hook + one per section). For a 3-section project: 5 LLM calls.

---

## Node-by-Node Design

### 1. Section Grouping (deterministic)

Groups clips into a hierarchical day → scene structure using metadata from the manifest.

```mermaid
graph LR
    M["manifest.json<br/><i>creation_time per clip</i>"] --> SG["Section Grouping"]
    R["Phase 1 Reviews<br/><i>content_type, key_moments</i>"] --> SG
    GAP["Gap Threshold<br/><i>default: 30 min</i>"] --> SG
    SG --> OUT["SectionGroup[]<br/><i>day → scene hierarchy</i>"]

    style SG fill:#e1f5fe
```

| Input | Source | Used For |
|-------|--------|----------|
| `manifest.clips[].creation_time` | ffprobe during preprocessing | Tier 1: group by date |
| `manifest.clips[].duration_sec` | ffprobe during preprocessing | Tier 2: calculate gaps between clips |
| `clip_reviews[].content_type` | Phase 1 LLM review | Label sections (e.g., "talking_head" → "Interview") |
| `clip_reviews[].key_moments` | Phase 1 LLM review | Label sections from high-value moment descriptions |
| `gap_threshold_minutes` | Config (default 30) | Tier 2: split within date when gap exceeds threshold |

**Algorithm**:
1. Parse `creation_time` (ISO 8601) → group clips by calendar date
2. Within each date, sort by time → split when gap between consecutive clips exceeds threshold
3. Enrich section labels from Phase 1 review content

**Output**: `list[SectionGroup]`
```
SectionGroup
  ├── group_id: "day1"
  ├── date: "2026-04-05"
  ├── label: "Day 1 — Apr 05"
  └── sections:
        ├── Section(section_id="day1_scene1", label="Rose garden", clip_ids=[...], time_range="09:39-10:05")
        ├── Section(section_id="day1_scene2", label="River park", clip_ids=[...], time_range="10:29-10:29")
        └── Section(section_id="day1_scene3", label="Restaurant", clip_ids=[...], time_range="11:00-11:12")
```

**Artifact saved**: `storyboard/sections_latest.json`

---

### 2. Storyline Planner (1 LLM call)

The "editor's planning session" — sees everything, distributes work to sections.

```mermaid
graph LR
    subgraph "Inputs"
        SG["SectionGroup[]"]
        CR["Condensed Clip Reviews<br/><i>grouped by section</i>"]
        FB["Full Creative Brief<br/><i>Tier 1: CONSTRAINTS<br/>Tier 2: Creative Direction<br/>Tier 3: Preferences</i>"]
        CAST["Deduplicated Cast"]
        HL["Highlights / Avoid<br/><i>extracted from brief</i>"]
    end

    subgraph "LLM Call"
        SP["Storyline Planner<br/><i>model: gemini-3-flash-preview<br/>temp: 0.6<br/>schema: SectionPlan</i>"]
    end

    SG --> SP
    CR --> SP
    FB --> SP
    CAST --> SP
    HL --> SP

    SP --> OUT["SectionPlan"]

    style SP fill:#fff3e0
```

**What the LLM sees per section** (condensed reviews, not just summaries):
```
### Rose garden (day1_scene1)
Day: Day 1 — Apr 05 | Time: 09:39-10:05
Clips: 37

  **IMG_9798** (5s) — ['landscape']
    - [2s] Wide shot of Taipei Rose Garden from entrance (value: high)
  **IMG_9816** (8s) — ['establishing']
    - [3s] Entrance sign with festival banner (value: high)
    - Speech: "We are now at the Taipei Rose Garden..."
  ...
```

**Constraint distribution instruction**:
```
CONSTRAINT DISTRIBUTION:
- MUST INCLUDE: the entrance sign of the rose garden, event infos, ...
- MUST EXCLUDE: ...

For each constraint, determine WHICH SECTION can satisfy it based on
the clip reviews. Assign it to that section's must_include or must_exclude.
DO NOT assign a constraint to a section that lacks the relevant footage.
```

**Output**: `SectionPlan`

| Field | Purpose |
|-------|---------|
| `title` | Creative video title |
| `story_concept` | 2-3 sentence narrative thesis |
| `section_narratives[]` | Per-section assignments (see below) |
| `hook_section_id` | Which section provides hook material |
| `hook_description` | What the hook should show |
| `constraint_satisfaction` | Explains any unresolvable constraints |
| `pacing_notes` | Overall rhythm strategy |
| `music_direction` | Audio approach |

**Per-section narrative** (`SectionNarrative`):

| Field | Purpose |
|-------|---------|
| `section_id` | Links to `Section.section_id` |
| `narrative_role` | What this section contributes to the arc |
| `arc_phase` | opening_context / rising_action / experience / climax / closing_reflection |
| `energy` | high / medium / low |
| `target_duration_sec` | Suggested duration |
| `section_goal` | **Focused editorial objective** for this section |
| `must_include` | **Constraints assigned to THIS section** (not global) |
| `must_exclude` | Avoidance constraints for this section |
| `key_clips` | Specific clip_ids to prioritize |

**Artifact saved**: `storyboard/storyline_latest.json`

---

### 3. Opening Hook (1 LLM call)

Creates a cinematic 10-15 second teaser from the best moments across all sections.

```mermaid
graph LR
    subgraph "Inputs"
        HV["High-Value Clips<br/><i>key_moments with<br/>editorial_value = 'high'</i>"]
        PLAN["SectionPlan<br/><i>story_concept<br/>hook_description</i>"]
        CAST["Cast"]
    end

    subgraph "LLM Call"
        HC["Hook Creator<br/><i>model: gemini-3-flash-preview<br/>temp: 0.3<br/>schema: HookStoryboard</i>"]
    end

    HV --> HC
    PLAN --> HC
    CAST --> HC

    HC --> OUT["HookStoryboard<br/><i>2-5 segments, ~12s</i>"]

    style HC fill:#fff3e0
```

| Input | Source | Details |
|-------|--------|---------|
| High-value clips | Filtered from all clip reviews | Only clips with at least one `key_moment.editorial_value == "high"` |
| `section_plan.story_concept` | Storyline output | Narrative context for hook tone |
| `section_plan.hook_description` | Storyline output | Specific direction for what hook should show |

**Output**: `HookStoryboard` — 2-5 `Segment` objects (~10-15s total), plus `hook_concept` explanation.

**Instructions**: Quick cuts (2-4s each), `audio_note = "music_bed"`, `transition = "cut"` for energy.

---

### 4. Per-Section Editor (N sequential LLM calls)

Each section is edited independently with focused goals. Sections run sequentially so each receives a narrative summary from all prior sections.

```mermaid
graph TD
    subgraph "Section 1 Inputs"
        SN1["SectionNarrative<br/><i>section_goal<br/>must_include<br/>key_clips</i>"]
        CR1["Section 1 Clip Reviews<br/><i>full reviews + transcripts</i>"]
        CD1["Creative Direction<br/><i>Tier 2+3 only<br/>(no global constraints)</i>"]
    end

    subgraph "Section 1 LLM"
        E1["Section 1 Editor<br/><i>temp: 0.3</i>"]
    end

    SN1 --> E1
    CR1 --> E1
    CD1 --> E1

    E1 --> SSB1["SectionStoryboard 1<br/><i>segments[]<br/>narrative_summary</i>"]

    SSB1 -->|"narrative_summary<br/>(2-3 sentences)"| CUM["Cumulative Context"]

    subgraph "Section 2 Inputs"
        SN2["SectionNarrative 2"]
        CR2["Section 2 Clip Reviews"]
        CD2["Creative Direction"]
    end

    CUM --> E2["Section 2 Editor"]
    SN2 --> E2
    CR2 --> E2
    CD2 --> E2

    E2 --> SSB2["SectionStoryboard 2"]
    SSB2 -->|narrative_summary| CUM2["Cumulative Context 1+2"]

    CUM2 --> E3["Section N Editor"]
    E3 --> SSBN["SectionStoryboard N"]

    style E1 fill:#fff3e0
    style E2 fill:#fff3e0
    style E3 fill:#fff3e0
```

**What each section editor receives**:

| Input | Source | Details |
|-------|--------|---------|
| `section_narrative.section_goal` | Storyline output | "Establish the garden atmosphere with entrance shots and flower close-ups" |
| `section_narrative.must_include` | Storyline output | Only constraints this section CAN satisfy |
| `section_narrative.must_exclude` | Storyline output | Only avoidances relevant here |
| `section_narrative.key_clips` | Storyline output | Specific clips to prioritize |
| Section clip reviews | Phase 1 reviews, filtered | Full reviews with key_moments, usable_segments |
| Section transcripts | Transcription, filtered | Speech text for natural cut points |
| Creative direction | Brief (Tier 2+3) | Intent, style, pacing — NO global constraints |
| Cumulative narratives | Prior section outputs | "Section 1 covered the garden entrance and flower beds..." |
| Style supplement | Style preset | Additional creative guidance |

**Key instruction**: "Within YOUR section, order clips for the best aesthetic flow. B-roll, signs, and close-ups can go wherever they serve the narrative best. You are NOT bound to chronological order within this section."

**Output**: `SectionStoryboard`

| Field | Purpose |
|-------|---------|
| `segments[]` | Ordered segments with in_sec/out_sec timestamps |
| `narrative_summary` | 2-3 sentences passed to next section as context |
| `discarded[]` | Clips from this section not used, with reasons |
| `cast[]` | People identified in this section |
| `music_cue` | Music strategy for this section |
| `editorial_reasoning` | Thinking process |

---

### 5. Merge (deterministic)

Combines hook + all section storyboards into the final `EditorialStoryboard`.

```mermaid
graph LR
    subgraph "Inputs"
        H["HookStoryboard<br/><i>2-5 segments</i>"]
        S1["SectionStoryboard 1<br/><i>segments, cast, discarded</i>"]
        S2["SectionStoryboard 2"]
        SN["SectionStoryboard N"]
        SP["SectionPlan<br/><i>narratives, title, concept</i>"]
    end

    subgraph "Merge"
        M["merge_section_storyboards()<br/><i>deterministic</i>"]
    end

    H --> M
    S1 --> M
    S2 --> M
    SN --> M
    SP --> M

    M --> OUT["EditorialStoryboard<br/><i>same model as Story Mode</i>"]

    style M fill:#e1f5fe
```

**Merge operations**:
1. **Segments**: Hook first, then each section in order → re-index 0..N sequentially
2. **Story arc**: One `StoryArcSection` per section (title from narrative_role, description from narrative_summary)
3. **Cast**: Union all, deduplicate by normalized name
4. **Discarded**: Union all
5. **Music plan**: Collect all section music cues
6. **Editorial reasoning**: Concatenate `[Hook] ...`, `[day1_scene1] ...`, `[day1_scene2] ...`
7. **Metadata**: title, style, story_concept from SectionPlan; duration computed from segments

**Output**: Standard `EditorialStoryboard` — fully backward compatible with Story Mode output. All downstream code (render, rough cut, FCPXML, director review, eval) works unchanged.

---

### 6. Post-Processing (shared with Story Mode)

1. **Clip ID resolution** — fixes LLM abbreviations (`C0073` → `20260330114125_C0073`)
2. **Timestamp auto-clamping** — clamps each segment to its clip's usable_segment bounds
3. **Validation** — checks clip_id existence, in < out, duration bounds, no duplicate indices
4. **Director review** (optional) — autonomous agent reviews the merged storyboard

---

## Constraint Distribution Flow

The critical design decision: constraints are resolved at the planning stage, not at the section editing stage.

```mermaid
sequenceDiagram
    participant Brief as Creative Brief
    participant SL as Storyline Planner
    participant S1 as Section 1 Editor
    participant S2 as Section 2 Editor
    participant S3 as Section 3 Editor

    Brief->>SL: MUST INCLUDE: entrance sign, restaurant logo, flower close-ups, airplane
    Brief->>SL: MUST EXCLUDE: shaky footage

    Note over SL: Sees ALL clip reviews per section.<br/>Knows entrance sign is in Section 1,<br/>restaurant logo is in Section 3.

    SL->>S1: must_include: ["entrance sign", "flower close-ups", "airplane"]<br/>section_goal: "Establish garden with entrance and variety"
    SL->>S2: must_include: []<br/>section_goal: "Personal reflection at the river park"
    SL->>S3: must_include: ["restaurant logo"]<br/>section_goal: "Capture the hotpot dining experience"
    SL->>S1: must_exclude: ["shaky footage"]
    SL->>S2: must_exclude: ["shaky footage"]
    SL->>S3: must_exclude: ["shaky footage"]

    Note over S1: Only sees garden clips.<br/>Only responsible for entrance sign + flowers.
    Note over S3: Only sees restaurant clips.<br/>Only responsible for restaurant logo.
```

---

## File Reference

| File | Key Functions |
|------|--------------|
| `editorial_agent.py` | `_run_phase2_sections()` — pipeline orchestration |
| `editorial_prompts.py` | `build_storyline_prompt()`, `build_hook_prompt()`, `build_section_storyboard_prompt()` |
| `section_grouping.py` | `group_clips_into_sections()`, `merge_section_storyboards()`, `format_sections_for_display()` |
| `models.py` | `Section`, `SectionGroup`, `SectionNarrative`, `SectionPlan`, `SectionStoryboard`, `HookStoryboard` |
| `briefing.py` | `format_brief_for_prompt(skip_constraints=True\|False)` |
| `config.py` | `GeminiConfig.use_timeline_mode`, `.section_gap_minutes` |
