import SwiftUI

enum VXButtonVariant { case primary, secondary, ghost, danger }
enum VXControlSize { case sm, md, lg }

/// The VX button. Emerald primary is rationed (one per view). Press darkens the
/// fill — no scale. Ported from `components/controls/Button.jsx`.
struct VXButton: View {
    var title: String
    var variant: VXButtonVariant = .secondary
    var size: VXControlSize = .md
    var icon: String? = nil
    var action: () -> Void = {}

    @State private var hovering = false

    private var height: CGFloat {
        switch size { case .sm: return VXMetrics.controlSM; case .md: return VXMetrics.controlMD; case .lg: return VXMetrics.controlLG }
    }
    private var fontSize: CGFloat { size == .sm ? 12 : 13 }

    private var fill: Color {
        switch variant {
        case .primary: return hovering ? VXColor.accentHover : VXColor.accent
        case .secondary: return hovering ? VXColor.surfaceActive : VXColor.surfaceRaised
        case .ghost: return hovering ? VXColor.surfaceRaised : .clear
        case .danger: return hovering ? VXColor.statusDanger.opacity(0.16) : .clear
        }
    }
    private var fg: Color {
        switch variant {
        case .primary: return VXColor.textOnAccent
        case .danger: return VXColor.statusDanger
        default: return VXColor.textBody
        }
    }
    private var border: Color {
        switch variant {
        case .primary: return .clear
        case .danger: return VXColor.statusDanger.opacity(0.5)
        default: return VXColor.borderStrong
        }
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let icon { VXIcon(name: icon, size: fontSize + 2, color: fg) }
                Text(title).font(.system(size: fontSize, weight: variant == .primary ? .semibold : .medium))
            }
            .foregroundStyle(fg)
            .padding(.horizontal, size == .sm ? 10 : 14)
            .frame(height: height)
            .background(fill)
            .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusMD).stroke(border, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(VXMotion.fast, value: hovering)
    }
}

/// Square icon button (toolbar affordances).
struct VXIconButton: View {
    var icon: String
    var label: String = ""
    var action: () -> Void = {}
    @State private var hovering = false
    var body: some View {
        Button(action: action) {
            VXIcon(name: icon, size: 16, color: VXColor.textSecondary)
                .frame(width: VXMetrics.controlMD, height: VXMetrics.controlMD)
                .background(hovering ? VXColor.surfaceRaised : .clear)
                .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
        }
        .buttonStyle(.plain)
        .help(label)
        .onHover { hovering = $0 }
        .animation(VXMotion.fast, value: hovering)
    }
}

/// Segmented control — the Story / Timeline toggle. Selected segment = emerald
/// soft fill + accent text. Ported from `SegmentedControl.jsx`.
struct VXSegmentedControl: View {
    struct Option: Identifiable { let value: String; let label: String; var id: String { value } }
    let options: [Option]
    @Binding var selection: String

    var body: some View {
        HStack(spacing: 2) {
            ForEach(options) { opt in
                let active = opt.value == selection
                Text(opt.label)
                    .font(.system(size: 12, weight: active ? .semibold : .medium))
                    .foregroundStyle(active ? VXColor.accent : VXColor.textSecondary)
                    .padding(.horizontal, 12)
                    .frame(height: 26)
                    .background(active ? VXColor.accentSoft : .clear)
                    .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusSM)
                        .stroke(active ? VXColor.accentBorder : .clear, lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusSM))
                    .contentShape(Rectangle())
                    .onTapGesture { selection = opt.value }
            }
        }
        .padding(2)
        .background(VXColor.surfaceCard)
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
    }
}
