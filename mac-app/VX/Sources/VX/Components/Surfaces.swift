import SwiftUI

/// Flat card on `--surface-card` with a 1px hairline. Selectable cards gain an
/// emerald border + soft glow. Ported from `components/layout/Card.jsx`.
struct VXCard<Content: View>: View {
    var selectable: Bool = false
    var selected: Bool = false
    var padding: CGFloat = 14
    @ViewBuilder var content: () -> Content
    @State private var hovering = false

    var body: some View {
        content()
            .padding(padding)
            .background(VXColor.surfaceCard)
            .overlay(
                RoundedRectangle(cornerRadius: VXMetrics.radiusLG)
                    .stroke(selected ? VXColor.accentBorder : (hovering && selectable ? VXColor.borderStrong : VXColor.borderDefault),
                            lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusLG))
            .shadow(color: selected ? VXColor.accent.opacity(0.25) : .clear, radius: selected ? 16 : 0, y: 4)
            .onHover { if selectable { hovering = $0 } }
            .animation(VXMotion.fast, value: hovering)
            .animation(VXMotion.fast, value: selected)
    }
}

/// 52px toolbar with left / center / right slots. Translucent chrome.
struct VXToolbar<L: View, C: View, R: View>: View {
    @ViewBuilder var left: () -> L
    @ViewBuilder var center: () -> C
    @ViewBuilder var right: () -> R

    init(@ViewBuilder left: @escaping () -> L,
         @ViewBuilder center: @escaping () -> C = { EmptyView() },
         @ViewBuilder right: @escaping () -> R = { EmptyView() }) {
        self.left = left; self.center = center; self.right = right
    }

    var body: some View {
        ZStack {
            HStack { left(); Spacer(); right() }
            center()
        }
        .padding(.horizontal, 18)
        .frame(height: VXMetrics.toolbarH)
        .background(VXColor.materialChrome)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderSubtle), alignment: .bottom)
    }
}

/// Bottom-right toast that auto-dismisses. Slides up, no bounce.
struct VXToast: View {
    let text: String
    var tone: BadgeTone = .success
    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(tone == .danger ? VXColor.statusDanger : VXColor.statusSuccess).frame(width: 7, height: 7)
            Text(text).font(VXFont.sm).foregroundStyle(VXColor.textBody)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(VXColor.surfaceRaised)
        .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusMD).stroke(VXColor.borderStrong, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
        .shadow(color: .black.opacity(0.45), radius: 14, y: 8)
    }
}

/// Thin progress meter (briefing completion, etc.).
struct ProgressMeter: View {
    var label: String
    var detail: String = ""
    var value: Double
    var maxValue: Double
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Eyebrow(label)
                Spacer()
                Text(detail).font(VXFont.xs).foregroundStyle(VXColor.textMuted)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(VXColor.surfaceRaised)
                    Capsule().fill(VXColor.accent)
                        .frame(width: geo.size.width * (maxValue > 0 ? value / maxValue : 0))
                }
            }
            .frame(height: 5)
        }
    }
}
