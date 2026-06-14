import SwiftUI

/// Typography tokens — the Apple system stack (SF Pro via `.system`, SF Mono via
/// `.monospaced`), 13px base, with the signature uppercase eyebrow. Ported from
/// `tokens/typography.css`. Mono figures are tabular so timecodes align.
enum VXFont {
    static let display = Font.system(size: 28, weight: .semibold)
    static let title   = Font.system(size: 20, weight: .semibold)
    static let heading = Font.system(size: 16, weight: .semibold)
    static let lg      = Font.system(size: 14)
    static let base    = Font.system(size: 13)
    static let sm      = Font.system(size: 12)
    static let xs      = Font.system(size: 11)
    static let eyebrow = Font.system(size: 10, weight: .semibold)

    static func mono(_ size: CGFloat = 12, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

/// The uppercase, 1.5px-tracked section label that opens every section
/// ("EDIT DECISION LIST"). The most repeated VX typographic device.
struct Eyebrow: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(VXFont.eyebrow)
            .tracking(1.5)
            .foregroundStyle(VXColor.textFaint)
    }
}
