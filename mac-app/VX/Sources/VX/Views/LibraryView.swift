import SwiftUI

/// The home screen: a grid of project tiles + an import CTA. Ported from
/// `ui_kits/mac-app/ProjectsLibrary.jsx`, wired to live `/projects` data.
struct LibraryView: View {
    @EnvironmentObject var state: AppState
    @State private var query = ""

    private let columns = [GridItem(.adaptive(minimum: 232), spacing: 16)]

    private var filtered: [ProjectSummary] {
        query.isEmpty ? state.projects
            : state.projects.filter { $0.name.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        VStack(spacing: 0) {
            VXToolbar(left: {
                Text("Library").font(VXFont.title).foregroundStyle(VXColor.textPrimary)
            }, right: {
                VXButton(title: "New Project", variant: .primary, icon: "plus") { state.route = .briefing }
            })

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    searchField
                    if let err = state.loadError {
                        Text(err).font(VXFont.sm).foregroundStyle(VXColor.statusWarning)
                    }
                    LazyVGrid(columns: columns, spacing: 16) {
                        ForEach(filtered) { p in ProjectTile(p: p) { state.open(p) } }
                        ImportTile { state.route = .briefing }
                    }
                }
                .padding(.horizontal, 28).padding(.top, 20).padding(.bottom, 32)
            }
        }
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            VXIcon(name: "search", size: 14, color: VXColor.textMuted)
            TextField("Search projects", text: $query)
                .textFieldStyle(.plain)
                .font(VXFont.base)
                .foregroundStyle(VXColor.textBody)
        }
        .padding(.horizontal, 10).frame(height: VXMetrics.controlMD).frame(maxWidth: 260)
        .background(VXColor.surfaceCard)
        .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusMD).stroke(VXColor.borderDefault, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
    }
}

struct ProjectTile: View {
    let p: ProjectSummary
    var onOpen: () -> Void

    var body: some View {
        VXCard(selectable: true, padding: 0) {
            VStack(alignment: .leading, spacing: 0) {
                ZStack {
                    LinearGradient(colors: [thumbColor, Color(hex: 0x0C0C0C)], startPoint: .topLeading, endPoint: .bottomTrailing)
                    ZStack {
                        Circle().fill(.black.opacity(0.45)).frame(width: 44, height: 44)
                        VXIcon(name: "play", size: 18, color: .white)
                    }
                    VStack { Spacer(); HStack { Spacer()
                        if p.hasRoughCut { Badge(tone: .success, dot: true, text: "cut") } }
                    }.padding(8)
                }
                .frame(height: 120).clipped()

                VStack(alignment: .leading, spacing: 7) {
                    Text(p.name).font(.system(size: 14, weight: .semibold)).foregroundStyle(VXColor.textPrimary).lineLimit(1)
                    HStack(spacing: 6) {
                        if let v = p.latestVersion { Badge(tone: .accent, text: "v\(v)") }
                        Badge(tone: .neutral, text: p.isTimeline ? "Timeline" : "Story")
                        Badge(tone: .neutral, text: p.provider.capitalized)
                    }
                    HStack {
                        Text("\(p.clipCount) clips").font(VXFont.mono(11)).foregroundStyle(VXColor.textMuted)
                        Spacer()
                        Text(p.hasStoryboard ? "storyboard ready" : "needs analysis")
                            .font(VXFont.mono(11)).foregroundStyle(p.hasStoryboard ? VXColor.textMuted : VXColor.statusWarning)
                    }
                }
                .padding(.horizontal, 13).padding(.vertical, 12)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: onOpen)
    }

    private var thumbColor: Color {
        // Deterministic tint from the name so tiles are visually distinct.
        let hues: [UInt] = [0x1D3A2A, 0x26323F, 0x3A2A26, 0x2C2540, 0x2A3A2C]
        let idx = abs(p.id.hashValue) % hues.count
        return Color(hex: hues[idx])
    }
}

struct ImportTile: View {
    var onTap: () -> Void
    @State private var hovering = false
    var body: some View {
        Button(action: onTap) {
            VStack(spacing: 12) {
                VXIcon(name: "plus", size: 26, color: hovering ? VXColor.accent : VXColor.textMuted)
                Text("Import a folder of clips").font(VXFont.sm)
                    .foregroundStyle(hovering ? VXColor.accent : VXColor.textMuted)
            }
            .frame(maxWidth: .infinity, minHeight: 240)
            .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusLG)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6, 4]))
                .foregroundStyle(hovering ? VXColor.accentBorder : VXColor.borderStrong))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(VXMotion.fast, value: hovering)
    }
}
