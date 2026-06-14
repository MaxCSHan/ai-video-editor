import SwiftUI

/// Spacing, radii, control sizing and layout rails — ported from
/// `tokens/spacing.css`. 4px grid; dense by default (pixels go to the
/// timeline, not the chrome).
enum VXMetrics {
    // Radii
    static let radiusXS: CGFloat = 3   // tags
    static let radiusSM: CGFloat = 4   // thumbs, small controls
    static let radiusMD: CGFloat = 6   // buttons, inputs, rows
    static let radiusLG: CGFloat = 8   // cards, video wells, panels
    static let radiusXL: CGFloat = 12  // modals, the window

    // Control heights (native macOS hit targets)
    static let controlSM: CGFloat = 24
    static let controlMD: CGFloat = 30
    static let controlLG: CGFloat = 38

    // Layout rails
    static let railSidebar: CGFloat = 248
    static let railInspector: CGFloat = 320
    static let railSection: CGFloat = 210
    static let toolbarH: CGFloat = 52
    static let titlebarH: CGFloat = 38

    static let hairline: CGFloat = 1
}

/// Motion — "cut, don't bounce." Short ease-out, no springs.
enum VXMotion {
    static let fast = Animation.easeOut(duration: 0.12)
    static let base = Animation.easeOut(duration: 0.16)
    static let slow = Animation.easeOut(duration: 0.24)
}
