import SwiftUI

/// PurposeTag — the tiny uppercase pill that names a segment's editorial role,
/// colored by the fixed 15-hue purpose vocabulary. Ported from `PurposeTag.jsx`.
struct PurposeTag: View {
    let purpose: String
    var size: VXControlSize = .md

    var body: some View {
        let hue = Purpose.color(for: purpose)
        Text(Purpose.from(purpose).label.uppercased())
            .font(.system(size: size == .sm ? 9 : 10, weight: .semibold))
            .tracking(0.5)
            .foregroundStyle(hue)
            .padding(.horizontal, size == .sm ? 5 : 7)
            .padding(.vertical, size == .sm ? 2 : 3)
            .background(hue.opacity(0.16))
            .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusXS).stroke(hue.opacity(0.45), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusXS))
    }
}

enum TimecodeTone { case neutral, inPoint, outPoint }

/// Monospaced, tabular timecode. `m:ss.t`. The green/red tones mark in/out.
struct Timecode: View {
    var seconds: Double? = nil
    var range: (Double, Double)? = nil
    var tone: TimecodeTone = .neutral
    var fontSize: CGFloat = 12

    static func fmt(_ s: Double) -> String {
        let m = Int(s) / 60, sec = Int(s) % 60, t = Int((s.truncatingRemainder(dividingBy: 1)) * 10)
        return "\(m):" + String(format: "%02d", sec) + (t != 0 ? ".\(t)" : "")
    }

    private var color: Color {
        switch tone { case .inPoint: return VXColor.markIn; case .outPoint: return VXColor.markOut; case .neutral: return VXColor.textTertiary }
    }

    var body: some View {
        Group {
            if let r = range {
                Text("\(Self.fmt(r.0)) → \(Self.fmt(r.1))")
            } else if let s = seconds {
                Text(Self.fmt(s))
            } else { Text("–") }
        }
        .font(VXFont.mono(fontSize))
        .monospacedDigit()
        .foregroundStyle(color)
    }
}

enum BadgeTone { case neutral, accent, success, danger }

/// Small status pill (version, clip count, "cached").
struct Badge: View {
    var tone: BadgeTone = .neutral
    var dot: Bool = false
    let text: String

    private var fg: Color {
        switch tone { case .neutral: return VXColor.textSecondary; case .accent: return VXColor.accent
        case .success: return VXColor.statusSuccess; case .danger: return VXColor.statusDanger }
    }
    private var bg: Color {
        switch tone { case .neutral: return VXColor.surfaceRaised; case .accent: return VXColor.accentSoft
        case .success: return VXColor.accentSoft; case .danger: return VXColor.statusDanger.opacity(0.14) }
    }

    var body: some View {
        HStack(spacing: 5) {
            if dot { Circle().fill(fg).frame(width: 6, height: 6) }
            Text(text).font(.system(size: 10, weight: .semibold))
        }
        .foregroundStyle(fg)
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(bg)
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusXS))
    }
}
