# VX — UI/UX Design

> The design contract is the **VX Design System** (fetched bundle), built
> explicitly to convert VX into a native macOS app. This doc maps that system to
> the SwiftUI implementation and specifies the screens — including the
> not-yet-mocked surfaces that "The Living Cut" (see `PRD.md`) introduces.

## 1. The five things that make a surface read as VX

1. **The dark room.** Near-black neutral surfaces (`#060606` → `#2a2a2a`);
   footage and the colored timeline are the only bright things. Depth comes from
   1px hairline borders, not shadow on flat surfaces. → `VXColor` (ported 1:1
   from `tokens/colors.css`).
2. **Emerald is the one action color** (`#2ecc71` = go / render / apply /
   in-point). Ration it: one primary button per view. Red (`#e74c3c`) is the
   out-point / destructive counterweight. → `VXColor.accent`, `.markIn`, `.markOut`.
3. **The 15-hue purpose vocabulary** colors every EDL segment (hook, establish,
   b-roll, climax, payoff, outro…). Fixed hues — never recolor. → `Purpose` enum.
4. **SF Pro + SF Mono**, 13px base, with the uppercase 1.5px-tracked **eyebrow**
   opening every section. All timecodes/costs are monospaced & tabular. →
   `VXFont`, `Eyebrow`, `Timecode`.
5. **Plain, precise, numbers-forward voice** — never cheerful, never emoji. (The
   separate lowercase-whisper voice is only for AI-generated video narration.)

## 2. Foundations → SwiftUI mapping

| Design token file | SwiftUI port | Notes |
|---|---|---|
| `tokens/colors.css` | `DesignSystem/VXColors.swift` | exact hex; semantic aliases preserved |
| `tokens/typography.css` | `DesignSystem/VXType.swift` | `.system` = SF Pro, `.monospaced` = SF Mono, 13px base |
| `tokens/spacing.css` | `DesignSystem/VXMetrics.swift` | radii, 30px control height, 248/320 rails |
| `tokens/motion.css` | `VXMotion` | ease-out 120/160/240ms — "cut, don't bounce" |
| `--purpose-*` | `DesignSystem/Purpose.swift` | the fixed 15-hue map |

**Components** (`Components/`) port the kit primitives: `VXButton`,
`VXIconButton`, `VXSegmentedControl`, `PurposeTag`, `Timecode`, `Badge`,
`VXCard`, `VXToolbar`, `VXToast`, `ProgressMeter`, `VXIcon`.

**Iconography:** the kit ships Lucide-style stand-ins; the app uses **SF Symbols**
(the design's recommended production system) via `VXIcon`, which maps each kit
name to a symbol (`play`→`play.fill`, `sparkle`→`sparkles`, `film`→`film`, …).

**Vibrancy & chrome:** hidden title bar + traffic lights + translucent
`--material-chrome` sidebar/toolbar (`backdrop-filter: blur(30px)`), matching
native macOS. The desktop has a faint emerald-tinted radial behind the window.

## 3. Screens

The window is the AppShell: traffic-light title bar, a vibrancy sidebar
(Library / Briefing / Settings + Recents), and the content area. Ported from
`ui_kits/mac-app/AppShell.jsx` → `Views/RootView.swift`.

### 3.1 Library — `Views/LibraryView.swift`
Project grid with thumbnails, version/mode/provider badges, search, and an
**Import a folder of clips** CTA. Wired to live `GET /projects`.
- **New:** the import CTA opens a native folder picker → a create+analyze job
  with live progress on the tile.

### 3.2 Briefing — `Views/BriefingView.swift`
The AI-guided smart briefing: a quick-scan summary + targeted questions
(AI bubble + answer field), with a progress meter. Entry point for creating a
project from a folder.
- **New (not in kit):** a quick-scan **visual dashboard** above the Q&A —
  detected people as chips, activities as a colored ribbon — so the user sees
  what VX saw. *(Face-crop chips require a representative-frame extraction pass;
  until then, role/text chips — see `SYSTEM-DESIGN.md` gaps.)*

### 3.3 Editor — `Views/EditorView.swift` + `Views/Inspector.swift` *(the centerpiece)*
The replacement for the one-shot HTML preview. Ported faithfully:
- **Player** — 16:9 well tinted by the active segment's purpose hue; AVKit
  `VideoPlayer` loads the clip proxy (→ playback proxy once it lands).
- **Timeline strip** — proportional colored blocks, one per segment; click to
  select; selected block outlined.
- **EDL table** — `#`, clip, in→out, duration, purpose tag, description; row
  selection mirrors the strip.
- **Status bar** — segment count, runtime, and the **live cost/token receipt**
  (`GET /projects/{id}/cost`); shows job stage/progress while a job runs.
- **Inspector** — selected segment with the green/red in-out **Scrubber**,
  metadata cells, description; Reset / Apply footer.
- **Section rail** (Timeline mode) — story-arc sections.

This screen gains the most under "The Living Cut" (all roadmap):
- **React Mode** — a "Review with me" beat-by-beat card stack (thumbnail + plain
  reason + KEEP / CUT / TELL‑IT), then "Apply my notes" → a **ghost-diff**
  proposal card with per-edit toggles and a projected quality delta.
- **Direct manipulation** — the scrubber writes back via a synchronous edit
  endpoint (sub-render-cycle), with **magnetic speech-boundary snap guides** and
  amber "clips a word" dots; drag-to-reorder; purpose/transition pickers; a
  **Discarded tray** with restore.
- **Watch it re-assemble** — a proxy-mode re-cut refreshes the player with only
  changed beats re-encoded.
- **A/B variants** — "cut it both ways" → two-up players; **Promote** to converge.
- **Pro escape hatch** — Export FCPXML in the Render modal.

### 3.4 Settings — `Views/SettingsView.swift`
Provider (Gemini/Claude), language, visual mode, snap-to-scene-cuts. Currently a
faithful static surface; wiring provider/snap strength is roadmap.

## 4. Interaction & motion principles

- **Hover** lightens one surface rung; **selected** = emerald soft fill + accent
  border; **press** darkens the fill (no scale); **focus** = 3px emerald ring.
- **Motion is a cut, not a bounce** — short ease-out (`VXMotion`), no springs.
- **Numbers are first-class** — costs/tokens/durations/timecodes are monospaced
  and exact (`13 calls · 64,120 tok · ~$0.0321`).
- **Errors are diagnostic, not cute** — e.g. "out of bounds — clamped to clip
  duration." No emoji in the interface.

## 5. Accessibility & keyboard (roadmap)

- One **server-authoritative undo stack** shared by manual + react + AI edits, so
  Cmd+Z behaves predictably regardless of edit origin (resolves the
  client-vs-server undo ambiguity flagged in review).
- Respect `prefers-reduced-motion`. Keyboard-first navigation of the EDL and
  React card stack. EDL search/filter for 50–200+ segment cuts.
