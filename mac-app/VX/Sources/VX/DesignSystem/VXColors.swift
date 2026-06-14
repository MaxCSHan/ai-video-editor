import SwiftUI

/// VX color tokens — ported 1:1 from the design system's `tokens/colors.css`.
/// The "editing room": a calm, near-black workspace where footage and the
/// timeline are the only bright things. Emerald is the single action color.
enum VXColor {
    // Raw neutral ladder
    static let black       = Color(hex: 0x060606) // --surface-sunken
    static let gray950     = Color(hex: 0x0A0A0A) // --surface-app
    static let gray900     = Color(hex: 0x111111)
    static let gray880     = Color(hex: 0x141414) // --surface-panel
    static let gray850     = Color(hex: 0x1A1A1A) // --surface-card
    static let gray800     = Color(hex: 0x222222) // --surface-raised / border-default
    static let gray750     = Color(hex: 0x2A2A2A) // --surface-active / border-strong
    static let gray700     = Color(hex: 0x333333)
    static let gray600     = Color(hex: 0x444444)
    static let gray500     = Color(hex: 0x555555)
    static let gray450     = Color(hex: 0x666666)
    static let gray400     = Color(hex: 0x888888)
    static let gray300     = Color(hex: 0xAAAAAA)
    static let gray100     = Color(hex: 0xE0E0E0)
    static let white       = Color(hex: 0xFFFFFF)

    // Brand emerald
    static let emerald700  = Color(hex: 0x1E8449)
    static let emerald600  = Color(hex: 0x229954) // accent-pressed
    static let emerald500  = Color(hex: 0x27AE60) // accent-hover
    static let emerald400  = Color(hex: 0x2ECC71) // signature accent
    static let emerald300  = Color(hex: 0x58D68D)

    // Counterweight red
    static let red600      = Color(hex: 0xC0392B)
    static let red500      = Color(hex: 0xE74C3C)
    static let red400      = Color(hex: 0xFF6B5E)

    // Channel hues
    static let blue400     = Color(hex: 0x4FC3F7) // speaker
    static let purple400   = Color(hex: 0xCE93D8) // music
    static let amber400    = Color(hex: 0xFFB74D) // sfx / warning

    // Semantic surfaces
    static let surfaceSunken = black
    static let surfaceApp    = gray950
    static let surfacePanel  = gray880
    static let surfaceCard   = gray850
    static let surfaceRaised  = gray800
    static let surfaceActive = gray750

    // Borders
    static let borderSubtle  = gray850
    static let borderDefault = gray800
    static let borderStrong  = gray750

    // Text
    static let textPrimary   = white
    static let textBody      = gray100
    static let textSecondary = gray300
    static let textTertiary  = gray400
    static let textMuted     = gray450
    static let textFaint     = gray500
    static let textDisabled  = gray600
    static let textOnAccent  = Color(hex: 0x04230F)

    // Accent / action
    static let accent        = emerald400
    static let accentHover   = emerald500
    static let accentPressed = emerald600
    static let accentSoft    = Color(hex: 0x2ECC71, opacity: 0.15)
    static let accentSoftStrong = Color(hex: 0x2ECC71, opacity: 0.30)
    static let accentBorder  = Color(hex: 0x2ECC71, opacity: 0.55)
    static let accentRing    = Color(hex: 0x2ECC71, opacity: 0.45)

    // Scrubber poles
    static let markIn  = emerald400
    static let markOut = red500

    // Status
    static let statusSuccess = emerald400
    static let statusDanger  = red500
    static let statusWarning = amber400
    static let statusInfo    = blue400

    // Transcript channels
    static let transcriptSpeaker = blue400
    static let transcriptMusic   = purple400
    static let transcriptSFX     = amber400
    static let transcriptActiveBG = Color(hex: 0x2D4A2D)

    // Vibrancy / scrim
    static let materialChrome  = Color(hex: 0x121212, opacity: 0.72)
    static let materialOverlay = Color(hex: 0x060606, opacity: 0.85)
}

extension Color {
    init(hex: UInt, opacity: Double = 1.0) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255.0,
            green: Double((hex >> 8) & 0xFF) / 255.0,
            blue: Double(hex & 0xFF) / 255.0,
            opacity: opacity
        )
    }
}
