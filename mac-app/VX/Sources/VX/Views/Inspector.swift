import SwiftUI

/// The right panel: selected-segment detail, green/red in-out scrubber, metadata
/// and description. Ported from `ui_kits/mac-app/Inspector.jsx`. (The transcript
/// channel view will be wired once the sidecar exposes per-clip transcripts.)
struct Inspector: View {
    let segment: Segment?
    @State private var tab = "inspector"

    var body: some View {
        if let seg = segment {
            VStack(alignment: .leading, spacing: 0) {
                header(seg)
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Scrubber(segment: seg)
                        HStack(spacing: 10) {
                            MetaCell(label: "Duration", value: "\(String(format: "%.1f", seg.duration))s")
                            MetaCell(label: "Transition", value: seg.transition.replacingOccurrences(of: "_", with: " "))
                        }.padding(.top, 18)
                        Eyebrow("Description").padding(.top, 18).padding(.bottom, 8)
                        Text(seg.description).font(VXFont.sm).foregroundStyle(VXColor.textSecondary).lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: 6) {
                            VXIcon(name: "mic", size: 13, color: VXColor.textMuted)
                            Text("Audio: \(seg.audioNote.replacingOccurrences(of: "_", with: " "))")
                                .font(VXFont.xs).foregroundStyle(VXColor.textMuted)
                        }.padding(.top, 14)
                        if !seg.textOverlay.isEmpty {
                            Eyebrow("Text overlay").padding(.top, 16).padding(.bottom, 6)
                            Text(seg.textOverlay).font(VXFont.sm).italic().foregroundStyle(VXColor.transcriptMusic)
                        }
                    }
                    .padding(.horizontal, 18).padding(.vertical, 16)
                }
                footer
            }
            .frame(maxHeight: .infinity)
        } else {
            Text("Select a segment to inspect.")
                .font(VXFont.sm).foregroundStyle(VXColor.textMuted).padding(24)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    private func header(_ seg: Segment) -> some View {
        HStack(spacing: 9) {
            Text("#\(seg.index)").font(VXFont.mono(13)).foregroundStyle(VXColor.textFaint)
            Text(seg.clipID).font(VXFont.heading).foregroundStyle(VXColor.textPrimary).lineLimit(1)
            PurposeTag(purpose: seg.purpose)
            Spacer()
        }
        .padding(.horizontal, 18).padding(.top, 16).padding(.bottom, 12)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            VXButton(title: "Reset", variant: .danger, size: .sm)
            Spacer()
            VXButton(title: "Apply", variant: .primary, size: .sm, icon: "check")
        }
        .padding(.horizontal, 18).padding(.vertical, 12)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderDefault), alignment: .top)
    }
}

/// The signature green/red in-out scrubber.
struct Scrubber: View {
    let segment: Segment
    var body: some View {
        let clipDur = segment.outSec + 3.2  // show some context beyond the range
        let inPct = segment.inSec / clipDur
        let outPct = segment.outSec / clipDur
        VStack(spacing: 8) {
            GeometryReader { geo in
                let w = geo.size.width
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: VXMetrics.radiusSM).fill(VXColor.surfaceSunken)
                    // selected range fill
                    RoundedRectangle(cornerRadius: 3)
                        .fill(VXColor.accentSoftStrong)
                        .overlay(RoundedRectangle(cornerRadius: 3).stroke(VXColor.accentBorder, lineWidth: 1))
                        .frame(width: max(0, w * (outPct - inPct)))
                        .offset(x: w * inPct)
                    // in handle
                    RoundedRectangle(cornerRadius: 3).fill(VXColor.markIn).frame(width: 10).offset(x: w * inPct - 5)
                    // out handle
                    RoundedRectangle(cornerRadius: 3).fill(VXColor.markOut).frame(width: 10).offset(x: w * outPct - 5)
                }
            }
            .frame(height: 30)
            HStack {
                Timecode(seconds: segment.inSec, tone: .inPoint)
                Spacer()
                Timecode(seconds: segment.outSec, tone: .outPoint)
            }
        }
    }
}

struct MetaCell: View {
    let label: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(1).foregroundStyle(VXColor.textFaint)
            Text(value).font(VXFont.base).foregroundStyle(VXColor.textPrimary)
        }
        .padding(.horizontal, 11).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(VXColor.surfaceCard)
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
    }
}
