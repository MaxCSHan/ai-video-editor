import SwiftUI

/// The 15-hue editorial "purpose" vocabulary — functional data-viz, not
/// decoration. Every EDL segment carries one purpose; its hue colors the
/// timeline block, the EDL row tag, and the inspector header. Hues are fixed
/// (ported from `--purpose-*` in colors.css) — never recolor them.
enum Purpose: String, CaseIterable {
    case hook, intro, establish, context, stakes
    case build_up, action, reaction, tension, climax
    case payoff, reflection, b_roll, cutaway, outro

    var hex: UInt {
        switch self {
        case .hook:       return 0xE74C3C
        case .intro:      return 0x3498DB
        case .establish:  return 0x2C3E50
        case .context:    return 0x2980B9
        case .stakes:     return 0xD35400
        case .build_up:   return 0xF1C40F
        case .action:     return 0xE67E22
        case .reaction:   return 0xF39C12
        case .tension:    return 0xE74C3C
        case .climax:     return 0xC0392B
        case .payoff:     return 0x27AE60
        case .reflection: return 0x16A085
        case .b_roll:     return 0x7F8C8D
        case .cutaway:    return 0x95A5A6
        case .outro:      return 0x8E44AD
        }
    }

    var color: Color { Color(hex: hex) }

    /// Human label: `build_up` → "build up".
    var label: String { rawValue.replacingOccurrences(of: "_", with: " ") }

    /// Tolerant lookup — LLM output occasionally uses unknown purposes.
    static func from(_ raw: String) -> Purpose {
        Purpose(rawValue: raw) ?? .b_roll
    }

    static func color(for raw: String) -> Color { from(raw).color }
}
