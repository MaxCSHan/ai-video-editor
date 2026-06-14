# VX — Composition Engine Architecture

> Status: v0.1 design, for evaluation. Fact-checked against 2026 video-tech best
> practices (see §7). This doc covers the **composition/rendering layer** — how a
> storyboard becomes a preview, a direct export, and a pro-NLE handoff. The
> **agent-director layer** (how the AI gets context and makes the edit) is a
> separate companion doc — see `SYSTEM-DESIGN.md` → "Agent-Director layer (Phase 2)".

---

## 1. Framing: VX is an AI-native composer, not an NLE

VX's job is to get the **big-picture storyline right** — ingest footage, build
context, and let the AI director compose a coherent edit — then either:

- **Hand a roughly-tuned timeline to DaVinci Resolve** (the pro path) so the user
  does detailed finishing there, *not* in VX; or
- **Render a direct export** (the light path) for someone who just wants a
  shareable vlog now.

Frame-accurate trimming, multitrack mixing, color grading, and keyframing are
**explicitly out of scope**. We win on story + rough assembly and hand off the rest.

### Why the current compose engine fights this

`rough_cut.py` renders the whole cut as a **batch of files**: for every segment it
runs ffmpeg to decode → filter → encode a standalone `.mp4`, validates them in
three layers, then stream-copy-concatenates. It's a well-built *headless/CLI*
design — and the wrong model for an interactive app:

| Symptom | Measured (the `myanmar` project) |
|---|---|
| Preview latency | **~85 s** per cut (84.7 s observed) — full re-extract + concat |
| Disk growth | `library/myanmar/exports/` = **814 MB** across ~30 version dirs |
| Intermediate files | **168** `seg_*.mp4` segment files (**72 MB**) + a `rough_cut.mp4` per version |
| Storage model | one full-res copy of every used second, per render version |

Every Preview writes a fresh export version, so storage and time grow without
bound. The fix is to stop *rendering to preview* and instead *compose by reference*.

---

## 2. The Composition Core: one timeline, three outputs

The `EditorialStoryboard` is already the composition. Each `Segment` (`models.py`)
carries `clip_id, in_sec, out_sec, transition, audio_note, text_overlay` — an
**edit-decision list that references original clips by time range**. We stop
treating it as "instructions to render files" and treat it as a **live timeline
object** with three consumers:

```
                     EditorialStoryboard  (the composition / EDL)
                                │
           ┌────────────────────┼─────────────────────────┐
           ▼                    ▼                          ▼
  1) PREVIEW (instant)   2) DIRECT EXPORT (light)   3) PRO HANDOFF (Resolve)
  AVMutableComposition   AVAssetExportSession /     OTIO (.otioz) primary +
  + AVPlayer (by ref,    AVAssetWriter →            FCPXML 1.10 fallback →
  zero files)            VideoToolbox HW encode     original full-res media
```

All three read the same storyboard. The Python `rough_cut.py` **stays** as the
headless/CLI path and a fallback master render; the AVFoundation engine becomes the
**app's** preview + light export.

---

## 3. Preview engine — `AVMutableComposition` by reference (the big win)

Build an in-memory composition that inserts each segment's `[in_sec, out_sec]`
range **from the original asset** into a composition track, then play it with
`AVPlayer`. **Nothing is written to disk; playback is instant and scrubbable across
the whole edit.** Editing an in/out point mutates a `CMTimeRange` and the preview
updates live — this is "watch it re-assemble" with **no render step**.

```swift
let composition = AVMutableComposition()
let videoTrack = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
let audioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
var cursor = CMTime.zero
for seg in storyboard.segments {                       // sequential EDL
    let asset = AVURLAsset(url: originalURL(for: seg.clipID))
    let range = CMTimeRange(start: seconds(seg.inSec), end: seconds(seg.outSec))
    try videoTrack.insertTimeRange(range, of: asset.tracks(withMediaType: .video)[0], at: cursor)
    try audioTrack.insertTimeRange(range, of: asset.tracks(withMediaType: .audio)[0], at: cursor)
    cursor = cursor + range.duration
}
let item = AVPlayerItem(asset: composition)
player.replaceCurrentItem(with: item)                  // AVPlayer plays it, no files
```

**Track reuse is mandatory** (caveat §7.1): a sequential cut needs only **one video
+ one audio track**; dissolves add a **second "B" video track** for the overlap. We
never create one track per clip.

**Background-render cache.** Pure reference playback is instant for cuts; stacked
dissolves/overlays can hitch. We adopt Final Cut Pro's model — reference playback +
a small background render of only the *effected* spans — rather than rendering the
whole timeline. (See §7.2.)

This is the seam in `mac-app/VX/Sources/VX/Components/PlayerLayerView.swift`: it
already hosts an `AVPlayer` via `AVPlayerLayer`; the composition simply becomes its
`AVPlayerItem`.

---

## 4. Direct export — light-user path

Render the composition **once** to a single file via VideoToolbox on the M-series
media engine. No per-segment files.

- **`AVAssetExportSession`** — preset-based, simplest, hardware-encoded. Good for
  the default "share" export.
- **`AVAssetWriter`** — when we need bitrate/profile/HDR control (HLG via HEVC
  Main 10, custom color properties).

**2026 delivery defaults** (§7, social-export sources):

| Target | Codec / container | Resolution | Bitrate | Color |
|---|---|---|---|---|
| Social / vlog (default) | H.264 / MP4 (+AAC) | 1080p | 8–12 Mbps | Rec.709 SDR |
| Social 4K | H.264 or HEVC / MP4 | 2160p | 35–45 Mbps | Rec.709 SDR |
| Optional master | ProRes 422 / MOV | source | high | Rec.709 / P3 |
| HDR (YouTube/Apple TV) | HEVC Main10 / MP4 | — | — | HLG / HDR10 |

Expose simple presets ("YouTube / TikTok / Quick web / Master"). Default to SDR
Rec.709 for predictable social results; tone-map HDR→SDR on the way out.

---

## 5. Pro handoff — timeline interchange to DaVinci Resolve

The roughly-tuned timeline goes to Resolve as an interchange file referencing the
**original full-resolution media** — so the user finishes there.

- **Primary (strategic): OpenTimelineIO `.otioz`.** OTIO is the 2026 industry
  interchange standard; **Resolve was the first NLE with native OTIO support
  (18.6+)**, and Premiere/Avid added it in 2025–2026. `.otioz` bundles the timeline
  + media for clean transport. Carries cut order, in/out, timecode, tracks,
  clip names.
- **Fallback (proven now): FCPXML 1.10.** The existing `fcpxml_export.py` already
  works with Resolve, uses rational-number frame-exact timing, and carries clip
  order, in/out, **dissolve transitions**, **title/monologue overlays**, **caption
  lane**, and **audio levels from `audio_note`**. Pin to 1.10 (1.11 Resolve support
  undocumented).

**What does *not* survive** (by design — Resolve's job): color grading, non-dissolve
transitions, per-segment styling beyond the title template.

**Relink reliably** (caveat §7.4): embed **reel names** + preserve **source
timecode** (FCPXML export already records source TC in each asset's `start`).
Resolve conforms by reel name → timecode → filename. Offer an optional media-package
export for moved drives.

> **Note on the Premiere↔OTIO caveat:** Premiere's OTIO export does *not* import
> into Resolve (file-ref incompatibility). Our direction is VX→Resolve, which is
> fine — but this is exactly why FCPXML 1.10 stays the guaranteed fallback.

---

## 6. Feature-parity port table (Python ffmpeg → AVFoundation)

To reach parity, the native engine must reproduce what `rough_cut.py` does at
encode time. This is the real implementation cost.

| Concern | Today (`rough_cut.py`) | Native AVFoundation mechanism | Difficulty |
|---|---|---|---|
| Cuts + ordering | per-segment extract + concat | `insertTimeRange` into reused track | trivial |
| Dissolve / fade | FCPXML only (concat assumes cuts) | `AVVideoComposition` opacity ramp on B-track | medium |
| Text / monologue overlays | ffmpeg `drawtext` | `AVVideoCompositionCoreAnimationTool` + `CATextLayer` | medium |
| Speech captions (lane 2) | `drawtext`, collision-aware | second CALayer lane | medium |
| Audio levels (`audio_note`) | per-segment volume | `AVAudioMix` input volume per range | easy |
| Scale / pad / crop / fps | ffmpeg filters | `AVVideoComposition` render size + layer instructions | medium |
| **Color normalization** | device-aware HLG/BT.2020↔709 (`_get_color_vf_for_clip`, `format_analyzer`) | `AVVideoComposition` color properties / Core Image / Metal | **hard (parity-critical)** |
| Validation | 3-layer probe/compat/integrity | mostly unneeded (no intermediate files); validate on export | simplification |

**Color is the load-bearing port** (§7.6): if the native preview doesn't apply the
same device-aware normalization, preview will not match the exported file or the
Resolve result. This must be designed before P1 ships beyond plain cuts.

---

## 7. 2026 fact-check — VALIDATED, with six caveats

Researched against Apple developer docs, Apple newsroom (M-series), the ASWF/OTIO
project, NLE vendor docs, and 2025–2026 industry sources.

### Verdict: the architecture is current best practice
- **`AVMutableComposition` + `AVPlayerItem` + `AVPlayer` is the native macOS
  edit-by-reference model in 2026** — it's how Final Cut Pro and iMovie work
  (reference playback + background render for effects; render only on export). It
  plays composed time ranges from sources **without pre-rendering**.
  *Sources: Apple AVFoundation docs (`AVMutableComposition`, `AVPlayerItem`); FCP
  "Background rendering" support guide; WWDC22 "Display HDR video in EDR with
  AVFoundation and Metal".*
- **Apple Silicon media engine** does hardware H.264/HEVC/ProRes decode+encode;
  AV1 **decode** from M3, AV1 **encode** announced with M5 (2026). It sustains
  multiple concurrent 4K streams in real time — so **4K timelines preview by
  reference without proxies on M3+** for simple cut/trim edits.
  *Sources: Apple newsroom M3 Ultra (Mar 2025) & M5 Pro/Max (Mar 2026); Mac Studio
  tech specs (concurrent 8K ProRes stream counts); Bitmovin AV1-support analysis.*
- **Export:** `AVAssetExportSession` (preset) and `AVAssetWriter` (control) both
  delegate to VideoToolbox hardware encoders; HLG/HDR10 via HEVC Main 10.
  *Sources: Apple dev docs (`AVAssetExportSession`, `AVAssetWriter`); Apple dev
  news "Support HDR video playback, editing, and export".*
- **Interchange:** OTIO is the emerging standard; **Resolve had native OTIO first
  (18.6+)**; `.otioz` bundles media; OTIO Beta 14 (2025–26) improved bundles. FCPXML
  1.10 is the mature, frame-exact Resolve path. "AI rough-assembly → timeline
  handoff to a pro NLE" is a recognized 2026 pattern ("hybrid production").
  *Sources: ASWF OTIO project updates; DaVinci Resolve 18.6 manual (OTIO import);
  fcp.cafe FCPXML case study; Adobe/Avid 2025–26 OTIO announcements; DiGen 2026 AI
  workflow guide.*
- **Light-export defaults:** H.264 MP4 + Rec.709 SDR is the 2026 social default
  (≈98% device compatibility, 3–30× faster encode than HEVC); ProRes 422 for a
  master. *Sources: 2026 social-codec/color guides (red5, pixflow), OWC mezzanine
  guide, YouTube HDR upload docs.*

### Caveats designed around (load-bearing)
1. **Track count: keep small (≤~4); the real limit is decode bandwidth.** Forum
   reports cite playback failure (error −11819) past ~30 tracks, but the P0 spike
   (§9) did **not** reproduce it — 2/8/16/24/32 tracks all reached `readyToPlay` on
   this M-series machine — so it's not a hard count wall; the true constraint is
   concurrent real-time **decode bandwidth**. Either way our sequential EDL uses
   only **2 video + 2 audio**, far under any limit, reusing tracks for
   non-overlapping ranges. *(Apple Developer Forums 2024–25; refined by P0 spike.)*
2. **Background render for heavy effects.** Reference playback can hitch on stacked
   dissolves/overlays; render only the effected spans (FCP model), not the timeline.
3. **HDR scope.** HLG/HDR10 export is native; **Dolby Vision export is not** in
   stock AVFoundation. Target HLG, or SDR Rec.709 for social (tone-map HDR→SDR).
4. **OTIO isn't a universal round-trip.** Premiere's OTIO → Resolve import is
   broken; VX→Resolve is fine, but keep **FCPXML 1.10 as the guaranteed fallback**
   and embed reel names + source timecode for relink.
5. **Proxies optional, not required.** 4K-simple previews fine by reference on M3+;
   a **540/720p ProRes-Proxy playback proxy** still helps older Macs / very high
   segment counts / smoothest scrub. Keep it an option (substrate blocker #2).
6. **Color parity is the hardest port** (see §6) — reproduce the device-aware
   normalization as `AVVideoComposition` color instructions, or preview ≠ export.

---

## 8. Sidecar contract changes (when implemented)

The native engine shifts compositing from Python to Swift but keeps the sidecar as
the data + jobs source:

| Endpoint | Purpose | Moves to Swift? |
|---|---|---|
| `GET /projects/{id}/composition` | the storyboard EDL the app composes (exists today as `/storyboard`) | — (Python serves data) |
| `GET /media/playback/{id}/{clip}` | optional 540/720p playback proxy | Python generates, Swift plays |
| Native preview | AVMutableComposition + AVPlayer | **Swift** (no sidecar round-trip) |
| `POST /projects/{id}/export` | direct render job (fallback = Python `rough_cut`) | Swift (AVAssetWriter) or Python |
| `POST /projects/{id}/handoff/otio` | new `.otioz` writer | Python (alongside `fcpxml_export.py`) |
| `POST /projects/{id}/handoff/fcpxml` | existing FCPXML export | Python (exists) |

Preview compositing happens entirely in the app (instant, offline). Python keeps
ownership of data, interchange writers, and the headless/master render fallback.

---

## 9. Phased implementation roadmap (deferred — for evaluation)

Each phase keeps `rough_cut.py` as a working fallback and adds a verification spike.

- **P0 (de-risk spike) — DONE ✓.** Built an `AVMutableComposition` from 8 real
  `myanmar` segments (6 distinct sources, 1 video + 1 audio track) and decoded real
  frames by reference at 6 timestamps; decoded real 4K H.264 **and** HEVC frames by
  reference; track probe passed to 32. **Zero files written.** Verdict: GO. Spike +
  results in `mac-app/VX/spikes/CompositionSpike/`.
- **P1 Composition preview (cuts only):** `CompositionBuilder` (storyboard →
  composition) behind `PlayerLayerView`; instant, zero-storage preview. Kills the
  814 MB / 85 s problem for the common case.
- **P2 Native parity:** `AVVideoComposition` dissolves + CALayer overlays/captions
  + **color instructions** + audio mix; background-render cache.
- **P3 Direct export:** `AVAssetExportSession`/`AVAssetWriter` with social + master
  presets (VideoToolbox).
- **P4 OTIO handoff:** add `.otioz` writer alongside FCPXML; relink validation
  against Resolve.

## 10. Open decisions
- **OTIO now or later?** FCPXML 1.10 already works; OTIO is strategically better but
  adds a writer. Recommend P4 (after preview/export land).
- **Background-render cache scope** — how aggressively to pre-render effected spans.
- **HDR scope** — ship SDR-only first, add HLG in P3? (Dolby Vision is out.)
- **Playback proxy** — generate eagerly at ingest, lazily on first editor open, or
  skip on M3+? (Ties to substrate blocker #2 in `PERFORMANCE.md`.)

---

## Companion
The intelligence side — how the agent gets context, makes director decisions,
supports built-in + user-authored styles, and the agent-native UIUX — is **Phase 2:
`AGENT-DIRECTOR-ARCHITECTURE.md`** (see `SYSTEM-DESIGN.md`). The two layers meet at
one point: "see the current result efficiently" is the *same* live
`AVMutableComposition` this doc describes — the agent and the director look at one
shared, instantly-updating cut.
