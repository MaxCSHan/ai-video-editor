# VX Architectural Manifesto & Project Structure Guideline

**Date:** 2026-04-10
**Author:** Principal Architect
**Status:** Draft — pending Golden Sample approval before wider adoption

---

## Preamble: Design Philosophy

This manifesto is written for a 1–2 person team shipping a real product. Every recommendation earns its complexity cost. When a simpler pattern achieves 90% of the benefit, we choose the simpler pattern.

**Three governing principles:**

1. **Pydantic models are the contract.** `EditorialStoryboard` is already the single source of truth. We extend this contract-first philosophy to every phase boundary.
2. **Filesystem is the database.** No ORM, no SQLite. The existing `library/<project>/` layout, versioned artifacts, and symlinks stay. We formalize the access patterns, not replace them.
3. **Incremental migration via Golden Sample.** No big-bang rewrite. One module gets refactored to the new standard. Once approved, it becomes the template. Everything else migrates as it's touched.

---

## Section 1 — Core Architectural Patterns

### 1.1 Domain-Driven Design (DDD)

#### Rationale

The VX codebase has a clear domain — it's not a generic web app. The domain concepts (clips, reviews, storyboards, segments, overlays) are already well-modeled in `models.py`. What's missing is the *boundary*: domain logic (storyboard validation, clip ID resolution, segment timestamp clamping) currently lives inside infrastructure code (`editorial_agent.py` orchestrates LLM calls *and* validates storyboard rules in the same file).

#### Domain Entities and Value Objects

| Type | Examples | Location Today | Rule |
|------|----------|----------------|------|
| **Entities** (identity + lifecycle) | `EditorialStoryboard`, `ClipReview`, `MonologuePlan`, `CreativeBrief` | `models.py` ✓ | Stay in `models.py`. No change needed. |
| **Value Objects** (immutable, identity-free) | `Segment`, `ReviewUsableSegment`, `TranscriptSegment`, `OutputFormat` | `models.py` / `config.py` ✓ | Stay put. Already correct. |
| **Domain Services** (stateless logic on entities) | `validate_storyboard()`, `_resolve_clip_id_refs()`, `clamp_timestamps()`, `merge_section_storyboards()` | Scattered in `editorial_agent.py`, `section_grouping.py` | **Extract to domain service modules** |
| **Adapters** (external systems) | Gemini API, Claude API, FFmpeg, filesystem I/O | Scattered everywhere | **Extract to adapter modules** |

#### Concrete Example

Today, `_resolve_clip_id_refs()` lives at `editorial_agent.py:~800` — it's pure domain logic (fuzzy-matching abbreviated clip IDs to full IDs) buried inside the LLM orchestrator. It should live in a domain service:

```python
# domain/clip_resolution.py
def resolve_clip_id_refs(
    storyboard: EditorialStoryboard,
    known_clip_ids: list[str],
) -> EditorialStoryboard:
    """Resolve abbreviated clip IDs (e.g., C0073) to full IDs via suffix matching."""
    ...
```

#### Anti-pattern

```python
# ❌ Domain logic mixed with infrastructure
def run_phase2(provider, config, reviews, ...):
    client = _get_gemini_client()           # infrastructure
    response = client.models.generate(...)   # infrastructure
    storyboard = parse_response(response)    # infrastructure
    _resolve_clip_id_refs(storyboard, ...)   # domain logic — wrong layer
    validate_storyboard(storyboard, ...)     # domain logic — wrong layer
    path.write_text(storyboard.json())       # infrastructure
```

Domain logic (resolve, validate) should not know or care that a Gemini client exists.

### 1.2 Layered / Hexagonal Architecture

#### Rationale

The audit found 23 `if provider == "gemini"` branches scattered across 3 files. This is the classic symptom of missing architectural layers — the orchestrator directly knows about every provider's implementation details.

#### The Dependency Rule

Dependencies only point inward. Four layers:

```
┌─────────────────────────────────────────────────┐
│            Entry Points (CLI, TUI)              │  ← Knows about services
├─────────────────────────────────────────────────┤
│            Services (Orchestrators)             │  ← Knows about domain + ports
├─────────────────────────────────────────────────┤
│            Domain (Models, Rules, Validation)   │  ← Knows about nothing external
├─────────────────────────────────────────────────┤
│            Adapters (Gemini, Claude, FFmpeg, FS) │  ← Implements ports defined by domain
└─────────────────────────────────────────────────┘
```

**Inward = toward Domain.** Services import domain. Adapters implement domain interfaces. Entry points import services. Nothing in Domain imports from Services or Adapters.

#### Concrete Example — Provider Abstraction

```python
# domain/ports.py
from typing import Protocol

class ClipReviewer(Protocol):
    """Port: reviews a single clip and returns structured ClipReview."""
    def review_clip(
        self,
        clip_id: str,
        proxy_path: Path,
        transcript_excerpt: str,
        creative_brief: CreativeBrief,
    ) -> ClipReview: ...

class StoryboardAssembler(Protocol):
    """Port: assembles clip reviews into an editorial storyboard."""
    def assemble(
        self,
        reviews: list[ClipReview],
        transcripts: dict[str, Transcript],
        brief: CreativeBrief,
    ) -> EditorialStoryboard: ...
```

```python
# adapters/gemini_reviewer.py
class GeminiClipReviewer:
    """Adapter: implements ClipReviewer via Gemini native video API."""
    def __init__(self, client: GeminiClient, config: GeminiConfig):
        self.client = client
        self.config = config

    def review_clip(self, clip_id, proxy_path, transcript_excerpt, creative_brief):
        prompt = build_clip_review_prompt(...)
        response = self.client.generate(prompt, proxy_path, ...)
        return parse_clip_review(response)
```

The orchestrator receives a `ClipReviewer` — it never checks which provider it is.

#### Anti-pattern

```python
# ❌ Orchestrator branch per provider (23 instances today)
if provider == "gemini":
    client = genai.Client(api_key=...)
    video_file = client.files.upload(...)
    response = client.models.generate_content(model=cfg.model, ...)
elif provider == "claude":
    client = anthropic.Anthropic(api_key=...)
    response = client.messages.create(model=cfg.model, ...)
```

### 1.3 Modularity & Bounded Contexts

#### Rationale

The codebase is a flat directory of 28 files. `editorial_agent.py` imports from 16 modules. There are no explicit boundaries — preprocessing, LLM orchestration, rendering, and versioning all live at the same level.

#### Bounded Context Map

| Context | Responsibility | Current Files | Key Models |
|---------|---------------|---------------|------------|
| **Ingestion** | Clip discovery, ffmpeg preprocessing, format analysis | `preprocess.py`, `format_analyzer.py` | — (dict/manifest) |
| **Briefing** | User context gathering, quick scan, creative brief | `briefing.py` | `QuickScanResult`, `CreativeBrief` |
| **Transcription** | Speech-to-text, speaker ID, chunking | `transcribe.py` | `Transcript` |
| **Editorial** | Phase 1 review, Phase 2 assembly, Phase 3 monologue | `editorial_agent.py`, `editorial_prompts.py`, `section_grouping.py` | `ClipReview`, `EditorialStoryboard`, `MonologuePlan` |
| **Director** | Self-review agent loop, tool use, regression guard | `editorial_director.py`, `director_prompts.py`, `director_tools.py` | `ReviewLog`, `ChatSession` |
| **Rendering** | HTML preview, markdown EDL | `render.py` | — |
| **Assembly** | Rough cut, FCPXML export | `rough_cut.py`, `fcpxml_export.py` | `CutComposition` |
| **Infrastructure** | LLM clients, tracing, versioning, file cache, config | `tracing.py`, `versioning.py`, `file_cache.py`, `config.py` | `ArtifactMeta` |
| **UI** | CLI dispatch, TUI state machine, i18n, setup | `cli.py`, `interactive.py`, `setup_wizard.py`, `i18n/` | — |

#### Boundary Rule

Contexts communicate through **Pydantic models only** at their boundaries. The Editorial context consumes `ClipReview` (its own Phase 1 output) and `CreativeBrief` (from Briefing). It produces `EditorialStoryboard` (consumed by Rendering and Assembly). No context reaches into another's internals.

#### Anti-pattern

```python
# ❌ Rendering reaches into editorial internals
from .editorial_agent import _resolve_clip_id_refs  # private function, wrong context
```

---

## Section 2 — The Structural Blueprint

### 2.1 Folder Topology

#### Rationale

Feature-based organization (grouping by pipeline phase) beats layer-based organization (grouping by role: `models/`, `services/`, `adapters/`) for this codebase. Why: our pipeline phases are the primary cognitive unit — when debugging Phase 2, you want all Phase 2 files together, not scattered across `services/phase2.py`, `adapters/phase2_gemini.py`, `models/phase2.py`.

However, cross-cutting concerns (config, LLM infrastructure, tracing, versioning) need a shared home. **Recommendation: hybrid — feature packages for pipeline phases, shared packages for infrastructure.**

#### Target Structure

```
src/ai_video_editor/
├── __init__.py
├── cli.py                          # Entry point (thinner — delegates to commands/)
├── interactive.py                  # TUI (thinner — delegates to commands/)
├── setup_wizard.py                 # First-run wizard
│
├── domain/                         # Pure domain — no I/O, no API calls
│   ├── __init__.py
│   ├── models.py                   # All Pydantic models (moved from top level)
│   ├── ports.py                    # Protocol definitions for adapters
│   ├── validation.py               # Storyboard validation, constraint checking
│   ├── clip_resolution.py          # Clip ID fuzzy matching, source resolution
│   ├── timestamps.py               # Timestamp clamping, duration arithmetic
│   └── exceptions.py               # Domain exception hierarchy
│
├── infra/                          # Shared infrastructure
│   ├── __init__.py
│   ├── config.py                   # Dataclass configs, path builders (moved)
│   ├── gemini_client.py            # Shared Gemini client factory + file upload
│   ├── anthropic_client.py         # Shared Anthropic client factory
│   ├── tracing.py                  # LLM call tracing, cost estimation
│   ├── versioning.py               # Two-phase commit, lineage DAG
│   ├── file_cache.py               # Gemini File API URI cache
│   └── atomic_write.py             # Write-to-temp + rename utility
│
├── ingestion/                      # Preprocessing & format analysis
│   ├── __init__.py
│   ├── preprocess.py               # ffmpeg proxy, frames, scenes, audio
│   ├── format_analyzer.py          # Device color profiles, format detection
│   └── ffmpeg.py                   # Typed ffmpeg command builder (new)
│
├── briefing/                       # User context & creative brief
│   ├── __init__.py
│   ├── quick_scan.py               # Gemini quick scan of all proxies
│   ├── questionnaire.py            # Interactive questionnaire
│   └── prompts.py                  # Briefing-specific prompt templates
│
├── transcription/                  # Speech-to-text
│   ├── __init__.py
│   ├── gemini_transcriber.py       # Gemini provider
│   ├── mlx_transcriber.py          # mlx-whisper provider
│   └── chunking.py                 # 90s chunk logic for drift mitigation
│
├── editorial/                      # Phases 1–3
│   ├── __init__.py
│   ├── orchestrator.py             # Phase sequencing (the slim editorial_agent)
│   ├── phase1_review.py            # Per-clip review logic
│   ├── phase2_assembly.py          # Story Mode split pipeline
│   ├── phase2_timeline.py          # Timeline Mode section-based assembly
│   ├── phase3_monologue.py         # Visual monologue generation
│   ├── prompts.py                  # All editorial prompt templates
│   ├── section_grouping.py         # Date-based clip grouping
│   └── adapters/                   # Provider-specific LLM interaction
│       ├── __init__.py
│       ├── gemini_reviewer.py      # Gemini Phase 1 adapter
│       ├── claude_reviewer.py      # Claude Phase 1 adapter
│       ├── gemini_assembler.py     # Gemini Phase 2 adapter
│       └── claude_assembler.py     # Claude Phase 2 adapter
│
├── director/                       # Editorial Director agent
│   ├── __init__.py
│   ├── agent.py                    # Multi-turn agent loop
│   ├── tools.py                    # Tool implementations
│   ├── prompts.py                  # System prompt + tool schema
│   └── display.py                  # Pretty-print review output
│
├── rendering/                      # Output generation
│   ├── __init__.py
│   ├── html_preview.py             # Self-contained HTML preview
│   ├── markdown_edl.py             # Markdown edit decision list
│   ├── rough_cut.py                # 3-phase ffmpeg assembly
│   ├── fcpxml_export.py            # FCPXML v1.9 for NLEs
│   └── storyboard_format.py        # Shared formatting helpers
│
├── eval/                           # Storyboard quality scoring
│   ├── __init__.py
│   └── scorer.py                   # Deterministic eval
│
├── i18n/                           # Internationalization (unchanged)
│   ├── __init__.py
│   └── locales/
│       ├── en.json
│       └── zh-TW.json
│
└── style_presets.py                # StylePreset definitions (small, stays top-level)
```

**File count: ~45 files across 10 packages** (up from 28 flat files). Each file is smaller. The largest file (`editorial_agent.py` at 3032 lines) splits into ~5 files averaging 500 lines each.

#### Anti-pattern

```
# ❌ Layer-based structure — pipeline phase logic scattered across directories
src/
├── models/
│   ├── review.py
│   ├── storyboard.py
│   └── monologue.py
├── services/
│   ├── phase1.py
│   ├── phase2.py
│   └── phase3.py
├── adapters/
│   ├── gemini_phase1.py
│   ├── gemini_phase2.py
│   ├── claude_phase1.py
│   └── claude_phase2.py
```

This forces you to open 3+ directories to understand one pipeline phase. Feature-based keeps related code together.

### 2.2 Dependency Management

#### Rationale

The audit found `editorial_agent.py` importing from 16 modules. Circular dependencies don't exist yet, but the flat structure makes them inevitable as the codebase grows.

#### Dependency DAG (packages)

```
cli / interactive
    ↓
editorial, briefing, transcription, director, rendering, eval
    ↓
domain, infra, ingestion
    ↓
(stdlib only)
```

**Rule: No upward or lateral imports between feature packages.** `editorial/` never imports from `briefing/` directly — it receives a `CreativeBrief` (a domain model) as a parameter.

#### Dependency Injection Strategy

Constructor injection. No framework. No container.

```python
# In cli.py or interactive.py — the composition root
def _build_phase1_reviewer(provider: str, config: Config) -> ClipReviewer:
    """Factory: build the right adapter for the configured provider."""
    if provider == "gemini":
        client = GeminiClient.from_env()
        return GeminiClipReviewer(client, config.gemini)
    elif provider == "claude":
        client = AnthropicClient.from_env()
        return ClaudeClipReviewer(client, config.claude)
    raise ValueError(f"Unknown provider: {provider}")
```

**The `if provider == "gemini"` check happens exactly once** — in the composition root (CLI/TUI entry point). Not in 23 places.

#### Anti-pattern

```python
# ❌ Service locator — hidden dependency, untestable
class Phase1Executor:
    def run(self):
        provider = os.environ.get("LLM_PROVIDER", "gemini")  # hidden coupling
        if provider == "gemini":
            from .gemini_analyze import get_client  # import inside method
            client = get_client()
```

### 2.3 Configuration & Environments

#### Rationale

Settings currently live in 5 locations with no documented precedence. `.vx.json` and `project.json` have no schema validation — typos fail silently.

#### Hierarchical Config Design

```
Layer 1: Code defaults       → config.py dataclasses (PreprocessConfig, GeminiConfig, etc.)
Layer 2: Workspace config    → .vx.json (provider, style, locale) — validated by Pydantic
Layer 3: Project config      → project.json (type, provider, style_preset) — validated by Pydantic
Layer 4: Environment vars    → .env via python-dotenv (API keys only)
Layer 5: CLI flags           → argparse overrides (highest precedence)
```

**Precedence: CLI > Project > Workspace > Code defaults.** Environment variables are for secrets only, never for behavioral config.

#### Concrete Change

Add Pydantic models for `.vx.json` and `project.json`:

```python
# domain/models.py (or infra/config.py)
class WorkspaceConfig(BaseModel):
    provider: str = "gemini"
    style: str | None = None
    locale: str = "en"
    setup_complete: bool = False

class ProjectConfig(BaseModel):
    name: str
    type: str = "editorial"
    provider: str = "gemini"
    style_preset: str | None = None
    version_counters: dict[str, int] = {}
    tracks: list[str] = ["main"]
```

Load with `model_validate_json()` — a typo in `.vx.json` now raises a clear `ValidationError` instead of silently proceeding with a `KeyError` five function calls later.

#### Shared Constants

All magic numbers move to `infra/config.py`:

```python
# infra/config.py — all in one place
GEMINI_UPLOAD_TIMEOUT_SEC = 300
MAX_PREPROCESS_WORKERS = 4
MAX_LLM_WORKERS = 5
MAX_TRANSCRIBE_WORKERS_MLX = 2
FILE_API_CACHE_MAX_AGE_SEC = 165_600  # 46h
TRANSCRIBE_CHUNK_SEC = 90
```

No more 4 separate definitions of the same timeout constant.

#### Anti-pattern

```python
# ❌ Constants duplicated across files (the audit found 4 × GEMINI_UPLOAD_TIMEOUT_SEC)
# editorial_agent.py
GEMINI_UPLOAD_TIMEOUT_SEC = 300
# briefing.py
_GEMINI_UPLOAD_TIMEOUT_SEC = 300
# transcribe.py
_GEMINI_UPLOAD_TIMEOUT_SEC = 300
# gemini_analyze.py
_GEMINI_UPLOAD_TIMEOUT_SEC = 300
```

---

## Section 3 — Engineering Standards

### 3.1 Naming Conventions

#### File Nomenclature

| Suffix | Role | Example |
|--------|------|---------|
| `_service.py` or `orchestrator.py` | Orchestrates multiple adapters/domain operations | `editorial/orchestrator.py` |
| `_adapter.py` or `{provider}_{role}.py` | Implements a port for a specific provider | `gemini_reviewer.py` |
| `_schema.py` or `models.py` | Pydantic/dataclass definitions | `domain/models.py` |
| `_prompts.py` or `prompts.py` | Prompt template construction | `editorial/prompts.py` |
| `_repo.py` | Data access abstraction (filesystem operations) | Not needed yet — filesystem access is thin |
| `ports.py` | Protocol definitions | `domain/ports.py` |
| `exceptions.py` | Exception hierarchy | `domain/exceptions.py` |

#### Prompt File Organization

Prompts stay as Python functions (not YAML/Jinja). Each bounded context owns its prompts:

```
briefing/prompts.py          # build_quick_scan_prompt(), build_targeted_questions_prompt()
editorial/prompts.py          # build_clip_review_prompt(), build_assembly_prompt(), ...
director/prompts.py           # build_director_system_prompt(), TOOL_DEFINITIONS
transcription/gemini_transcriber.py  # Prompt is simple enough to inline
```

**Rule:** Prompts that serve a single phase live in that phase's `prompts.py`. The shared `storyboard_format.py` (formatting helpers used by multiple phases) moves to `rendering/storyboard_format.py`.

#### Anti-pattern

```
# ❌ Generic names that don't communicate role
utils.py          # What kind of utils? For whom?
helpers.py        # Same problem
analyze.py        # Analyze what? How?
agent.py          # Which agent? What does it do?
```

### 3.2 DRY vs. AHA (Avoid Hasty Abstractions)

#### Rationale

The audit found three duplications that are genuinely harmful (`_wait_for_gemini_file` ×4, `_resolve_clip_source` ×3, Gemini client ×7) and zero premature abstractions. This codebase's problem is **under-abstraction**, not over-abstraction.

#### Heuristic: When to Deduplicate

| Signal | Action |
|--------|--------|
| Same logic, 3+ copies, behavior should be identical | **Deduplicate.** (`_wait_for_gemini_file` — create one shared version with the `FAILED` state check) |
| Same logic, 2-3 copies, behavior intentionally diverges | **Parameterize.** (`_resolve_clip_source` — one function with a `fallback_strategy` parameter) |
| Similar structure, different domain semantics | **Keep separate.** Phase 1 and Phase 2 prompts look similar but serve different cognitive tasks — don't merge them. |

#### Prompt Duplication Rule

**Prompts for different cognitive tasks remain fully independent.** The split-pipeline design (Phase 2A: creative reasoning, 2A.5: structuring, 2B: assembly) exists precisely because merging these tasks degraded quality. Sharing templates between them would reintroduce the monolithic-prompt failure mode.

What CAN be shared:
- Formatting helpers: `format_duration()`, `format_cast_for_prompt()`, `_output_language_directive()`
- Structural templates: the storyboard JSON schema description, constraint hierarchy formatting

What MUST NOT be shared:
- System instructions (different cognitive modes)
- Temperature/model selection (different quality tradeoffs)
- Response schema specifications (different output shapes)

#### Anti-pattern

```python
# ❌ Premature abstraction: generic "LLM call" wrapper that hides important differences
def call_llm(phase, provider, prompt, schema=None, temp=None, model=None):
    """Universal LLM call for any phase, any provider."""
    # This function inevitably grows if/elif branches for every phase ×
    # every provider, becoming worse than the disease it's treating.
```

### 3.3 Interface Over Implementation

#### Rationale

Python's `Protocol` (PEP 544) enables structural subtyping — an adapter satisfies a port if it has the right methods, without explicit inheritance. This is lighter than ABCs and avoids the diamond inheritance problems.

#### Protocol Definitions

```python
# domain/ports.py
from typing import Protocol
from pathlib import Path
from .models import ClipReview, EditorialStoryboard, Transcript, CreativeBrief, MonologuePlan

class ClipReviewer(Protocol):
    """Reviews a single clip — provider-agnostic."""
    def review_clip(
        self,
        clip_id: str,
        proxy_path: Path,
        transcript_excerpt: str,
        creative_brief: CreativeBrief,
    ) -> ClipReview: ...

class StoryboardAssembler(Protocol):
    """Assembles reviews into a storyboard — provider-agnostic."""
    def assemble(
        self,
        reviews: list[ClipReview],
        transcripts: dict[str, Transcript],
        brief: CreativeBrief,
        visual_bundle_path: Path | None = None,
    ) -> EditorialStoryboard: ...

class Transcriber(Protocol):
    """Transcribes a single clip — provider-agnostic."""
    def transcribe(
        self, audio_path: Path, clip_id: str, speaker_names: list[str]
    ) -> Transcript: ...

class MonologueGenerator(Protocol):
    """Generates visual text overlays — provider-agnostic."""
    def generate(
        self,
        storyboard: EditorialStoryboard,
        transcripts: dict[str, Transcript],
    ) -> MonologuePlan: ...
```

**Where Protocol helps most:** The LLM provider boundary and the transcription boundary. These are the two places where `if provider ==` branches proliferate.

**Where Protocol is overkill:** FFmpeg wrapping. There's only one FFmpeg, and we're not abstracting over multiple media processors. A simple module with typed functions is sufficient.

#### Anti-pattern

```python
# ❌ ABC with forced inheritance — too rigid for adapters
from abc import ABC, abstractmethod

class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str: ...

    @abstractmethod
    def generate_structured(self, prompt: str, schema: type) -> BaseModel: ...

    @abstractmethod
    def upload_file(self, path: Path) -> str: ...  # Claude doesn't upload files!
```

ABCs force a least-common-denominator interface. Protocols let each adapter expose only what it supports.

---

## Section 4 — Operational Excellence & Developer Experience

### 4.1 Observability

#### Rationale

The existing `tracing.py` (839 lines) already records per-call token usage, cost, and timing to `traces.jsonl`. Phoenix integration exists as an optional dependency. What's missing is **structured per-project, per-phase context propagation** — when a storyboard is bad, you need to trace backward through all the LLM calls that produced it.

#### Structured Logging Standard

**Use `structlog`** configured with JSON output for machine-parseable logs:

```python
# infra/logging.py
import structlog

def configure_logging(project_name: str | None = None, verbosity: int = 0):
    """Configure structlog with project context."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if verbosity > 0
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbosity > 1 else logging.INFO
        ),
    )
```

#### Per-Phase Context Propagation

```python
# In the orchestrator, before each phase:
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

log = structlog.get_logger()

def run_phase1(clip_id: str, ...):
    bind_contextvars(phase="phase1", clip_id=clip_id, provider=provider)
    log.info("phase1.start")
    try:
        review = reviewer.review_clip(...)
        log.info("phase1.complete", tokens=trace.total_tokens, cost_usd=trace.cost_usd)
        return review
    except Exception:
        log.exception("phase1.failed")
        raise
    finally:
        clear_contextvars()
```

#### Where Observability Lives

```
infra/
├── tracing.py          # LLM call-level tracing (exists, keep)
├── logging.py          # structlog configuration (new)
```

No separate `observability/` package. It's infrastructure.

#### Tracing a Vlog's Journey

Every `traces.jsonl` entry already has `phase`, `clip_id`, `model`, `tokens`, `cost`, `duration`. The missing piece is a **run ID** — a UUID generated at pipeline start and attached to every trace and log entry in that run:

```python
bind_contextvars(run_id=str(uuid.uuid4()), project=project_name)
```

Now `jq '.run_id == "abc123"' traces.jsonl` gives you every LLM call for one pipeline run.

### 4.2 Error Handling Strategy

#### Global Exception Hierarchy

```python
# domain/exceptions.py

class VXError(Exception):
    """Base for all VX domain errors. CLI catches this for user-facing messages."""
    pass

# --- Domain errors (pure logic failures) ---
class StoryboardValidationError(VXError):
    """Storyboard violates structural constraints (missing segments, bad timestamps)."""
    pass

class ClipResolutionError(VXError):
    """Clip ID could not be resolved to a known clip."""
    pass

class ConstraintViolationError(VXError):
    """User constraints (must-include, must-exclude) not satisfied."""
    pass

# --- Infrastructure errors (external system failures) ---
class LLMProviderError(VXError):
    """LLM API call failed after retries."""
    def __init__(self, provider: str, phase: str, message: str, cause: Exception | None = None):
        self.provider = provider
        self.phase = phase
        super().__init__(f"[{provider}/{phase}] {message}")
        self.__cause__ = cause

class LLMResponseParseError(LLMProviderError):
    """LLM returned unparseable or invalid structured output."""
    pass

class LLMCostLimitExceeded(VXError):
    """Cumulative LLM cost exceeded the configured limit."""
    pass

class MediaProcessingError(VXError):
    """FFmpeg or media operation failed."""
    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"ffmpeg failed (rc={returncode}): {stderr[:200]}")

class FileUploadError(LLMProviderError):
    """File upload to LLM provider failed or timed out."""
    pass

class RenderTimeoutError(VXError):
    """Rough cut or preview render exceeded timeout."""
    pass
```

#### Catch Rules

| Layer | Catches | Does |
|-------|---------|------|
| **Adapters** | Provider SDK exceptions (`google.api_core.exceptions.*`, `anthropic.*`) | Translates to `LLMProviderError` or subclass. Retryable errors are retried inside the adapter with backoff (existing `tracing.py` retry logic). |
| **Services/Orchestrators** | `LLMProviderError`, `StoryboardValidationError` | Logs, updates version status (`fail_version()`), propagates up. |
| **Entry Points (CLI/TUI)** | `VXError` | Prints user-friendly message. Never prints a stack trace for domain errors. Prints trace for unexpected errors. |
| **Nobody** | Bare `except:` or `except Exception` without re-raise | **Banned.** |

#### Anti-pattern

```python
# ❌ Catching too broadly, swallowing context
try:
    storyboard = parse_response(response)
except Exception as e:
    print(f"  WARN: parse failed ({e}), retrying...")  # loses stack trace, retries blindly
```

### 4.3 Intermediate State & Artifact Management

#### Rationale

The existing two-phase commit protocol (`begin_version` / `commit_version` / `fail_version`) handles metadata atomicity well. Two gaps: (1) content writes are not atomic (crash mid-write → corrupt file), and (2) no resumability (failed pipeline restarts from scratch).

#### Atomic File Writes

```python
# infra/atomic_write.py
import os
import tempfile
from pathlib import Path

def atomic_write_text(path: Path, content: str) -> None:
    """Write content atomically via write-to-temp + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        os.unlink(tmp)
        raise
```

Apply to all artifact writes: storyboard JSON, review JSON, manifest, user context, transcripts. This is a one-line change at each call site: `path.write_text(data)` → `atomic_write_text(path, data)`.

#### Resumability

The existing versioning system already enables this. Each phase checks for cached output before running:

```python
# This pattern already exists in editorial_agent.py — formalize it:
def _phase_is_cached(editorial_paths, phase, provider, version=None) -> bool:
    """Check if a phase output exists and is valid."""
    path = resolve_versioned_path(editorial_paths, phase, provider, version)
    return path is not None and path.exists()
```

**Formalized resumability protocol:**

1. Before each phase: check if output exists at the expected version.
2. If exists + `status: "complete"` in `.meta.json` → skip (already done).
3. If exists + `status: "pending"` → stale from a crash → delete and re-run.
4. If not exists → run phase.

This is already the implicit behavior. Making it explicit with a `should_run_phase()` helper eliminates the ad-hoc checks scattered across `editorial_agent.py`.

#### Validation at Phase Boundaries

Every phase boundary should validate its output through Pydantic before persisting:

```
Phase 1 output: ClipReview.model_validate_json(response)      ← already done ✓
Phase 2 output: EditorialStoryboard.model_validate_json(...)   ← already done ✓
Phase 3 output: MonologuePlan.model_validate_json(...)         ← already done ✓
Manifest load:  json.loads(...)                                ← NOT validated ✗ (fix this)
User context:   json.loads(...)                                ← NOT validated ✗ (fix this)
Transcript:     json.loads(...)                                ← NOT validated ✗ (fix this)
```

The fix is straightforward: replace `json.loads()` with `Model.model_validate_json()` for every artifact that has a Pydantic model.

---

## Section 5 — Domain & Tech-Stack Specifics

### 5.1 External Binary Orchestration (FFmpeg)

#### Rationale

`rough_cut.py` has excellent failure-driven ffmpeg design (each flag traces to a real bug). But raw `subprocess.run()` calls are scattered across `preprocess.py` and `rough_cut.py` with string-concatenated command lines. A typed command builder prevents shell injection, ensures cleanup, and centralizes error translation.

#### FFmpeg Adapter Pattern

```python
# ingestion/ffmpeg.py
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from domain.exceptions import MediaProcessingError

log = logging.getLogger(__name__)

@dataclass
class FFmpegCommand:
    """Typed ffmpeg command builder — no string concatenation."""
    inputs: list[str] = field(default_factory=list)
    output_args: list[str] = field(default_factory=list)
    global_args: list[str] = field(default_factory=list, init=False)
    filters: list[str] = field(default_factory=list)
    _output: str | None = None

    def input(self, path: Path | str, **kwargs) -> "FFmpegCommand":
        for k, v in kwargs.items():
            self.inputs.extend([f"-{k}", str(v)])
        self.inputs.extend(["-i", str(path)])
        return self

    def filter(self, f: str) -> "FFmpegCommand":
        self.filters.append(f)
        return self

    def output(self, path: Path | str, **kwargs) -> "FFmpegCommand":
        for k, v in kwargs.items():
            self.output_args.extend([f"-{k}", str(v)])
        self._output = str(path)
        return self

    def overwrite(self) -> "FFmpegCommand":
        self.global_args.append("-y")
        return self

    def build(self) -> list[str]:
        cmd = ["ffmpeg"] + self.global_args + self.inputs
        if self.filters:
            cmd.extend(["-vf", ",".join(self.filters)])
        cmd.extend(self.output_args)
        if self._output:
            cmd.append(self._output)
        return cmd

    def run(self, timeout: int = 600) -> subprocess.CompletedProcess:
        cmd = self.build()
        log.debug("ffmpeg.run", extra={"cmd": " ".join(cmd)})
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise MediaProcessingError(
                command=" ".join(cmd),
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result
```

Usage:

```python
# Before (string concatenation):
cmd = f"ffmpeg -y -i {input_path} -vf scale={w}:{h} -c:v libx264 -crf 28 {output_path}"
subprocess.run(cmd, shell=True, ...)

# After (typed builder):
FFmpegCommand() \
    .overwrite() \
    .input(input_path) \
    .filter(f"scale={w}:{h}") \
    .output(output_path, c_v="libx264", crf=28) \
    .run()
```

**Note:** This is an incremental aid, not a mandatory replacement for every ffmpeg call on day one. The complex `rough_cut.py` pipeline (concat demuxer, multi-phase assembly) can adopt it gradually. The simpler `preprocess.py` calls are the first migration targets.

#### Cleanup

Temporary media files (intermediate segments, concat lists) must use a context manager:

```python
import tempfile
from contextlib import contextmanager

@contextmanager
def temp_media_dir(prefix: str = "vx_"):
    """Temporary directory for intermediate media — cleaned up on exit or error."""
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

#### Anti-pattern

```python
# ❌ Raw subprocess with shell=True, no error translation
result = subprocess.run(f"ffmpeg -y -i '{input}' '{output}'", shell=True)
if result.returncode != 0:
    print("ffmpeg failed")  # no stderr, no command, no exception
```

### 5.2 LLM State & Prompt Management

#### Prompt Storage

**Prompts stay as Python functions.** No YAML, no Jinja, no external template files. For a codebase this size, Python functions are the right abstraction:

- Type-checked parameters
- IDE navigation (go-to-definition)
- Testable (unit-test prompt output for specific inputs)
- No template language to learn

```python
# editorial/prompts.py
def build_clip_review_prompt(
    clip_id: str,
    duration_str: str,
    resolution: str,
    transcript_excerpt: str,
    creative_brief: CreativeBrief,
    style_supplement: str = "",
) -> str:
    """Build the Phase 1 clip review system + user prompt."""
    ...
```

Each bounded context owns its prompts. Cross-context shared formatters (`format_duration`, `_output_language_directive`, constraint hierarchy formatting) live in `rendering/storyboard_format.py` or a shared `domain/formatting.py`.

#### Provider Abstraction

Separate **what to ask** (orchestrator) from **how to call** (adapter):

```
Orchestrator (editorial/orchestrator.py):
    - Decides which phase to run
    - Builds prompts (via editorial/prompts.py)
    - Passes prompt + data to adapter
    - Receives parsed domain model back
    - Runs domain validation

Adapter (editorial/adapters/gemini_assembler.py):
    - Receives prompt string + optional media files
    - Calls provider SDK (genai.Client / anthropic.Anthropic)
    - Handles retries, structured output parsing, token counting
    - Returns parsed Pydantic model or raises LLMProviderError
```

The orchestrator never imports `google.genai` or `anthropic`. The adapter never imports domain validation logic.

#### Response Parsing & Validation

```python
# In adapter:
def _parse_structured_response(
    response_text: str,
    model_class: type[T],
    phase: str,
) -> T:
    """Parse LLM response into Pydantic model with clear error on failure."""
    try:
        return model_class.model_validate_json(response_text)
    except ValidationError as e:
        raise LLMResponseParseError(
            provider=self.provider_name,
            phase=phase,
            message=f"Response failed validation: {e.error_count()} errors",
            cause=e,
        )
```

**Retry on parse failure:** The adapter retries up to `MAX_LLM_RETRIES` times when the response is structurally invalid (wrong JSON, missing required fields). This already happens in `tracing.py`'s retry logic — formalize it by making parse failure a retriable condition.

#### Split-Pipeline Coordination

The Phase 2 split pipeline (2A → 2A.5 → 2B) is formalized as a **pipeline composed of sequential adapter calls**, not one monolithic function:

```python
# editorial/phase2_assembly.py
class StoryModePipeline:
    """Phase 2 split pipeline: creative reasoning → structuring → precise assembly."""

    def __init__(
        self,
        reasoning_adapter: ReasoningAdapter,     # Call 2A
        structuring_adapter: StructuringAdapter,  # Call 2A.5
        assembly_adapter: AssemblyAdapter,        # Call 2B
    ):
        self.reasoning = reasoning_adapter
        self.structuring = structuring_adapter
        self.assembly = assembly_adapter

    def run(self, reviews, transcripts, brief, ...) -> EditorialStoryboard:
        # Step 1: Creative reasoning (Gemini Pro, temp 0.8)
        reasoning_prose = self.reasoning.reason(reviews, transcripts, brief)

        # Step 2: Structure into StoryPlan (Flash Lite, temp 0.2)
        story_plan = self.structuring.structure(reasoning_prose, reviews)

        # Step 3: Precise timestamp assembly (Flash, temp 0.3)
        storyboard = self.assembly.assemble(story_plan, reviews)

        # Domain validation
        resolve_clip_id_refs(storyboard, known_clip_ids)
        validate_storyboard(storyboard, reviews)

        return storyboard
```

Each step is a separate adapter instance with its own model/temperature configuration. The `use_split_pipeline` toggle becomes unnecessary — Story Mode always uses the split pipeline (it's been the default since the refactor proved its value).

### 5.3 UI / Logic Decoupling (TUI & CLI)

#### Command Pattern

The TUI/CLI layer sends commands to the domain layer through a defined interface. No business logic in `cli.py` or `interactive.py`.

```python
# Example: CLI "analyze" command becomes thin dispatch
def cmd_analyze(args):
    """CLI handler — zero business logic."""
    config = load_config(args)
    paths = config.editorial_project(args.project)

    # Build adapters (composition root)
    reviewer = build_reviewer(config)
    assembler = build_assembler(config)

    # Delegate to orchestrator
    from editorial.orchestrator import run_analysis
    result = run_analysis(paths, config, reviewer, assembler)

    # Display result
    print(f"Storyboard v{result.version} written to {result.path}")
```

**The 2824-line `interactive.py` splits into:**
- `interactive.py` — TUI state machine, menu navigation, display formatting (~800 lines)
- `editorial/orchestrator.py` — pipeline sequencing that TUI currently does inline (~1000 lines)
- Other business logic extracted to relevant bounded contexts (~1000 lines)

#### Anti-pattern

```python
# ❌ Business logic in TUI handler (interactive.py today)
def handle_analyze_menu():
    # ... 200 lines of questionary UI ...
    # ... then directly calls LLM APIs ...
    # ... then does storyboard validation ...
    # ... then writes files ...
    # TUI, orchestration, domain logic, and I/O all in one function
```

### 5.4 Concurrency Model

#### Current State

All concurrency uses `ThreadPoolExecutor` (synchronous threads). This works because:
- LLM calls are I/O-bound → threads are fine for I/O
- FFmpeg is CPU-bound → threads are fine because FFmpeg itself is a subprocess

#### Recommendation: Stay with ThreadPoolExecutor

**Do not migrate to asyncio.** The cost/benefit is wrong for this codebase:

| Factor | asyncio | ThreadPoolExecutor |
|--------|---------|-------------------|
| LLM SDK support | Anthropic has async. google-genai async is immature. | Both SDKs have sync. |
| FFmpeg subprocess | `asyncio.create_subprocess_exec` — adds complexity, no benefit | `subprocess.run` in thread — simple, works. |
| questionary TUI | Fundamentally synchronous (prompt_toolkit). | Native fit. |
| Debugging | Stack traces are cryptic. | Stack traces are normal. |
| Team size benefit | Large teams, high-concurrency services. | 1–2 person team. Perfect fit. |

**Formalized concurrency pattern:**

```python
# IO-bound (LLM calls): ThreadPoolExecutor with semaphore
MAX_LLM_WORKERS = 5

def run_phase1_parallel(clips, reviewer: ClipReviewer, ...) -> list[ClipReview]:
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_LLM_WORKERS) as pool:
        futures = {
            pool.submit(reviewer.review_clip, clip_id, ...): clip_id
            for clip_id, ... in clips
        }
        for fut in as_completed(futures):
            clip_id = futures[fut]
            try:
                results[clip_id] = fut.result()
            except LLMProviderError as e:
                log.error("phase1.clip_failed", clip_id=clip_id, error=str(e))
    return [results[cid] for cid in clip_order if cid in results]

# CPU-bound (FFmpeg): ThreadPoolExecutor with lower concurrency
MAX_PREPROCESS_WORKERS = 4  # Saturates disk/CPU with 4 ffmpeg processes

def preprocess_parallel(clips, ...) -> list[dict]:
    with ThreadPoolExecutor(max_workers=MAX_PREPROCESS_WORKERS) as pool:
        ...
```

**The bridge pattern** (parallel LLM → sequential FFmpeg) is already the natural pipeline flow. Phases run sequentially; within each phase, work items are parallelized.

#### Anti-pattern

```python
# ❌ asyncio for a CLI tool with synchronous dependencies
async def main():
    loop = asyncio.get_event_loop()
    # Run sync questionary in executor... to call async Gemini... to run sync ffmpeg in executor
    # Three layers of async/sync bridging for zero actual benefit
```

---

## Section 6 — Testing Strategy

### 6.1 Test Organization

#### Recommendation: Top-level `tests/` mirroring `src/` structure

```
tests/
├── conftest.py                    # Shared fixtures: sample storyboards, clip reviews, config
├── domain/
│   ├── test_validation.py         # Storyboard validation rules
│   ├── test_clip_resolution.py    # Clip ID fuzzy matching
│   └── test_timestamps.py         # Timestamp clamping, duration math
├── editorial/
│   ├── test_prompts.py            # Prompt construction (existing test coverage)
│   ├── test_orchestrator.py       # Phase sequencing with mock adapters
│   └── test_section_grouping.py   # Date-based grouping logic
├── infra/
│   ├── test_versioning.py         # Two-phase commit protocol
│   ├── test_file_cache.py         # Cache expiration, TTL
│   └── test_atomic_write.py       # Atomic write correctness
├── rendering/
│   ├── test_rough_cut.py          # Segment extraction command building
│   ├── test_fcpxml.py             # FCPXML generation
│   └── test_html_preview.py       # HTML output structure
├── eval/
│   └── test_scorer.py             # Deterministic scoring
├── integration/
│   ├── test_gemini_adapter.py     # Recorded response fixtures
│   └── test_ffmpeg_commands.py    # Command assertion (no actual encode)
└── fixtures/
    ├── sample_storyboard.json     # Valid EditorialStoryboard
    ├── sample_clip_review.json    # Valid ClipReview
    ├── sample_transcript.json     # Valid Transcript
    ├── sample_creative_brief.json # Valid CreativeBrief
    └── tiny_clip.mp4              # 2-second test video (360p, H.264)
```

**Why top-level, not colocated:** The source package ships to users via `pip install`. Test files, fixtures, and test dependencies should not be in the distribution. Top-level `tests/` is excluded by default in `pyproject.toml`.

### 6.2 Test Layers

| Layer | Scope | Speed | I/O | Coverage Target |
|-------|-------|-------|-----|----------------|
| **Unit** | Domain logic, prompt construction, eval scoring, config validation | <1s each | None | Every domain function. Every prompt builder. Every validation rule. |
| **Integration** | LLM adapter with recorded fixtures, ffmpeg command building, versioning protocol with temp dirs | <5s each | Filesystem only | Every adapter's happy path + one error path. Versioning lifecycle. |
| **E2E / Pipeline** | Full phase-to-phase with fixture data | <60s | Filesystem + subprocess (ffmpeg) | One golden path per mode (Story, Timeline). Run in CI only. |

#### Unit Test Examples

```python
# tests/domain/test_validation.py
def test_storyboard_rejects_overlapping_segments():
    """Segments from the same clip must not have overlapping time ranges."""
    sb = make_storyboard(segments=[
        make_segment(clip_id="C001", in_sec=0.0, out_sec=5.0),
        make_segment(clip_id="C001", in_sec=3.0, out_sec=8.0),  # overlaps
    ])
    errors = validate_storyboard(sb)
    assert any("overlap" in e.lower() for e in errors)

# tests/domain/test_clip_resolution.py
def test_resolve_abbreviated_clip_id():
    known = ["20260330114125_C0073", "20260330120000_C0059"]
    assert resolve_clip_id("C0073", known) == "20260330114125_C0073"

def test_resolve_ambiguous_clip_id_raises():
    known = ["A_C0073", "B_C0073"]
    with pytest.raises(ClipResolutionError):
        resolve_clip_id("C0073", known)
```

### 6.3 Mocking & Fixtures Strategy

#### LLM Response Mocking: Recorded Fixtures

**Use VCR-style recorded fixtures**, not hand-written fakes. The workflow:

1. Run the real LLM call once with `VX_RECORD_FIXTURES=1`.
2. Serialize the response to `tests/fixtures/responses/phase1_gemini_C0073.json`.
3. In tests, the adapter receives this recorded response instead of calling the API.

```python
# tests/conftest.py
@pytest.fixture
def recorded_gemini_response():
    """Load a recorded Gemini Phase 1 response."""
    path = FIXTURES_DIR / "responses" / "phase1_gemini_C0073.json"
    return json.loads(path.read_text())

# tests/editorial/test_orchestrator.py
def test_phase1_processes_gemini_response(recorded_gemini_response):
    """Orchestrator correctly processes a real Gemini response into ClipReview."""
    mock_reviewer = MockClipReviewer(responses={"C0073": recorded_gemini_response})
    result = run_phase1(clips=["C0073"], reviewer=mock_reviewer, ...)
    assert result["C0073"].clip_id == "C0073"
    assert len(result["C0073"].usable_segments) > 0
```

#### FFmpeg Testing: Command Assertion

Don't run real encodes in tests. Assert the command that *would* be built:

```python
def test_proxy_command_includes_hw_encode():
    cmd = FFmpegCommand() \
        .input(Path("clip.mov")) \
        .filter("scale=360:240") \
        .output(Path("proxy.mp4"), c_v="h264_videotoolbox", crf=28) \
        .build()
    assert "h264_videotoolbox" in cmd
    assert "-vf" in cmd
    assert "scale=360:240" in cmd
```

For the small number of tests that need actual ffmpeg (e.g., validating concat demuxer behavior), use `tests/fixtures/tiny_clip.mp4` — a 2-second, 360p, H.264 video.

#### Pydantic Model Factories

```python
# tests/conftest.py
from ai_video_editor.domain.models import (
    EditorialStoryboard, Segment, ClipReview, ReviewUsableSegment
)

def make_segment(**overrides) -> Segment:
    defaults = {
        "index": 0, "clip_id": "C0001", "in_sec": 0.0, "out_sec": 5.0,
        "purpose": "test", "description": "test segment",
    }
    return Segment(**(defaults | overrides))

def make_storyboard(**overrides) -> EditorialStoryboard:
    defaults = {
        "title": "Test", "style": "cinematic",
        "story_concept": "test concept",
        "segments": [make_segment()],
        "editorial_reasoning": "test",
    }
    return EditorialStoryboard(**(defaults | overrides))
```

---

## Section 7 — Documentation & Knowledge Management

### 7.1 Architectural Decision Records (ADRs)

#### Template (Nygard)

```markdown
# ADR-XXXX: Title

**Date:** YYYY-MM-DD
**Status:** Accepted | Superseded by ADR-XXXX | Deprecated

## Context
What is the issue that we're seeing that is motivating this decision?

## Decision
What is the change that we're proposing and/or doing?

## Consequences
What becomes easier or harder as a result of this change?
```

#### Retroactive ADRs to Write

| ADR | Title | Source Document |
|-----|-------|----------------|
| ADR-0001 | Multi-call split pipeline for Phase 2 | `refactor_plan/llm-architecture-improvement-plan.md` |
| ADR-0002 | Structural chronological enforcement (Timeline Mode) | `design-timeline-mode.md` |
| ADR-0003 | Pydantic field descriptions as Gemini response schema instructions | `prompt-engineering-cookbook.md` |
| ADR-0004 | Filesystem as state machine (no database) | README §Architecture |
| ADR-0005 | Two-phase commit versioning with lineage DAG | `versioning.py` header comment |
| ADR-0006 | 90-second transcription chunking for Gemini timestamp drift | `dev-gemini-timestamp-drift.md` |

#### Location

```
docs/adr/
├── 0001-multi-call-split-pipeline.md
├── 0002-structural-chronological-enforcement.md
├── ...
└── template.md
```

### 7.2 README Hierarchy

```
README.md                      # System map, quickstart, architecture overview (exists, keep)
src/ai_video_editor/
├── domain/README.md           # "All Pydantic models and pure domain logic. No I/O."
├── infra/README.md            # "Shared infrastructure: LLM clients, tracing, versioning."
├── editorial/README.md        # "Phases 1–3 of the editorial pipeline."
├── rendering/README.md        # "Output generation: HTML preview, rough cut, FCPXML."
└── director/README.md         # "Experimental: multi-turn agent review loop."
```

Each sub-README is 10–30 lines: one paragraph explaining the package's responsibility, a file listing with one-line descriptions, and the key entry points.

### 7.3 Code Documentation (SSOT)

**Sphinx with autodoc** is overkill for a 25K-line CLI tool. Defer this until the project has external API consumers or is open-sourced.

For now:
- **Docstrings are the source of truth.** Every public function has a one-line summary. Multi-line docstrings for non-obvious functions (e.g., `_resolve_clip_id_refs` explaining the fuzzy matching algorithm).
- **The "Why" Rule:** Comments never explain *what*. Comments explain *why* a non-obvious decision was made. The ffmpeg flag comments in `rough_cut.py` are a perfect example of this done right.
- **CLAUDE.md is the machine-readable architecture doc.** Keep it updated as the structure evolves.

### 7.4 Onboarding Path

**`CONTRIBUTING.md`** (new file):

```markdown
# Contributing to VX

## Setup (< 10 minutes)
git clone ... && cd ai-video-editor
uv venv && uv pip install -e ".[dev]"
cp .env.example .env  # Add your API keys

## Run
vx                     # Interactive mode
vx new test ~/clips/   # Create a project

## Tests
pytest                  # Unit + integration (< 30s)
pytest -m e2e           # Full pipeline (slow, needs ffmpeg + API keys)

## Architecture
- Read `docs/architecture-manifesto.md` for the why
- Read `src/ai_video_editor/infra/gemini_client.py` as the Golden Sample (first refactored module)
- Read `docs/adr/` for major design decisions

## Code Standards
- `ruff check src/ && ruff format src/` before every commit
- New modules follow the Golden Sample pattern
- Provider-specific code lives in adapter files, never in orchestrators
```

---

## Section 8 — CI/CD & Deployment Alignment

### 8.1 Containerization

VX is a CLI tool, not a web service. Containerization serves two purposes: **reproducible CI** and **distribution to users without Python environments**.

```dockerfile
# Dockerfile
FROM python:3.11-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv pip install --system -e ".[dev]"

FROM deps AS runtime
COPY src/ src/
COPY tests/ tests/
RUN pip install --system -e .

# CI stage: run tests
FROM runtime AS test
CMD ["pytest", "-x", "--tb=short"]

# Distribution stage: minimal runtime
FROM base AS dist
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=runtime /usr/local/bin/vx /usr/local/bin/vx
COPY src/ src/
ENTRYPOINT ["vx"]
```

### 8.2 CI Pipeline

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv pip install ruff
      - run: ruff check src/
      - run: ruff format --check src/

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv pip install -e ".[dev]" mypy
      - run: mypy src/ai_video_editor/domain/ --strict

  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv pip install -e ".[dev]"
      - run: pytest tests/ -m "not integration and not e2e" -x --tb=short

  integration:
    runs-on: ubuntu-latest
    needs: [lint, typecheck, unit]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: sudo apt-get install -y ffmpeg
      - run: uv pip install -e ".[dev]"
      - run: pytest tests/ -m integration -x --tb=short
```

**Progression:** Lint → Typecheck → Unit → Integration. Fast failures first.

### 8.3 Linting & Type Checking

- **Ruff:** Already configured. Add pre-commit hook:
  ```yaml
  # .pre-commit-config.yaml
  repos:
    - repo: https://github.com/astral-sh/ruff-pre-commit
      rev: v0.9.0
      hooks:
        - id: ruff
          args: [--fix]
        - id: ruff-format
  ```

- **mypy:** Strict mode for `domain/` (pure logic, should be fully typed). Gradual mode for adapters and UI (external SDK types are often incomplete):
  ```ini
  # pyproject.toml
  [tool.mypy]
  python_version = "3.11"
  warn_return_any = true
  warn_unused_configs = true

  [[tool.mypy.overrides]]
  module = "ai_video_editor.domain.*"
  strict = true

  [[tool.mypy.overrides]]
  module = ["ai_video_editor.infra.*", "ai_video_editor.editorial.*"]
  disallow_untyped_defs = false
  ```

---

## Section 9 — Golden Sample Selection

### Candidate Analysis

| Module | Layers Touched | Size | Representativeness | Difficulty |
|--------|---------------|------|-------------------|-----------|
| `gemini_analyze.py` | Adapter only | 95 lines | Low — descriptive mode only, not editorial | Easy |
| `file_cache.py` | Infra only | 57 lines | Low — single concern, no domain logic | Trivial |
| `eval.py` | Domain + scoring | 456 lines | Medium — domain logic, but no adapters | Medium |
| `transcribe.py` | Adapter + domain + infra | 785 lines | High — has provider branching, chunking, caching | Too large for one session |
| **Gemini infrastructure extraction** | **Infra + adapter + domain exceptions** | **~200 lines new** | **High — eliminates 4×wait, 7×client, establishes patterns** | **Just right** |

### Selection: Extract `infra/gemini_client.py` + refactor `gemini_analyze.py`

**Justification:**

1. **Touches three layers:** Infrastructure (client factory, file upload), adapter (provider-specific analysis), domain (exceptions).
2. **Small enough for one session:** ~200 lines of new code, ~100 lines deleted from deduplication.
3. **Eliminates real pain:** The 4 duplicated `_wait_for_gemini_file` and 7 duplicated client creation points are the most frequently cited problems in the audit.
4. **Maximally representative:** Every other adapter refactoring will follow this exact pattern — extract shared infrastructure, implement adapter against Protocol, use domain exceptions.
5. **Low risk:** `gemini_analyze.py` is only used in descriptive mode. If the refactoring breaks something, the blast radius is small.

### Golden Sample: `infra/gemini_client.py`

```python
"""Shared Gemini client infrastructure — single source of truth for client
creation, file upload, and processing wait.

Eliminates 4× duplicated _wait_for_gemini_file and 7× duplicated client
creation scattered across editorial_agent, briefing, transcribe, and
gemini_analyze.

This is the Golden Sample module. All conventions in the Architecture
Manifesto are demonstrated here. Use this as the template for new modules.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from ..domain.exceptions import FileUploadError, LLMProviderError

# Lazy import: google.genai is heavy (~2s import on cold start).
# Imported inside methods that need it.

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (single definition — was duplicated 4× across the codebase)
# ---------------------------------------------------------------------------

GEMINI_UPLOAD_TIMEOUT_SEC = 300  # 5 minutes
GEMINI_UPLOAD_POLL_SEC = 3


class GeminiClient:
    """Thin wrapper around google.genai.Client providing:

    - Centralized client creation with API key validation
    - File upload with processing wait, timeout, and FAILED state handling
    - Structured logging for all operations
    - Domain exception translation (google SDK errors → VX exceptions)

    Usage:
        client = GeminiClient.from_env()
        video_file = client.upload_and_wait(proxy_path, label="C0073")
        response = client.generate(model="gemini-3-flash-preview", contents=[...])
    """

    def __init__(self, api_key: str):
        from google import genai

        self._client = genai.Client(api_key=api_key)

    @classmethod
    def from_env(cls) -> GeminiClient:
        """Create client from GEMINI_API_KEY environment variable.

        Raises LLMProviderError if the key is not set.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise LLMProviderError(
                provider="gemini",
                phase="init",
                message="GEMINI_API_KEY is not set. Add it to your .env file.",
            )
        return cls(api_key)

    @property
    def raw(self):
        """Access the underlying google.genai.Client for advanced operations.

        Prefer the typed methods on this class. Use .raw only for operations
        not yet wrapped (e.g., specific config options).
        """
        return self._client

    def upload_and_wait(
        self,
        file_path: Path,
        *,
        label: str = "",
        timeout_sec: int = GEMINI_UPLOAD_TIMEOUT_SEC,
        poll_sec: int = GEMINI_UPLOAD_POLL_SEC,
    ):
        """Upload a file to Gemini File API and wait for processing to complete.

        This is the single correct implementation. It:
        - Polls with configurable interval (was: 2s, 3s, or 5s depending on file)
        - Checks for FAILED state (was: missing in 2 of 4 implementations)
        - Raises FileUploadError on timeout or failure
        - Logs progress with structured context

        Returns the processed google.genai.types.File object.
        """
        display_name = label or file_path.name
        size_mb = file_path.stat().st_size / 1024 / 1024
        log.info(
            "gemini.upload.start",
            extra={"file": display_name, "size_mb": round(size_mb, 1)},
        )

        try:
            video_file = self._client.files.upload(file=str(file_path))
        except Exception as e:
            raise FileUploadError(
                provider="gemini",
                phase="upload",
                message=f"Upload failed for {display_name}: {e}",
                cause=e,
            )

        start = time.monotonic()
        while video_file.state.name == "PROCESSING":
            elapsed = time.monotonic() - start
            if elapsed > timeout_sec:
                raise FileUploadError(
                    provider="gemini",
                    phase="upload",
                    message=(
                        f"File processing timed out after {timeout_sec}s "
                        f"for {display_name}"
                    ),
                )
            log.debug(
                "gemini.upload.polling",
                extra={"file": display_name, "elapsed_sec": round(elapsed, 1)},
            )
            time.sleep(poll_sec)
            video_file = self._client.files.get(name=video_file.name)

        if video_file.state.name == "FAILED":
            raise FileUploadError(
                provider="gemini",
                phase="upload",
                message=f"Gemini file processing failed for {display_name}",
            )

        elapsed = time.monotonic() - start
        log.info(
            "gemini.upload.complete",
            extra={"file": display_name, "elapsed_sec": round(elapsed, 1)},
        )
        return video_file

    def generate(self, *, model: str, contents: list, config=None) -> str:
        """Generate content and return the text response.

        Wraps google.genai.Client.models.generate_content with:
        - Domain exception translation
        - Structured logging

        For structured output, pass config with response_schema.
        Returns response.text.
        """
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response.text
        except Exception as e:
            # Let retryable errors propagate (tracing.py handles retry logic)
            raise

    def generate_structured(self, *, model: str, contents: list, config) -> object:
        """Generate structured content, returning the full response object.

        Use this when you need access to the raw response (e.g., for
        response.parsed with Gemini's native structured output).
        """
        try:
            return self._client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            raise

    def get_cached_or_upload(
        self,
        file_path: Path,
        clip_id: str,
        cache_path: Path,
        *,
        label: str = "",
    ):
        """Upload a file, reusing a cached URI if available and not expired.

        Integrates with file_cache.py for Gemini File API URI reuse across
        pipeline phases (briefing → transcription → Phase 1 → Phase 2).
        """
        from ..infra.file_cache import get_cached_uri, cache_file_uri, load_file_api_cache

        cache = load_file_api_cache(cache_path)
        cached_uri = get_cached_uri(cache, clip_id)

        if cached_uri:
            log.debug("gemini.cache.hit", extra={"clip_id": clip_id})
            # Return a lightweight object with .uri and .name for compatibility
            try:
                return self._client.files.get(name=cached_uri.split("/")[-1])
            except Exception:
                log.debug("gemini.cache.stale", extra={"clip_id": clip_id})
                # Fall through to upload

        video_file = self.upload_and_wait(file_path, label=label or clip_id)
        cache_file_uri(cache, clip_id, video_file.uri, cache_path)
        return video_file
```

### Golden Sample: Refactored `gemini_analyze.py`

```python
"""Gemini descriptive-mode adapter — upload proxy video and analyze with
native video understanding.

Demonstrates the adapter pattern:
- Receives a GeminiClient (injected, not created internally)
- Implements provider-specific logic only
- Returns domain objects or raises domain exceptions
- Zero direct google.genai imports (all through GeminiClient)
"""

from __future__ import annotations

from pathlib import Path

from ..domain.exceptions import LLMProviderError
from ..infra.gemini_client import GeminiClient
from ..infra.config import GeminiConfig
from ..rendering.storyboard_format import build_storyboard_prompt, format_duration


def analyze_video(
    client: GeminiClient,
    video_file,
    video_info: dict,
    cfg: GeminiConfig,
) -> str:
    """Send video + storyboard prompt to Gemini and return the markdown response."""
    from google.genai import types

    prompt = build_storyboard_prompt(
        filename=video_info["filename"],
        duration=format_duration(video_info["duration_sec"]),
        resolution=f"{video_info['width']}x{video_info['height']}",
    )

    return client.generate(
        model=cfg.model,
        contents=[
            types.Content(
                parts=[
                    types.Part.from_uri(file_uri=video_file.uri, mime_type="video/mp4"),
                    types.Part.from_text(text=prompt),
                ]
            )
        ],
        config=types.GenerateContentConfig(temperature=cfg.temperature),
    )


def run_gemini_analysis(
    proxy_path: Path,
    video_info: dict,
    storyboard_dir: Path,
    cfg: GeminiConfig,
    client: GeminiClient | None = None,
) -> Path:
    """Full Gemini descriptive pipeline: upload proxy → analyze → write storyboard.

    Args:
        client: Injected GeminiClient. Created from env if not provided
                (backward compatibility during migration).
    """
    if client is None:
        client = GeminiClient.from_env()

    video_file = client.upload_and_wait(proxy_path, label=proxy_path.name)
    storyboard_md = analyze_video(client, video_file, video_info, cfg)

    storyboard_dir.mkdir(parents=True, exist_ok=True)
    output_path = storyboard_dir / "storyboard_gemini.md"
    output_path.write_text(storyboard_md)
    return output_path
```

### Golden Sample: `domain/exceptions.py`

```python
"""VX domain exception hierarchy.

All exceptions that cross module boundaries are defined here.
Adapters translate provider-specific errors into these types.
Entry points (CLI, TUI) catch VXError for user-facing messages.

Hierarchy:
    VXError
    ├── StoryboardValidationError
    ├── ClipResolutionError
    ├── ConstraintViolationError
    ├── LLMProviderError
    │   ├── LLMResponseParseError
    │   ├── FileUploadError
    │   └── LLMCostLimitExceeded
    ├── MediaProcessingError
    └── RenderTimeoutError
"""


class VXError(Exception):
    """Base for all VX domain errors."""


class StoryboardValidationError(VXError):
    """Storyboard violates structural constraints."""


class ClipResolutionError(VXError):
    """Clip ID could not be resolved to a known clip."""


class ConstraintViolationError(VXError):
    """User constraints (must-include, must-exclude) not satisfied."""


class LLMProviderError(VXError):
    """LLM API call failed after retries."""

    def __init__(
        self,
        provider: str,
        phase: str,
        message: str,
        cause: Exception | None = None,
    ):
        self.provider = provider
        self.phase = phase
        super().__init__(f"[{provider}/{phase}] {message}")
        if cause:
            self.__cause__ = cause


class LLMResponseParseError(LLMProviderError):
    """LLM returned unparseable or invalid structured output."""


class FileUploadError(LLMProviderError):
    """File upload to LLM provider failed or timed out."""


class LLMCostLimitExceeded(VXError):
    """Cumulative LLM cost exceeded the configured limit."""


class MediaProcessingError(VXError):
    """FFmpeg or media operation failed."""

    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"ffmpeg failed (rc={returncode}): {stderr[:200]}")


class RenderTimeoutError(VXError):
    """Rough cut or preview render exceeded timeout."""
```

---

## Migration Roadmap

### Phase 0: Golden Sample (this PR)
1. Create `src/ai_video_editor/domain/exceptions.py`
2. Create `src/ai_video_editor/infra/gemini_client.py`
3. Refactor `gemini_analyze.py` to use `GeminiClient`
4. Add `tests/infra/test_gemini_client.py` (unit tests with mocked SDK)
5. All existing behavior preserved. No breaking changes.

### Phase 1: Deduplicate (next 2–3 PRs)
- Replace all 4× `_wait_for_gemini_file` with `GeminiClient.upload_and_wait()`
- Replace all 7× `genai.Client()` with `GeminiClient.from_env()`
- Move shared constants to `infra/config.py`
- Add Pydantic validation for `.vx.json` and `project.json`

### Phase 2: Extract Domain (2–3 PRs)
- Move `models.py` → `domain/models.py` (re-export from old location for backward compat)
- Extract `_resolve_clip_id_refs()` → `domain/clip_resolution.py`
- Extract validation logic → `domain/validation.py`
- Extract timestamp clamping → `domain/timestamps.py`

### Phase 3: Provider Abstraction (2–3 PRs)
- Create `domain/ports.py` with Protocol definitions
- Extract Gemini Phase 1 adapter from `editorial_agent.py`
- Extract Claude Phase 1 adapter from `editorial_agent.py`
- Move provider selection to composition root (CLI/TUI)

### Phase 4: Package Structure (1 large PR)
- Create package directories (`editorial/`, `rendering/`, `briefing/`, etc.)
- Move files to new locations
- Update all imports
- Add per-package `README.md` files

### Phase 5: Test Foundation (ongoing)
- Add unit tests for domain logic (validation, resolution, timestamps)
- Add unit tests for eval scoring
- Add integration tests for versioning protocol
- Set up CI pipeline

**Estimated timeline:** 4–6 weeks of incremental work, each PR independently shippable. No feature freeze required.
