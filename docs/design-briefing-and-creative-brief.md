# Briefing, Creative Brief & Style Preset System

System design document covering the full user context pipeline: how raw footage becomes creative direction that guides every LLM call in the editing pipeline.

---

## 1. High-Level Pipeline Overview

Where briefing artifacts sit in the overall pipeline and which stages consume them.

```mermaid
graph TD
    RAW["<b>Raw Footage</b><br/>N clips in library/project/clips/"]

    subgraph "Preprocessing (deterministic)"
        PP["ffmpeg<br/>4K->360p proxy, frames,<br/>scene detect, audio extract"]
    end

    subgraph "Briefing Pipeline"
        QS["<b>Quick Scan</b><br/><i>1 Gemini LLM call</i>"]
        TUI["<b>TUI Questionnaire</b><br/>quick / director / deep / file"]
        CB["<b>CreativeBrief</b><br/><i>user_context_v{N}.json</i>"]
    end

    subgraph "Style System"
        SP["<b>StylePreset</b><br/>e.g. Silent Vlog"]
        SUPP["phase1_supplement<br/>phase2_supplement<br/>phase3_prompt"]
    end

    subgraph "Downstream LLM Stages"
        TR["<b>Transcription</b><br/>speaker names from brief"]
        P1["<b>Phase 1</b><br/>per-clip review"]
        P2["<b>Phase 2</b><br/>editorial assembly"]
        P3["<b>Phase 3</b><br/>visual monologue<br/><i>(optional, preset-dependent)</i>"]
    end

    RAW --> PP
    PP -->|"proxy videos"| QS
    QS -->|"scan results<br/>(people, activities, mood,<br/>suggested questions)"| TUI
    TUI --> CB
    SP --> SUPP

    CB -->|"speaker names"| TR
    CB -->|"Tier 1-3 prompt block"| P1
    CB -->|"Tier 1-3 prompt block"| P2
    CB -->|"Tier 3 preferences"| P3
    SUPP -->|"phase1_supplement"| P1
    SUPP -->|"phase2_supplement"| P2
    SUPP -->|"phase3_prompt template"| P3

    style QS fill:#fff3e0,stroke:#f57c00
    style TUI fill:#e3f2fd,stroke:#1565c0
    style CB fill:#e8f5e9,stroke:#388e3c
    style SP fill:#f3e5f5,stroke:#7b1fa2
    style SUPP fill:#f3e5f5,stroke:#7b1fa2
    style P1 fill:#fff3e0,stroke:#f57c00
    style P2 fill:#fff3e0,stroke:#f57c00
    style P3 fill:#fff3e0,stroke:#f57c00
```

---

## 2. Quick Scan (Gemini File API + Structured Output)

The AI's first look at the footage. One cheap LLM call that produces structured observations used to ask smarter briefing questions.

```mermaid
graph LR
    subgraph "Inputs"
        PROXIES["<b>Proxy Videos</b><br/>all clips, 360p"]
        CONCAT["<b>concat_proxies()</b><br/><i>deterministic</i><br/>chronological bundles<br/><=40 min each"]
        CACHE["<b>file_api_cache.json</b><br/><i>Gemini File URIs</i><br/>46-hour TTL"]
    end

    subgraph "Upload"
        UP["<b>Gemini File API</b><br/>upload bundles<br/>(reuse cached URIs)"]
    end

    subgraph "LLM Call"
        LLM["<b>Quick Scan</b><br/><i>model: gemini-2.5-flash</i><br/><i>temp: 0.3</i><br/><i>schema: QuickScanResult</i>"]
    end

    PROXIES --> CONCAT
    CONCAT -->|"bundle .mp4 files"| UP
    CACHE -->|"cached URIs<br/>(skip re-upload)"| UP
    UP -->|"video parts +<br/>QUICK_SCAN_PROMPT +<br/>concat timeline"| LLM

    LLM --> OUT["<b>QuickScanResult</b><br/>overall_summary<br/>people: PersonSighting[]<br/>activities: str[]<br/>mood: str<br/>suggested_questions: str[]<br/>clip_summaries: ClipSummary[]"]

    OUT --> SAVE["<b>quick_scan_v{N}.json</b><br/>+ .meta.json sidecar<br/>+ _latest symlink"]
    UP -->|"new URIs"| CACHE

    style LLM fill:#fff3e0,stroke:#f57c00
    style OUT fill:#e8f5e9,stroke:#388e3c
    style CACHE fill:#e1f5fe,stroke:#0288d1
    style CONCAT fill:#e1f5fe,stroke:#0288d1
    style SAVE fill:#e8f5e9,stroke:#388e3c
```

### QuickScanResult Schema

| Field | Type | Description |
|-------|------|-------------|
| `overall_summary` | `str` | 2-3 sentences about the footage as a whole |
| `people` | `PersonSighting[]` | Each: description, estimated_appearances, role_guess |
| `activities` | `str[]` | Activities, locations, events observed |
| `mood` | `str` | Overall energy/vibe |
| `suggested_questions` | `str[]` | Context questions the AI wants answered |
| `clip_summaries` | `ClipSummary[]` | Per-clip one-liner + energy level |

---

## 3. Smart Briefing Flow (TUI Questionnaire)

Interactive user interview with four depth levels, producing a `CreativeBrief`.

```mermaid
graph TD
    START["<b>run_smart_briefing()</b>"]

    %% ── Existing context check ─────────────────────────────────
    START --> CHECK{"Existing<br/>user_context_v{N}.json?"}

    CHECK -->|"yes"| SHOW["Display existing fields<br/>(people, activity, tone, etc.)"]
    SHOW --> REUSE{"Use existing<br/>context?"}
    REUSE -->|"Use as-is"| DONE_REUSE["Return existing dict"]
    REUSE -->|"Edit it"| EDIT["<b>_edit_existing()</b><br/>questionary.text() per string field<br/><i>skip non-string fields (context_qa)</i>"]
    EDIT --> SAVE
    REUSE -->|"Re-scan"| SCAN
    REUSE -->|"Skip"| DONE_SKIP["Return None"]

    CHECK -->|"no"| SCAN["<b>run_quick_scan()</b><br/><i>see Section 2</i>"]

    SCAN --> SCANOK{"Scan<br/>succeeded?"}
    SCANOK -->|"no"| FALLBACK["<b>_ask_questions()</b><br/>legacy manual briefing<br/>(no AI context)"]
    SCANOK -->|"yes"| DISPLAY["<b>_display_scan_results()</b><br/>people, activities, mood"]

    DISPLAY --> DEPTH{"<b>Briefing depth?</b>"}

    %% ── Depth branches ─────────────────────────────────────────
    DEPTH -->|"quick"| QUICK["<b>Quick Brief</b><br/>3 questions, ~30s"]
    DEPTH -->|"director"| DIR["<b>Director's Brief</b><br/>9 questions, ~2 min"]
    DEPTH -->|"deep"| DEEP["<b>Deep Brief</b><br/>all fields, ~5 min"]
    DEPTH -->|"file"| FILE["<b>_brief_from_file()</b><br/>creative_brief.md"]
    DEPTH -->|"skip"| DONE_SKIP

    QUICK --> CB
    DIR --> CB
    DEEP --> CB
    FILE --> CB
    FALLBACK --> SAVE

    CB["<b>CreativeBrief</b><br/>(v2, Pydantic model)"]
    CB --> SAVE["<b>save_creative_brief()</b><br/>user_context_v{N}.json<br/>+ .meta.json<br/>+ _latest symlink"]

    SAVE --> DONE["Return brief as dict"]

    style SCAN fill:#fff3e0,stroke:#f57c00
    style QUICK fill:#e3f2fd,stroke:#1565c0
    style DIR fill:#e3f2fd,stroke:#1565c0
    style DEEP fill:#e3f2fd,stroke:#1565c0
    style FILE fill:#f3e5f5,stroke:#7b1fa2
    style CB fill:#e8f5e9,stroke:#388e3c
    style SAVE fill:#e8f5e9,stroke:#388e3c
    style EDIT fill:#e3f2fd,stroke:#1565c0
```

---

## 4. Briefing Depth Comparison

What each depth level asks and which `CreativeBrief` fields it populates.

```mermaid
graph TD
    subgraph "Quick Brief (3 Qs, ~30s)"
        Q1_Q["People<br/><i>(pre-populated from scan)</i>"]
        Q2_Q["Activity"]
        Q3_Q["Must-include / exclude<br/><i>(combined single question)</i>"]
    end

    subgraph "Director's Brief (9 Qs, ~2 min)"
        Q1_D["People"]
        Q2_D["Activity"]
        Q3_D["AI-suggested Q&A<br/><i>(from scan.suggested_questions)</i>"]
        Q4_D["Intent<br/><i>'what should viewers feel?'</i>"]
        Q5_D["Audience<br/><i>platform selector</i>"]
        Q6_D["Tone<br/><i>preset selector + custom</i>"]
        Q7_D["Pacing<br/><i>slow / balanced / punchy / builds</i>"]
        Q8_D["Must-include"]
        Q9_D["Must-exclude"]
        Q10_D["Duration"]
    end

    subgraph "Deep Brief (~5 min)"
        DALL["All Director's questions<br/>+"]
        D1["Story thesis"]
        D2["Key beats<br/><i>(comma-separated, ordered)</i>"]
        D3["Story hook / opening"]
        D4["Ending note"]
        D5["Structure<br/><i>chrono / thematic / circular / vignettes</i>"]
        D6["Music direction"]
        D7["Visual tone"]
        D8["Style references"]
        D9["Free notes"]
    end

    subgraph "File Mode"
        FM["<b>creative_brief.md</b><br/><i>opened in $EDITOR</i><br/>freeform prose/bullets<br/>any format"]
        FM --> PARSE["<b>parse_creative_brief_md()</b><br/>strip boilerplate<br/>extract raw text"]
        PARSE --> CDT["CreativeBrief with<br/>creative_direction_text = raw text"]
    end

    Q1_Q --> BQ["<b>CreativeBrief</b><br/>people, activity, highlights"]
    Q1_D --> BD["<b>CreativeBrief</b><br/>+ intent, audience, tone,<br/>pacing, context_qa, duration"]
    DALL --> BDP["<b>CreativeBrief</b><br/>+ narrative (thesis, beats,<br/>hook, ending, structure)<br/>+ style (music, visual_tone)<br/>+ references, notes"]

    style BQ fill:#e8f5e9,stroke:#388e3c
    style BD fill:#e8f5e9,stroke:#388e3c
    style BDP fill:#e8f5e9,stroke:#388e3c
    style CDT fill:#e8f5e9,stroke:#388e3c
    style FM fill:#f3e5f5,stroke:#7b1fa2
```

---

## 5. File-Based Creative Direction (`creative_brief.md`)

```mermaid
graph LR
    subgraph "Generation"
        SCAN2["QuickScanResult<br/><i>(optional context)</i>"]
        GEN["<b>generate_creative_brief_md()</b><br/>lightweight guide, NOT a form<br/>includes scan observations<br/>as inspiration"]
    end

    subgraph "User Editing"
        MD["<b>creative_brief.md</b><br/><i>opened in $EDITOR</i><br/>user writes freeform vision:<br/>prose, bullets, stream of<br/>consciousness"]
    end

    subgraph "Parsing"
        PARSE2["<b>parse_creative_brief_md()</b><br/>1. strip HTML comments<br/>2. strip heading<br/>3. strip boilerplate before marker<br/>4. return cleaned text"]
    end

    SCAN2 --> GEN
    GEN -->|"writes file<br/>if not exists"| MD
    MD -->|"user edits<br/>and saves"| PARSE2
    PARSE2 --> BRIEF2["<b>CreativeBrief</b><br/>brief_version: 2<br/>source: 'file'<br/>creative_direction_text: raw text"]

    style MD fill:#f3e5f5,stroke:#7b1fa2
    style BRIEF2 fill:#e8f5e9,stroke:#388e3c
```

---

## 6. CreativeBrief Model

The single data structure that carries all user creative direction through the pipeline.

```mermaid
graph TD
    subgraph "Legacy Fields (v1 compatible)"
        L1["people: str"]
        L2["activity: str"]
        L3["tone: str"]
        L4["highlights: str"]
        L5["avoid: str"]
        L6["duration: str"]
        L7["context_qa: list[dict]<br/><i>[{question, answer}]</i>"]
    end

    subgraph "Enhanced Fields (v2)"
        E1["intent: str<br/><i>'what should viewers feel?'</i>"]
        E2["audience: AudienceSpec<br/><i>platform, viewer</i>"]
        E3["narrative: NarrativeDirection<br/><i>thesis, hook, key_beats,<br/>ending, structure</i>"]
        E4["style: StyleDirection<br/><i>pacing, music, energy,<br/>transitions, visual_tone</i>"]
        E5["references: list[str]"]
        E6["notes: str"]
        E7["creative_direction_text: str<br/><i>raw freeform from file</i>"]
    end

    subgraph "Metadata"
        M1["brief_version: 1 or 2"]
        M2["source: 'tui' | 'file' | 'preset'"]
        M3["preset_key: str"]
    end

    subgraph "Methods"
        HCD["has_creative_direction() -> bool<br/><i>true if any v2 field populated</i>"]
        TLD["to_legacy_dict() -> dict<br/><i>export people, activity, tone,<br/>highlights, avoid, duration, context_qa</i>"]
    end

    L1 & L2 & L3 & L4 & L5 & L6 & L7 --> CB2["<b>CreativeBrief</b>"]
    E1 & E2 & E3 & E4 & E5 & E6 & E7 --> CB2
    M1 & M2 & M3 --> CB2
    CB2 --> HCD
    CB2 --> TLD

    style CB2 fill:#e8f5e9,stroke:#388e3c
```

---

## 7. Three-Tier Prompt Formatting

How `CreativeBrief` is transformed into prompt text injected into LLM calls via `format_brief_for_prompt()`.

```mermaid
graph TD
    CB3["<b>CreativeBrief</b>"]

    CB3 --> ROUTE{"brief_version<br/>& has_creative_direction()?"}

    ROUTE -->|"v1 or no enhanced fields"| LEGACY["<b>format_context_for_prompt()</b><br/><i>legacy two-section format</i>"]
    ROUTE -->|"v2 with enhanced fields"| THREE["<b>format_brief_for_prompt()</b><br/><i>three-tier hierarchy</i>"]

    subgraph "Tier 1: CONSTRAINTS (non-negotiable)"
        T1A["MUST INCLUDE: {highlights}"]
        T1B["MUST EXCLUDE: {avoid}"]
        T1C["'If you cannot satisfy a constraint,<br/>you MUST explain why in editorial_reasoning.'"]
    end

    subgraph "Tier 2: CREATIVE DIRECTION (strong guidance)"
        T2_FILE{"creative_direction_text<br/>populated?"}
        T2A["<b>Freeform Path</b><br/>raw text injected as-is<br/><i>'the filmmaker's vision —<br/>read carefully...'</i>"]
        T2B["<b>Structured Path</b>"]
        T2B1["NORTH STAR: {intent}"]
        T2B2["AUDIENCE: {platform} for {viewer}"]
        T2B3["STORY THESIS: {thesis}"]
        T2B4["STRUCTURE: {structure}"]
        T2B5["KEY BEATS / OPENING / ENDING<br/><i>(Phase 2 only)</i>"]
        T2B6["PACING / MUSIC / ENERGY ARC"]
        T2B7["TRANSITIONS / VISUAL TONE<br/><i>(Phase 2 only)</i>"]
        T2B8["REFERENCE STYLE: {references}"]
    end

    subgraph "Tier 3: PREFERENCES (soft hints)"
        T3A["People in the footage: {people}"]
        T3B["Activity/occasion: {activity}"]
        T3C["Desired tone: {tone}"]
        T3D["Duration preference: {duration}"]
        T3E["Additional notes: {notes}"]
        T3F["Q&A pairs from context_qa"]
    end

    THREE --> T1A & T1B & T1C
    THREE --> T2_FILE
    T2_FILE -->|"yes"| T2A
    T2_FILE -->|"no"| T2B
    T2B --> T2B1 & T2B2 & T2B3 & T2B4 & T2B5 & T2B6 & T2B7 & T2B8
    THREE --> T3A & T3B & T3C & T3D & T3E & T3F

    style THREE fill:#e8f5e9,stroke:#388e3c
    style T1A fill:#ffcdd2,stroke:#c62828
    style T1B fill:#ffcdd2,stroke:#c62828
    style T1C fill:#ffcdd2,stroke:#c62828
    style T2A fill:#fff3e0,stroke:#f57c00
    style T2B fill:#fff3e0,stroke:#f57c00
    style T3A fill:#e3f2fd,stroke:#1565c0
    style T3B fill:#e3f2fd,stroke:#1565c0
    style T3C fill:#e3f2fd,stroke:#1565c0
    style T3D fill:#e3f2fd,stroke:#1565c0
    style T3E fill:#e3f2fd,stroke:#1565c0
    style T3F fill:#e3f2fd,stroke:#1565c0
```

**Phase-specific behavior:**
- `phase="phase1"`: Omits key beats, opening, ending, transitions, visual tone (not relevant for single-clip review)
- `phase="phase2"`: Includes all fields (full editorial context for assembly)

---

## 8. Style Preset System

Curated, genre-specific prompt supplements that shape LLM behavior across all phases. Distinct from briefing (user context) — presets are controlled creative templates.

```mermaid
graph TD
    subgraph "StylePreset Model"
        KEY["key: str<br/><i>'silent_vlog'</i>"]
        LABEL["label: str<br/><i>'Visual Monologue Vlog'</i>"]
        DESC["description: str<br/><i>TUI one-liner</i>"]
        P1S["phase1_supplement: str<br/><i>appended to clip review prompt</i>"]
        P2S["phase2_supplement: str<br/><i>appended to editorial assembly prompt</i>"]
        HP3["has_phase3: bool<br/><i>activates Phase 3?</i>"]
        P3P["phase3_prompt: str<br/><i>Phase 3 template with placeholders</i>"]
        REFS["creator_references: list[str]"]
    end

    KEY & LABEL & DESC & P1S & P2S & HP3 & P3P & REFS --> SP2["<b>StylePreset</b>"]

    SP2 -->|"phase1_supplement"| P1INJ["<b>Phase 1 Injection</b><br/>appended to<br/>build_clip_review_prompt()"]
    SP2 -->|"phase2_supplement"| P2INJ["<b>Phase 2 Injection</b><br/>appended to<br/>build_phase2a_reasoning_prompt()<br/>or build_editorial_assembly_prompt()"]
    SP2 -->|"phase3_prompt"| P3INJ["<b>Phase 3 Activation</b><br/>template filled with:<br/>{title}, {duration}, {style},<br/>{story_concept}, {cast},<br/>{story_arc}, {segments_table},<br/>{transcripts}, {user_context}"]

    style SP2 fill:#f3e5f5,stroke:#7b1fa2
    style P1INJ fill:#fff3e0,stroke:#f57c00
    style P2INJ fill:#fff3e0,stroke:#f57c00
    style P3INJ fill:#fff3e0,stroke:#f57c00
```

### Silent Vlog Preset: What Each Supplement Does

| Phase | Supplement Focus | Key Instructions |
|-------|-----------------|------------------|
| **Phase 1** | Text placement evaluation | Rate negative space, text_readability (good/fair/poor), ambient audio quality, speech vs. non-speech segments |
| **Phase 2** | Story structure for visual monologue | Opening 15-20% must establish context (NO speech), scenery-conversation-scenery alternation, 15-20% non-speech for text overlay, mark SPEECH vs SCENERY segments |
| **Phase 3** | Text overlay generation | Persona (conversational/observer/stream), ALL LOWERCASE, 5-8 words, two-breath rule, ONLY on scenery segments, lower_third position, arc structure |

---

## 9. How Briefing & Style Feed Into Each LLM Phase

Concrete data flow showing exactly which artifacts each phase consumes and how.

### 9.1 Phase 1: Per-Clip Review

```mermaid
graph LR
    subgraph "Inputs"
        PROXY["<b>Proxy Video</b><br/>(Gemini) or<br/><b>Extracted Frames</b><br/>(Claude)"]
        TRANS["<b>Transcript</b><br/><i>per-clip, trimmed to<br/>usable ranges</i>"]
        CTX["<b>CreativeBrief</b><br/><i>formatted via<br/>format_brief_for_prompt(<br/>  brief, phase='phase1')</i><br/>Tier 1: CONSTRAINTS<br/>Tier 2: DIRECTION (no beats/hook)<br/>Tier 3: PREFERENCES"]
        STYLE1["<b>style_supplement</b><br/><i>from StylePreset.phase1_supplement</i><br/>e.g. text placement,<br/>speech vs. scenery eval"]
    end

    subgraph "Prompt Assembly"
        BUILD["<b>build_clip_review_prompt()</b><br/>clip_id, filename, duration<br/>+ JSON schema template<br/>+ instructions<br/>+ transcript<br/>+ user_context block<br/>+ style supplement"]
    end

    subgraph "LLM Call"
        P1LLM["<b>Phase 1 Review</b><br/><i>model: gemini-1.5-pro / claude</i><br/><i>schema: ClipReview</i>"]
    end

    PROXY --> BUILD
    TRANS --> BUILD
    CTX --> BUILD
    STYLE1 --> BUILD
    BUILD --> P1LLM
    P1LLM --> OUT1["<b>ClipReview</b><br/>summary, quality, content_type,<br/>people, key_moments,<br/>usable_segments, discard_segments,<br/>audio, editorial_notes"]

    OUT1 --> SAVE1["<b>review_{provider}_v{N}.json</b><br/>+ .meta.json (tracks user_context version)<br/>+ _latest symlink"]

    style P1LLM fill:#fff3e0,stroke:#f57c00
    style CTX fill:#e8f5e9,stroke:#388e3c
    style STYLE1 fill:#f3e5f5,stroke:#7b1fa2
    style OUT1 fill:#e8f5e9,stroke:#388e3c
```

### 9.2 Phase 2: Editorial Assembly (Gemini Split Pipeline)

```mermaid
graph TD
    %% ── Input artifacts ────────────────────────────────────────
    REVIEWS["<b>All Phase 1 Reviews</b><br/>condensed: usable_segments,<br/>key_moments, speakers, quality"]
    TRANS2["<b>All Transcripts</b><br/>trimmed to usable ranges"]
    CTX2["<b>CreativeBrief</b><br/><i>format_brief_for_prompt(<br/>  brief, phase='phase2')</i><br/>Tier 1: CONSTRAINTS<br/>Tier 2: DIRECTION (full)<br/>Tier 3: PREFERENCES"]
    STYLE2["<b>style_supplement</b><br/><i>StylePreset.phase2_supplement</i>"]
    TIMELINE["<b>Filing Timeline</b><br/>chronological shooting order"]
    CAST["<b>Cast List</b><br/>deduplicated from reviews"]

    %% ── Call 2A ────────────────────────────────────────────────
    REVIEWS -->|"condensed clip data<br/>(usable segs, key moments,<br/>speakers, quality)"| C2A
    TRANS2 -->|"inline transcripts<br/>per clip"| C2A
    CTX2 -->|"full three-tier<br/>prompt block"| C2A
    STYLE2 -->|"appended to prompt"| C2A
    TIMELINE --> C2A
    CAST --> C2A

    C2A["<b>Call 2A: Editorial Reasoning</b><br/><i>model: gemini-1.5-pro</i><br/><i>temp: 0.3</i><br/><i>output: freeform text</i><br/>constraint checks, story concept,<br/>segment sequence, discarded clips"]

    C2A --> PLAN["<b>editorial_plan_v{N}.txt</b><br/>freeform editorial reasoning"]

    %% ── Call 2A.5 ──────────────────────────────────────────────
    PLAN -->|"editorial plan text"| C2A5
    C2A5["<b>Call 2A.5: Structuring</b><br/><i>model: gemini-1.5-flash</i><br/><i>temp: 0.2</i><br/><i>schema: StoryPlan</i>"]

    C2A5 --> SPLAN["<b>story_plan_v{N}.json</b><br/>title, style, story_concept,<br/>cast, story_arc,<br/>planned_segments[]:<br/>  clip_id, segment_index,<br/>  purpose, arc_phase,<br/>  audio_strategy"]

    %% ── Call 2B ────────────────────────────────────────────────
    SPLAN -->|"story plan context<br/>(title, concept, pacing)"| C2B
    REVIEWS -->|"per-segment bounded<br/>time windows"| C2B
    TRANS2 -->|"per-segment inline<br/>transcripts"| C2B

    C2B["<b>Call 2B: Assembly</b><br/><i>model: gemini-1.5-pro</i><br/><i>temp: 0.3</i><br/><i>schema: EditorialStoryboard</i>"]

    C2B --> FINAL2["<b>EditorialStoryboard</b><br/>editorial_reasoning,<br/>title, style, story_concept,<br/>cast[], story_arc[],<br/>segments[] (precise in/out timestamps),<br/>discarded[], music_plan[],<br/>pacing_notes, technical_notes"]

    FINAL2 --> SAVE2["<b>editorial_{provider}_v{N}.json</b><br/>+ .meta.json<br/>+ _latest symlink"]

    %% ── Styling ────────────────────────────────────────────────
    style C2A fill:#fff3e0,stroke:#f57c00
    style C2A5 fill:#fff3e0,stroke:#f57c00
    style C2B fill:#fff3e0,stroke:#f57c00
    style CTX2 fill:#e8f5e9,stroke:#388e3c
    style STYLE2 fill:#f3e5f5,stroke:#7b1fa2
    style PLAN fill:#e1f5fe,stroke:#0288d1
    style SPLAN fill:#e1f5fe,stroke:#0288d1
    style FINAL2 fill:#e8f5e9,stroke:#388e3c
```

### 9.3 Phase 3: Visual Monologue (Style-Dependent, Split Pipeline)

Only runs when `StylePreset.has_phase3 == True` (e.g., Silent Vlog).

```mermaid
graph TD
    %% ── Inputs ─────────────────────────────────────────────────
    SB["<b>EditorialStoryboard</b><br/>title, style, story_concept,<br/>cast, story_arc, segments[]"]
    TRANS3["<b>Transcripts</b><br/>per-clip (detect speech segments)"]
    CTX3["<b>CreativeBrief</b><br/><i>format_brief_for_prompt()</i>"]
    P3TMPL["<b>StylePreset.phase3_prompt</b><br/><i>template with placeholders:</i><br/>{title}, {duration}, {style},<br/>{story_concept}, {cast},<br/>{story_arc}, {segments_table},<br/>{transcripts}, {user_context}"]

    %% ── Call M1 ────────────────────────────────────────────────
    SB -->|"segments table:<br/>index, clip_id, duration,<br/>purpose, audio_note,<br/>[HAS SPEECH] flag"| M1
    TRANS3 -->|"verify speech<br/>presence"| M1
    CTX3 --> M1

    M1["<b>Call M1: Segment Analysis</b><br/><i>model: gemini-1.5-pro</i><br/><i>temp: 0.2</i><br/><i>schema: OverlayPlan</i>"]

    M1 --> OPLAN["<b>OverlayPlan</b><br/>persona_recommendation<br/>persona_rationale<br/>eligible_segments[]:<br/>  segment_index, duration,<br/>  arc_phase, intent,<br/>  max_overlay_count"]

    %% ── Call M2 ────────────────────────────────────────────────
    SB --> M2
    TRANS3 --> M2
    OPLAN -->|"eligible segments<br/>+ persona + tone"| M2

    M2["<b>Call M2: Creative Text</b><br/><i>model: gemini-1.5-pro</i><br/><i>temp: 0.8 (creative variance)</i><br/><i>schema: OverlayDrafts</i><br/>Rules: ALL LOWERCASE,<br/>5-8 words, two-breath rule,<br/>ONLY scenery segments,<br/>lower_third position"]

    M2 --> DRAFTS["<b>OverlayDrafts</b><br/>overlays[]:<br/>  segment_index, text,<br/>  appear_at, duration_sec,<br/>  word_count, arc_phase"]

    %% ── Call M3 (deterministic) ────────────────────────────────
    DRAFTS --> M3["<b>Call M3: Validation</b><br/><i>deterministic (no LLM)</i><br/>check eligibility,<br/>timing gaps, duration bounds,<br/>auto-fix minor issues"]

    M3 --> MONO["<b>MonologuePlan</b><br/>persona, persona_description,<br/>tone_mechanics, arc_structure,<br/>overlays[] (validated),<br/>total_text_time_sec,<br/>pacing_notes, music_sync_notes"]

    MONO --> SAVE3["<b>monologue_{provider}_v{N}.json</b><br/>+ overlay_plan_v{N}.json<br/>+ fixlog_v{N}.txt<br/>+ .meta.json + _latest symlink"]

    %% ── Styling ────────────────────────────────────────────────
    style M1 fill:#fff3e0,stroke:#f57c00
    style M2 fill:#fff3e0,stroke:#f57c00
    style M3 fill:#e1f5fe,stroke:#0288d1
    style CTX3 fill:#e8f5e9,stroke:#388e3c
    style P3TMPL fill:#f3e5f5,stroke:#7b1fa2
    style OPLAN fill:#e1f5fe,stroke:#0288d1
    style DRAFTS fill:#e1f5fe,stroke:#0288d1
    style MONO fill:#e8f5e9,stroke:#388e3c
```

---

## 10. Gemini File API Cache (`file_cache.py`)

Shared upload cache that prevents re-uploading proxy videos across pipeline stages.

```mermaid
graph LR
    subgraph "Producers (upload + cache)"
        QS2["<b>Quick Scan</b><br/>uploads concat bundles<br/>keys: _concat_bundle_0, ..."]
        TR2["<b>Transcription</b><br/>uploads per-clip proxies<br/>keys: clip_001, clip_002, ..."]
    end

    subgraph "Cache (file_api_cache.json)"
        FC["<b>file_api_cache.json</b><br/>{clip_id: {uri, cached_at}}<br/>auto-purge >46h<br/>(Gemini retention window)"]
    end

    subgraph "Consumers (reuse cached URIs)"
        QS3["Quick Scan<br/><i>reuse bundle URIs</i>"]
        TR3["Transcription<br/><i>reuse per-clip URIs</i>"]
    end

    QS2 -->|"cache_file_uri()"| FC
    TR2 -->|"cache_file_uri()"| FC
    FC -->|"get_cached_uri()"| QS3
    FC -->|"get_cached_uri()"| TR3

    style FC fill:#e1f5fe,stroke:#0288d1
```

---

## 11. Versioning & Artifact Storage

All briefing artifacts follow the two-phase commit pattern from `versioning.py`.

```mermaid
graph LR
    subgraph "Write Path"
        BEGIN["<b>begin_version()</b><br/>allocate version N<br/>create .meta.json (status: pending)"]
        WRITE["Write artifact file<br/>artifact_v{N}.json"]
        LINK["<b>update_latest_symlink()</b><br/>artifact_latest.json -> v{N}"]
        COMMIT["<b>commit_version()</b><br/>update .meta.json<br/>(status: complete, output_paths)"]
    end

    subgraph "Read Path"
        RESOLVE["<b>resolve_*_path()</b><br/>1. check _latest symlink<br/>2. fallback to bare file<br/>3. return None"]
    end

    subgraph "Briefing Artifacts"
        A1["quick_scan_v{N}.json"]
        A2["user_context_v{N}.json<br/><i>(CreativeBrief)</i>"]
        A3["quick_scan_v{N}.meta.json"]
        A4["user_context_v{N}.meta.json"]
    end

    BEGIN --> WRITE --> LINK --> COMMIT
    RESOLVE --> A1
    RESOLVE --> A2

    style A2 fill:#e8f5e9,stroke:#388e3c
```

### Artifact Summary

| Artifact | File | Producer | Consumers |
|----------|------|----------|-----------|
| Quick Scan | `quick_scan_v{N}.json` | `run_quick_scan()` | TUI questionnaire, `creative_brief.md` generation |
| Creative Brief | `user_context_v{N}.json` | `run_smart_briefing()` / `run_briefing()` | Transcription (speaker names), Phase 1, Phase 2, Phase 3 |
| File API Cache | `file_api_cache.json` | Quick Scan uploads, Transcription uploads | Quick Scan (reuse), Transcription (reuse) |
| Creative Brief MD | `creative_brief.md` | `generate_creative_brief_md()` | `parse_creative_brief_md()` |

---

## Color Legend

| Color | Meaning |
|-------|---------|
| Orange (`#fff3e0`) | LLM call |
| Green (`#e8f5e9`) | Final / saved artifact |
| Blue (`#e1f5fe`) | Deterministic / intermediate artifact |
| Purple (`#f3e5f5`) | Style preset / file-based input |
| Light blue (`#e3f2fd`) | User interaction (TUI) |
| Red (`#ffcdd2`) | Constraint (non-negotiable) |
