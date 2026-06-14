import SwiftUI

/// Maps the design kit's Lucide-style `VXIcon` names to native SF Symbols — the
/// recommended production icon system per the design README. Stroke-style,
/// monochrome, inherits the given color.
struct VXIcon: View {
    let name: String
    var size: CGFloat = 16
    var color: Color = VXColor.textTertiary

    private static let map: [String: String] = [
        "play": "play.fill",
        "pause": "pause.fill",
        "film": "film",
        "download": "square.and.arrow.down",
        "plus": "plus",
        "search": "magnifyingglass",
        "folder": "folder",
        "sparkle": "sparkles",
        "settings": "gearshape",
        "mic": "mic",
        "check": "checkmark",
        "eye": "eye",
        "chevronRight": "chevron.right",
        "chevronLeft": "chevron.left",
        "wand": "wand.and.stars",
        "scissors": "scissors",
        "message": "bubble.left.and.bubble.right",
        "layers": "square.stack.3d.up",
        "x": "xmark",
        "arrowRight": "arrow.right",
        "refresh": "arrow.triangle.2.circlepath",
        "clock": "clock",
        "trash": "trash",
    ]

    var body: some View {
        Image(systemName: Self.map[name] ?? "questionmark")
            .font(.system(size: size, weight: .medium))
            .foregroundStyle(color)
    }
}
