// VX Composition P0 spike (v2) — headless proof on THIS machine:
//   A) by-reference playback of an AVMutableComposition from multiple real source
//      clips (no intermediate files), frames actually decoding;
//   B) 4K by-reference decode (synthetic H.264 + HEVC 4K);
//   C) practical composition video-track limit (the ≤16-track caveat).
// Frame proof uses AVAssetImageGenerator (synchronous, headless-reliable — it
// decodes from the referenced sources). Player-status checks run under
// dispatchMain() so AVFoundation's main-queue callbacks are serviced.
// Build: swiftc Spike.swift -o spike   Run: ./spike

import Foundation
import AVFoundation
import CoreVideo
import CoreGraphics

func line() { print(String(repeating: "─", count: 64)) }

struct Seg: Decodable { let clip_id: String; let source: String; let in_sec: Double; let out_sec: Double }

func t(_ s: Double) -> CMTime { CMTime(seconds: s, preferredTimescale: 600) }
func tr(_ a: Double, _ b: Double) -> CMTimeRange { CMTimeRange(start: t(a), end: t(b)) }

func loadTracks(_ asset: AVURLAsset) -> (video: AVAssetTrack?, audio: AVAssetTrack?) {
    var v: AVAssetTrack?; var a: AVAssetTrack?
    let sem = DispatchSemaphore(value: 0)
    Task {
        v = try? await asset.loadTracks(withMediaType: .video).first
        a = try? await asset.loadTracks(withMediaType: .audio).first
        sem.signal()
    }
    sem.wait()
    return (v, a)
}

// Build a 1-video + 1-audio composition (track REUSE) from sequential segments.
func buildComposition(_ segs: [Seg]) -> (AVMutableComposition, CMTime) {
    let comp = AVMutableComposition()
    let vTrack = comp.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
    let aTrack = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
    var cursor = CMTime.zero
    for s in segs {
        let asset = AVURLAsset(url: URL(fileURLWithPath: s.source))
        let (v, a) = loadTracks(asset)
        guard let v = v else { print("  ! no video track in \(s.clip_id)"); continue }
        do {
            try vTrack.insertTimeRange(tr(s.in_sec, s.out_sec), of: v, at: cursor)
            if let a = a { try? aTrack.insertTimeRange(tr(s.in_sec, s.out_sec), of: a, at: cursor) }
            cursor = cursor + tr(s.in_sec, s.out_sec).duration
        } catch { print("  ! insert failed for \(s.clip_id): \(error)") }
    }
    return (comp, cursor)
}

// Headless frame proof: decode a real CGImage from the composition at each time.
func proveFrames(_ comp: AVMutableComposition, at times: [Double]) -> [(Double, Int, Int, Bool)] {
    let gen = AVAssetImageGenerator(asset: comp)
    gen.requestedTimeToleranceBefore = .zero
    gen.requestedTimeToleranceAfter = t(0.4)
    var out: [(Double, Int, Int, Bool)] = []
    for time in times {
        var actual = CMTime.zero
        if let cg = try? gen.copyCGImage(at: t(time), actualTime: &actual) {
            out.append((time, cg.width, cg.height, true))
        } else {
            out.append((time, 0, 0, false))
        }
    }
    return out
}

func runSpike() {
    // A) real multi-source composition -------------------------------------
    line(); print("A) REAL MULTI-SOURCE COMPOSITION (by reference, no files written)"); line()
    let segData = try! Data(contentsOf: URL(fileURLWithPath: "/tmp/vx-spike/segments.json"))
    let segs = try! JSONDecoder().decode([Seg].self, from: segData)
    print("segments: \(segs.count) from \(Set(segs.map{$0.clip_id}).count) distinct clips")
    let (compA, durA) = buildComposition(segs)
    print("composition: \(String(format: "%.1f", durA.seconds))s · video tracks=\(compA.tracks(withMediaType: .video).count) · audio tracks=\(compA.tracks(withMediaType: .audio).count)")
    var cumulative = 0.0; var probeTimes: [Double] = []
    for s in segs { probeTimes.append(cumulative + 0.4); cumulative += (s.out_sec - s.in_sec) }
    let probesA = proveFrames(compA, at: Array(probeTimes.prefix(6)))
    print("frame decode probes (each time falls in a different source segment):")
    for (tt, w, h, ok) in probesA { print("  t=\(String(format: "%5.1f", tt))s → \(ok ? "DECODED \(w)x\(h)" : "no frame")") }
    print("→ \(probesA.filter{$0.3}.count)/\(probesA.count) timestamps decoded a real frame BY REFERENCE")

    // B) 4K mixed-codec ------------------------------------------------------
    line(); print("B) 4K BY-REFERENCE DECODE (synthetic H.264 + HEVC 4K)"); line()
    let fourK = [
        Seg(clip_id: "4k_h264", source: "/tmp/vx-spike/test4k_h264.mp4", in_sec: 0.5, out_sec: 3.5),
        Seg(clip_id: "4k_hevc", source: "/tmp/vx-spike/test4k_hevc.mp4", in_sec: 0.5, out_sec: 3.5),
    ]
    if FileManager.default.fileExists(atPath: fourK[0].source) {
        let (comp4k, dur4k) = buildComposition(fourK)
        print("4K composition: \(String(format: "%.1f", dur4k.seconds))s · video tracks=\(comp4k.tracks(withMediaType: .video).count)")
        let p = proveFrames(comp4k, at: [0.8, 3.6])  // first time in H.264 clip, second in HEVC clip
        for (tt, w, h, ok) in p { print("  t=\(String(format: "%4.1f", tt))s → \(ok ? "DECODED \(w)x\(h)" : "no frame")") }
        print("→ \(p.filter{$0.3 && $0.1 >= 3840}.count)/\(p.count) probes decoded a real 3840-wide frame (mixed codec)")
    } else { print("  (4K test clips not found — skipped)") }

    // C) track-limit probe ---------------------------------------------------
    line(); print("C) COMPOSITION VIDEO-TRACK LIMIT PROBE (the ≤16-track caveat)"); line()
    let segData2 = try! Data(contentsOf: URL(fileURLWithPath: "/tmp/vx-spike/segments.json"))
    let segs2 = try! JSONDecoder().decode([Seg].self, from: segData2)
    let (srcV, _) = loadTracks(AVURLAsset(url: URL(fileURLWithPath: segs2[0].source)))
    for n in [2, 8, 16, 24, 32] {
        let comp = AVMutableComposition()
        for _ in 0..<n {
            let tk = comp.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
            if let srcV = srcV { try? tk?.insertTimeRange(tr(0, 1), of: srcV, at: .zero) }
        }
        let item = AVPlayerItem(asset: comp)
        let player = AVPlayer(playerItem: item)
        player.volume = 0
        let sem = DispatchSemaphore(value: 0)
        var fired = false
        let o = item.observe(\.status, options: [.new]) { it, _ in
            if (it.status == .readyToPlay || it.status == .failed) && !fired { fired = true; sem.signal() }
        }
        player.play()  // kick the playback pipeline (where -11819 surfaces)
        _ = sem.wait(timeout: .now() + 10)
        o.invalidate()
        player.replaceCurrentItem(with: nil)
        let status: String
        switch item.status {
        case .readyToPlay: status = "readyToPlay ✓"
        case .failed:      status = "FAILED: \(item.error?.localizedDescription ?? "?")"
        default:           status = "timeout/unknown"
        }
        print("  \(String(format: "%2d", n)) video tracks → \(status)")
    }

    line(); print("DONE — no intermediate files were written by this spike."); line()
    exit(0)
}

// Run work off-main so AVFoundation's main-queue callbacks get serviced by dispatchMain().
DispatchQueue.global().async { runSpike() }
dispatchMain()
