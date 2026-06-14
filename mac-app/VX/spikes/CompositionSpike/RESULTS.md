# Composition P0 spike — results

Throwaway proof for `mac-app/docs/COMPOSITION-ARCHITECTURE.md` §9 (P0): does
`AVMutableComposition` play an edit **by reference** (no intermediate files) on
this machine, including 4K and mixed codecs, and where is the track limit?

## Verdict: GO. Composition-by-reference works; P1 (`CompositionBuilder`) is de-risked.

Run on: Apple Silicon, macOS (Swift 6.2 toolchain, `swiftc` default mode).
Sources: real `myanmar` clips (1080p60 H.264 `.MOV`) + synthetic 4K H.264/HEVC.

| Test | Result |
|---|---|
| **A. Multi-source by reference** | **6/6** probe timestamps decoded real 1920×1080 frames from **6 distinct source clips** in one composition (78.1s, **1 video + 1 audio track**, reuse pattern). **No files written.** |
| **B. 4K mixed-codec by reference** | **2/2** probes decoded real **3840×2160** frames — one from a 4K H.264 clip, one from a 4K HEVC clip — in a single composition. |
| **C. Track-limit probe** | 2, 8, 16, 24, **32** video tracks all reached `readyToPlay` — the hard −11819 failure did **not** reproduce here. |

## What this confirms
- The storyboard EDL can be played as an `AVMutableComposition` built **by
  reference** across multiple original sources — the core of the new preview
  engine. No per-segment extraction, no concat, **zero intermediate storage**
  (vs. the current 814 MB / ~85 s batch render).
- 4K and mixed H.264/HEVC decode by reference on this M-series machine (frames
  proven via `AVAssetImageGenerator`, headless).
- Track reuse (1 video + 1 audio for a sequential cut) is correct and sufficient.

## Refined caveat (updates doc §7.1)
The reported "≤16 composition tracks / error −11819" did **not** reproduce at 32
tracks for *prepared* playback here. Two notes: (1) `readyToPlay` proves the item
prepared, not that 32 concurrent 4K streams sustain real-time playback — the true
constraint is concurrent real-time **decode bandwidth**, not a hard track count;
(2) our design uses only **2 video + 2 audio tracks** regardless, so we're far
under any limit. Treat the track count as "keep it small (≤~4)" guidance, not a
hard failure boundary — and re-test sustained playback under real effect load.

## How to reproduce
```bash
# 1. resolve real segments + 4K test clips into /tmp/vx-spike (see git history of this dir),
#    or regenerate: segments.json from the sidecar storyboard + ffmpeg testsrc2 4K clips.
cd /tmp/vx-spike
swiftc Spike.swift -o spike && ./spike
```
`Spike.swift` (in this dir) reads `/tmp/vx-spike/segments.json` and the 4K test
clips. It writes nothing. Note: uses the synchronous `copyCGImage(at:)`
(deprecated macOS 15) for headless frame proof — fine for a spike; the production
`CompositionBuilder` will use `AVPlayer` for live preview, not the image generator.
