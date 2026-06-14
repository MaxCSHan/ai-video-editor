import SwiftUI

/// The AI-guided smart briefing: a quick-scan summary followed by targeted
/// questions. Ported from `ui_kits/mac-app/BriefingView.jsx`. This is the entry
/// point for creating a new project from a folder of clips.
struct BriefingView: View {
    @EnvironmentObject var state: AppState
    @State private var answers: [String] = ["", "", ""]
    @State private var newName = ""
    @State private var sourceDir = ""

    private let questions = [
        "Who is the person who appears in most clips?",
        "What's the emotional core you want the edit to land?",
        "Should the edit stay chronological, or can VX reorder for impact?",
    ]

    var body: some View {
        VStack(spacing: 0) {
            VXToolbar(left: {
                Text("Briefing").font(VXFont.title).foregroundStyle(VXColor.textPrimary)
            }, right: {
                VXButton(title: "Continue to analysis", variant: .primary, icon: "chevronRight") {
                    state.route = .editor
                }
            })
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    newProjectCard
                    quickScanCard
                    ForEach(Array(questions.enumerated()), id: \.offset) { i, q in
                        VStack(alignment: .leading, spacing: 10) {
                            AIBubble(text: q)
                            TextField("Type your answer…", text: $answers[i], axis: .vertical)
                                .textFieldStyle(.plain).font(VXFont.base).foregroundStyle(VXColor.textBody)
                                .lineLimit(2...4)
                                .padding(10)
                                .background(VXColor.surfaceCard)
                                .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusMD).stroke(VXColor.borderDefault, lineWidth: 1))
                                .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
                                .padding(.leading, 39)
                        }
                    }
                    ProgressMeter(label: "Briefing", detail: "\(answeredCount) of \(questions.count) answered",
                                  value: Double(answeredCount), maxValue: Double(questions.count))
                        .padding(.leading, 39)
                }
                .frame(maxWidth: 660)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 28).padding(.vertical, 24)
            }
        }
    }

    private var answeredCount: Int { answers.filter { !$0.isEmpty }.count }

    private var newProjectCard: some View {
        VXCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                Eyebrow("New project")
                HStack(spacing: 10) {
                    TextField("Project name", text: $newName)
                        .textFieldStyle(.plain).font(VXFont.base).foregroundStyle(VXColor.textBody)
                        .padding(8).background(VXColor.surfaceRaised).clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusSM))
                    TextField("/path/to/footage", text: $sourceDir)
                        .textFieldStyle(.plain).font(VXFont.mono(12)).foregroundStyle(VXColor.textBody)
                        .padding(8).background(VXColor.surfaceRaised).clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusSM))
                    VXButton(title: "Import", variant: .secondary, icon: "folder") { importFolder() }
                }
                Text("VX will preprocess every clip (proxies, scenes, audio), then quick-scan the footage to ask sharper questions.")
                    .font(VXFont.xs).foregroundStyle(VXColor.textMuted)
            }
        }
    }

    private var quickScanCard: some View {
        VXCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    VXIcon(name: "eye", size: 16, color: VXColor.textTertiary)
                    Text("Quick scan").font(VXFont.sm).foregroundStyle(VXColor.textPrimary).fontWeight(.semibold)
                    if let p = state.activeProject { Badge(tone: .neutral, text: "\(p.clipCount) clips") }
                    Spacer()
                    Badge(tone: .success, dot: true, text: "Gemini")
                }
                Text("Once footage is imported, VX watches all of it and summarizes the trip, then asks a few questions to sharpen the edit.")
                    .font(VXFont.sm).foregroundStyle(VXColor.textSecondary).lineSpacing(2)
            }
        }
    }

    private func importFolder() {
        guard !newName.isEmpty, !sourceDir.isEmpty else { return }
        Task {
            let req = CreateProjectRequest(name: newName, sourceDir: sourceDir)
            if let job = try? await APIClient.shared.createProject(req) {
                await MainActor.run { state.currentJob = job }
                await state.reloadProjects()
            }
        }
    }
}

struct AIBubble: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: 8).fill(VXColor.accentSoft)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(VXColor.accentBorder, lineWidth: 1))
                    .frame(width: 28, height: 28)
                VXIcon(name: "sparkle", size: 15, color: VXColor.accent)
            }
            Text(text)
                .font(VXFont.base).foregroundStyle(VXColor.textBody).lineSpacing(2)
                .padding(.horizontal, 14).padding(.vertical, 11)
                .background(VXColor.surfaceCard)
                .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusLG).stroke(VXColor.borderDefault, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusLG))
            Spacer(minLength: 0)
        }
    }
}
