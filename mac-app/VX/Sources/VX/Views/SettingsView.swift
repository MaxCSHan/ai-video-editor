import SwiftUI

/// Settings — provider, language, visual mode, snapping. Ported from the
/// SettingsView in `ui_kits/mac-app/index.html`.
struct SettingsView: View {
    @State private var provider = "gemini"
    @State private var language = "en"
    @State private var visual = true
    @State private var snap = true

    var body: some View {
        VStack(spacing: 0) {
            VXToolbar(left: {
                Text("Settings").font(VXFont.title).foregroundStyle(VXColor.textPrimary)
            })
            ScrollView {
                VXCard(padding: 4) {
                    VStack(spacing: 0) {
                        SettingRow(label: "Default provider", hint: "Used for transcription, reviews and assembly.") {
                            Picker("", selection: $provider) {
                                Text("Gemini").tag("gemini"); Text("Claude").tag("claude")
                            }.labelsHidden().frame(width: 130)
                        }
                        SettingRow(label: "Language", hint: "TUI + AI-generated narration.") {
                            Picker("", selection: $language) {
                                Text("English").tag("en"); Text("繁體中文").tag("zh-TW")
                            }.labelsHidden().frame(width: 130)
                        }
                        SettingRow(label: "Visual mode", hint: "Phase 2 sees proxy videos for richer edits.") {
                            Toggle("", isOn: $visual).labelsHidden().toggleStyle(.switch).tint(VXColor.accent)
                        }
                        SettingRow(label: "Snap to scene cuts", hint: "Magnetise the scrubber to detected cuts.", last: true) {
                            Toggle("", isOn: $snap).labelsHidden().toggleStyle(.switch).tint(VXColor.accent)
                        }
                    }
                }
                .frame(maxWidth: 560)
                .frame(maxWidth: .infinity)
                .padding(28)
            }
        }
    }
}

struct SettingRow<Control: View>: View {
    let label: String
    let hint: String
    var last: Bool = false
    @ViewBuilder var control: () -> Control

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(VXFont.base).foregroundStyle(VXColor.textBody).fontWeight(.medium)
                Text(hint).font(VXFont.xs).foregroundStyle(VXColor.textMuted)
            }
            Spacer()
            control()
        }
        .padding(.vertical, 13).padding(.horizontal, 12)
        .overlay(last ? nil : Rectangle().frame(height: 1).foregroundStyle(VXColor.borderSubtle), alignment: .bottom)
    }
}
